"""
CSULB Lost & Found -- Label Print Poller
========================================

Runs on the print server. Polls the Cloudflare Worker for queued print jobs
submitted from the hosted web form, and drives the Brother QL-1050 directly.

Nothing about the Worker, the KV queue, the web form, or the passwords changed.
This file talks to exactly the same endpoints as before. What changed is how a
job gets printed.

WHAT THIS REPLACES
------------------
The previous version opened `Item Code Layout (1).lbx` in P-touch Editor,
rewrote a CSV the template was linked to, sent Ctrl+P as simulated keystrokes,
hunted the print dialog for a button called "All Records", clicked it, waited
four seconds, and reported success.

Every step there is a guess about the state of somebody else's GUI:

  * a screensaver, a Windows update toast, or a colleague clicking anything
    steals focus, and the keystrokes land in the wrong window;
  * P-touch takes eight seconds to load on a good day and longer under load,
    so the fixed sleep either wasted time or fired early;
  * the CSV is one file lock away from failing, which is why it needed six
    retries;
  * and worst of all, clicking Print is not evidence that anything printed.
    A printer that was off, jammed, or out of tape produced a queue full of
    completed jobs and a counter with no labels on it.

None of that exists now. There is no GUI, no template, no CSV, no P-touch
Editor process, and no focus to steal. `ql_raster` renders the label, builds
Brother raster commands, and hands them to the Windows spooler in RAW mode --
and a job is only reported complete once the spooler says the job left the
queue without an error. Anything else is reported as a failure with a sentence
someone at the printer can act on.

INSTALL
-------
    pip install requests pillow pywin32

    (pyautogui, pygetwindow and psutil are no longer needed. So is P-touch
     Editor -- this does not use it, and does not care whether it is installed.)

CONFIGURE
---------
Copy run_poller.example.bat to run_poller.bat and fill in the three values:

    LNF_WORKER_URL, LNF_POLLER_KEY, LNF_PRINTER

The poller key is a secret and does NOT belong in this file -- it would be
committed. The Worker URL defaults to the real one and is not secret.

RUN
---
    python label_poller.py                  poll forever
    python label_poller.py --list-printers  show what Windows can see
    python label_poller.py --preview x.png  render a sample label to a PNG
    python label_poller.py --selftest       print one real sample label
    python label_poller.py --once           drain the queue once and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

from ql_raster import (
    LABEL_LENGTH_DOTS,
    MEDIA_NAME,
    PRINTABLE_DOTS,
    PrinterError,
    build_job,
    check_printer,
    list_printers,
    render_label,
    send_raw,
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
WORKER_URL = os.environ.get(
    "LNF_WORKER_URL", "https://lf-label-printer.david-pedersen.workers.dev"
).rstrip("/")
POLLER_KEY = os.environ.get("LNF_POLLER_KEY", "change-me-poller-key")
PRINTER = os.environ.get("LNF_PRINTER", "Brother QL-1050")
POLL_INTERVAL = float(os.environ.get("LNF_POLL_INTERVAL", "4"))
# The Worker stores poller:lastSeen with a 30-minute TTL, so a heartbeat only
# has to beat that. It was hardcoded to 30 SECONDS, which is 2,880 KV writes a
# day against a free-tier limit of 1,000 -- the quota was gone by mid-morning.
HEARTBEAT_INTERVAL = float(os.environ.get("LNF_HEARTBEAT_INTERVAL", "600"))

# After a failure, wait before printing again. The failures worth retrying --
# cover open, roll finished, printer switched off -- take a person a few seconds
# to fix, and hammering the queue in the meantime just fills the log.
RETRY_PAUSE_SECONDS = 20

# The in-flight job is written here BEFORE any byte reaches the printer, so a
# power cut mid-print is reported as an unknown outcome rather than forgotten.
STATE_DIR = Path(os.environ.get("LNF_STATE_DIR", Path(__file__).resolve().parent / "state"))

POLLER_VERSION = "2.0.0"
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("LabelPoller")


# ── talking to the Worker ──────────────────────────────────────────────────

_conn = {"online": True, "failures": 0, "down_since": 0.0, "last_repeat": 0.0}


def _note_connection(ok: bool, error: str = "") -> None:
    """
    Log connection state on transition, not on every failed poll.

    The print server's network drops intermittently. A line every four seconds
    is a log nobody reads, so an outage is logged once when it starts, once
    every five minutes while it lasts, and once when it clears -- with how long
    it lasted, which is the only part anyone ever wants.
    """
    now = time.monotonic()
    if ok:
        if not _conn["online"]:
            log.info("Reconnected to the Worker after %.0fs offline (%d failed polls).",
                     now - _conn["down_since"], _conn["failures"])
        _conn["online"], _conn["failures"] = True, 0
        return
    _conn["failures"] += 1
    if _conn["online"]:
        log.error("Lost connection to the Worker: %s", error)
        _conn.update(online=False, down_since=now, last_repeat=now)
    elif now - _conn["last_repeat"] > 300:
        log.warning("Still offline (%d failed polls so far): %s", _conn["failures"], error)
        _conn["last_repeat"] = now


def _headers() -> dict:
    return {"X-Poller-Key": POLLER_KEY, "Content-Type": "application/json"}


def fetch_pending_jobs() -> list[dict]:
    try:
        r = requests.get(f"{WORKER_URL}/api/jobs/pending", headers=_headers(), timeout=10)
        if r.status_code != 200:
            _note_connection(False, f"HTTP {r.status_code}: {r.text[:200]}")
            return []
        _note_connection(True)
        return r.json().get("jobs", [])
    except Exception as exc:
        _note_connection(False, str(exc))
        return []


def send_heartbeat() -> None:
    """Tell the admin panel the poller is alive, so nobody has to guess."""
    try:
        requests.post(f"{WORKER_URL}/api/poller/heartbeat", headers=_headers(), timeout=8)
    except Exception:
        pass   # Not critical -- the next poll retries.


def _report(path: str, payload: dict | None = None) -> bool:
    """
    Report an outcome, retrying hard.

    This is the one call that must not be lost: a job whose outcome never
    arrives sits pending and prints again on the next poll, which is how you end
    up with the same label twice.
    """
    for attempt in range(5):
        try:
            requests.post(f"{WORKER_URL}{path}", headers=_headers(),
                          json=payload or {}, timeout=10).raise_for_status()
            return True
        except Exception as exc:
            log.warning("Could not report outcome (attempt %d/5): %s", attempt + 1, exc)
            time.sleep(min(2 ** attempt, 15))
    log.error("Gave up reporting %s.", path)
    return False


def mark_complete(job_id: str) -> bool:
    return _report(f"/api/jobs/{job_id}/complete")


def mark_failed(job_id: str, error) -> bool:
    return _report(f"/api/jobs/{job_id}/fail", {"error": str(error)})


# ── the in-flight record ───────────────────────────────────────────────────

def _state_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "in-flight.json"


def mark_in_flight(job: dict, codes: list[str]) -> None:
    _state_path().write_text(json.dumps({
        "id": job["id"], "codes": codes,
        "startedAt": datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")


def clear_in_flight() -> None:
    path = _state_path()
    if path.exists():
        path.unlink()


def recover_in_flight() -> None:
    """
    Report a job the previous run died in the middle of.

    Without this the job stays pending in KV and prints again on the next poll,
    with no way to tell whether the first attempt produced labels. Saying "we
    don't know, check the roll" is worth more than silently doing it twice.
    """
    path = _state_path()
    if not path.exists():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        path.unlink()
        return
    log.error("Found job %s left in flight from %s -- the poller stopped mid-print.",
              record.get("id"), record.get("startedAt"))
    mark_failed(record.get("id", ""), (
        "The poller stopped while this job was printing, so it is not known whether "
        "the labels came out. Check the printer before printing this range again."
    ))
    path.unlink()


# ── jobs ───────────────────────────────────────────────────────────────────

def job_labels(job: dict) -> list[dict]:
    """
    Turn the Worker's job into the labels to print.

    The Worker sends a starting number and a quantity, exactly as it always
    has -- {prefix: "26", start: 1179, quantity: 10, rcv: "2026-08-31",
    exp: "2026-09-30"} -- so nothing on that side needs redeploying.
    """
    prefix = str(job.get("prefix", "26"))
    start = int(job["start"])
    quantity = int(job["quantity"])
    return [
        {
            "code": f"{prefix}-{start + i:04d}",
            "receivedOn": job.get("rcv", ""),
            "expiresOn": job.get("exp", ""),
        }
        for i in range(quantity)
    ]


def process_job(job: dict) -> bool:
    """Print one job. Returns True if it printed."""
    job_id = job["id"]
    try:
        labels = job_labels(job)
    except (KeyError, TypeError, ValueError) as exc:
        log.error("Job %s is malformed: %s", job_id, exc)
        mark_failed(job_id, f"The job was malformed: {exc}")
        return False

    if not labels:
        mark_failed(job_id, "The job contained no labels.")
        return False

    codes = [label["code"] for label in labels]
    span = codes[0] if len(codes) == 1 else f"{codes[0]} to {codes[-1]}"
    log.info("Job %s: %d label(s), %s", job_id, len(labels), span)

    mark_in_flight(job, codes)
    try:
        data = build_job(labels)
        detail = send_raw(PRINTER, data, f"L&F labels {span}")
    except PrinterError as exc:
        clear_in_flight()
        log.error("Job %s failed: %s", job_id, exc)
        mark_failed(job_id, str(exc))
        return False
    except Exception as exc:
        clear_in_flight()
        log.exception("Job %s failed unexpectedly.", job_id)
        mark_failed(job_id, f"{type(exc).__name__}: {exc}")
        return False

    clear_in_flight()
    log.info("Job %s done -- %s (%d bytes).", job_id, detail, len(data))
    mark_complete(job_id)
    return True


# ── the loop ───────────────────────────────────────────────────────────────

def main_loop(once: bool = False) -> None:
    recover_in_flight()
    last_heartbeat = 0.0

    while True:
        jobs = fetch_pending_jobs()
        printed = all(process_job(job) for job in jobs) if jobs else True

        if time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL:
            send_heartbeat()
            last_heartbeat = time.monotonic()

        if once and not jobs:
            return
        if not jobs:
            time.sleep(POLL_INTERVAL)
        elif not printed:
            log.info("Pausing %ds before trying again.", RETRY_PAUSE_SECONDS)
            time.sleep(RETRY_PAUSE_SECONDS)


# ── entry point ────────────────────────────────────────────────────────────

SAMPLE = {
    "code": "26-8888",
    "receivedOn": date.today().isoformat(),
    "expiresOn": date.today().replace(day=28).isoformat(),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CSULB Lost & Found label poller (Brother QL-1050).")
    parser.add_argument("--list-printers", action="store_true",
                        help="show the printers Windows can see, then exit")
    parser.add_argument("--preview", metavar="FILE.png",
                        help="render a sample label to a PNG instead of printing")
    parser.add_argument("--dump", metavar="FILE.prn",
                        help="write the raw printer bytes for a sample label")
    parser.add_argument("--selftest", action="store_true",
                        help="print one real sample label and exit")
    parser.add_argument("--once", action="store_true",
                        help="drain the queue once and exit, instead of polling")
    args = parser.parse_args(argv)

    if args.list_printers:
        for name in list_printers():
            print(name)
        return 0

    # These need no Worker credentials on purpose: proving the artwork and the
    # byte stream are right should not require a working network.
    if args.preview:
        render_label(SAMPLE["code"], SAMPLE["receivedOn"], SAMPLE["expiresOn"]).save(args.preview)
        print(f"Wrote {args.preview} "
              f"({PRINTABLE_DOTS} x {LABEL_LENGTH_DOTS} dots, {MEDIA_NAME}).")
        return 0

    if args.dump:
        data = build_job([SAMPLE])
        Path(args.dump).write_bytes(data)
        print(f"Wrote {args.dump} ({len(data)} bytes).")
        return 0

    if args.selftest:
        try:
            detail = send_raw(PRINTER, build_job([SAMPLE]), "L&F self-test")
        except PrinterError as exc:
            print(f"FAILED: {exc}")
            return 1
        print(f"OK: {detail}")
        return 0

    if POLLER_KEY == "change-me-poller-key":
        print("Set LNF_POLLER_KEY to the Poller API Key from the admin panel "
              "(/admin.html). Copy run_poller.example.bat to run_poller.bat and "
              "fill it in there.", file=sys.stderr)
        return 2

    log.info("=" * 62)
    log.info("CSULB Lost & Found label poller %s", POLLER_VERSION)
    log.info("Worker   : %s", WORKER_URL)
    log.info("Printer  : %s", PRINTER)
    log.info("Media    : %s, %d dots long", MEDIA_NAME, LABEL_LENGTH_DOTS)
    log.info("Polling every %.0fs", POLL_INTERVAL)
    log.info("=" * 62)

    try:
        main_loop(once=args.once)
    except KeyboardInterrupt:
        log.info("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
