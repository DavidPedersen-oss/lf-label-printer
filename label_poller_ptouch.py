"""
SUPERSEDED -- kept only as a rollback.
=====================================

This is the P-touch Editor version of the poller. `label_poller.py` replaced it
on 2026-08-31 with one that renders labels itself and writes Brother raster
commands straight to the spooler -- no GUI, no template, no CSV, and a job is
only reported complete when the spooler says it printed.

Keep this until the new poller has printed a few real runs. To roll back, stop
the new poller and run this file instead; it needs the old dependencies back
(pip install pyautogui pygetwindow psutil) and P-touch Editor installed with
the .lbx template still on the Desktop.

Nothing on the Worker or KV side differs between the two.

--- original header follows ---

CSULB Lost & Found -- Label Print Poller
==========================================
Runs on the print server. Polls the Cloudflare Worker for queued print jobs
submitted from the hosted web form, then automates P-touch Editor exactly
like before -- no local web server, no firewall/VLAN issues.

Setup:
    pip install requests pyautogui pygetwindow pywin32 psutil

Run:
    python label_poller.py
"""

import time, os, logging, sys
import requests
import pyautogui, psutil
import win32gui, win32con

# ── CONFIG ─────────────────────────────────────────────────────────────────────
WORKER_URL    = "https://lnf-labels-api.YOUR-SUBDOMAIN.workers.dev"
POLLER_KEY    = "change-me-poller-key"     # Must match pollerApiKey in admin panel
TEMPLATE_PATH = r"C:\Users\1\Desktop\Item Code Layout (1).lbx"
PTOUCH_CSV    = r"C:\Users\1\Desktop\p_touch_clean_database.csv"
POLL_INTERVAL = 4   # seconds between checks
# See label_poller.py: 30 seconds here was 2,880 KV writes/day against a
# 1,000/day free-tier write limit. The Worker's TTL on poller:lastSeen is 30
# minutes, so anything comfortably under that is fine.
HEARTBEAT_INTERVAL = float(os.environ.get("LNF_HEARTBEAT_INTERVAL", "600"))
# ─────────────────────────────────────────────────────────────────────────────

pyautogui.FAILSAFE = False

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("LabelPoller")


_last_conn_state = {"ok": True, "last_logged": 0, "fail_count": 0}

def fetch_pending_jobs():
    try:
        r = requests.get(
            f"{WORKER_URL}/api/jobs/pending",
            headers={"X-Poller-Key": POLLER_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            _note_connection(False, f"HTTP {r.status_code}: {r.text}")
            return []
        data = r.json()
        _note_connection(True)
        return data.get("jobs", [])
    except Exception as e:
        _note_connection(False, str(e))
        return []


def _note_connection(ok, error=None):
    """Log connection state transitions instead of spamming every failed
    poll -- the print server's network drops intermittently, so this keeps
    the log readable while still recording how long an outage lasted."""
    now = time.time()
    if ok:
        if not _last_conn_state["ok"]:
            down_for = now - _last_conn_state["last_logged"]
            log.info(f"Reconnected to Worker after {down_for:.0f}s offline "
                      f"({_last_conn_state['fail_count']} failed polls).")
        _last_conn_state["ok"] = True
        _last_conn_state["fail_count"] = 0
    else:
        _last_conn_state["fail_count"] += 1
        if _last_conn_state["ok"]:
            # Just went down -- log immediately.
            log.error(f"Lost connection to Worker: {error}")
            _last_conn_state["last_logged"] = now
            _last_conn_state["ok"] = False
        elif now - _last_conn_state.get("_last_repeat_log", 0) > 300:
            # Still down -- only re-log every 5 minutes to avoid spam.
            log.warning(f"Still offline ({_last_conn_state['fail_count']} "
                        f"failed polls so far): {error}")
            _last_conn_state["_last_repeat_log"] = now


def send_heartbeat():
    """Let the Worker know the poller is alive so the admin panel can show
    real status instead of leaving Dave guessing whether it's running."""
    try:
        requests.post(
            f"{WORKER_URL}/api/poller/heartbeat",
            headers={"X-Poller-Key": POLLER_KEY},
            timeout=8,
        )
    except Exception:
        pass  # Heartbeat failures aren't critical -- next poll will retry.


def mark_complete(job_id):
    try:
        requests.post(
            f"{WORKER_URL}/api/jobs/{job_id}/complete",
            headers={"X-Poller-Key": POLLER_KEY},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Failed to mark job complete: {e}")


def mark_failed(job_id, error):
    try:
        requests.post(
            f"{WORKER_URL}/api/jobs/{job_id}/fail",
            headers={"X-Poller-Key": POLLER_KEY},
            json={"error": str(error)},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Failed to mark job failed: {e}")


def write_csv(rcv_fmt, exp_fmt, prefix, start, quantity, retries=6, delay=1.0):
    lines = ["Exp Date,Code,ReceivedDate"]
    for i in range(quantity):
        code = f"{prefix}-{str(start + i).zfill(4)}"
        lines.append(f"{exp_fmt},{code},{rcv_fmt}")
    content = "\r\n".join(lines) + "\r\n"

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with open(PTOUCH_CSV, "w", newline="") as f:
                f.write(content)
            log.info(f"CSV written: {quantity} row(s)  |  {prefix}-{str(start).zfill(4)} to {prefix}-{str(start+quantity-1).zfill(4)}")
            return
        except PermissionError as e:
            last_err = e
            log.warning(f"CSV locked (attempt {attempt}/{retries}), retrying in {delay}s...")
            time.sleep(delay)
    raise RuntimeError(f"Could not write CSV after {retries} attempts: {last_err}")


def kill_ptouch():
    for proc in psutil.process_iter(['name', 'pid']):
        if 'ptedit' in proc.info['name'].lower():
            log.info(f"Closing existing P-touch (PID {proc.info['pid']})")
            proc.terminate()
            try: proc.wait(timeout=3)
            except: proc.kill()
    time.sleep(1)


def find_ptouch_hwnd(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []
        def cb(h, _):
            t = win32gui.GetWindowText(h)
            if "p-touch" in t.lower() or "ptouch" in t.lower():
                found.append(h)
        win32gui.EnumWindows(cb, None)
        if found:
            return found[0]
        time.sleep(0.5)
    return None


def focus_window(hwnd):
    import ctypes
    user32   = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid  = user32.GetWindowThreadProcessId(fg_hwnd, None)
    my_tid  = kernel32.GetCurrentThreadId()
    if fg_tid and fg_tid != my_tid:
        user32.AttachThreadInput(fg_tid, my_tid, True)
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if fg_tid and fg_tid != my_tid:
        user32.AttachThreadInput(fg_tid, my_tid, False)
    time.sleep(0.4)


def automate_print():
    log.info(f"Opening template: {TEMPLATE_PATH}")
    os.startfile(TEMPLATE_PATH)

    log.info("Waiting for P-touch Editor to load (8s)...")
    time.sleep(8)

    hwnd = find_ptouch_hwnd(timeout=10)
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError("P-touch Editor did not open in time.")

    log.info(f"Window ready: '{win32gui.GetWindowText(hwnd)}'")
    focus_window(hwnd)

    pyautogui.hotkey("ctrl", "p")
    log.info("Sent Ctrl+P, waiting for print dialog...")
    time.sleep(2.5)

    print_dlg = None
    for _ in range(20):
        print_dlg = win32gui.FindWindow(None, "Print")
        if print_dlg:
            break
        time.sleep(0.3)

    if print_dlg:
        buttons = []
        def enum_cb(h, _):
            cls  = win32gui.GetClassName(h)
            text = win32gui.GetWindowText(h)
            if cls == "Button":
                buttons.append((h, text))
        win32gui.EnumChildWindows(print_dlg, enum_cb, None)

        all_records_hwnd = None
        print_btn_hwnd   = None
        for h, text in buttons:
            clean = text.replace('&', '').lower()
            if clean.startswith('all records'):
                all_records_hwnd = h
            if clean == 'print':
                print_btn_hwnd = h

        if all_records_hwnd:
            log.info("Clicking All Records...")
            win32api = __import__("win32api")
            win32api.SendMessage(all_records_hwnd, win32con.BM_CLICK, 0, 0)
            time.sleep(0.3)
        else:
            log.warning("All Records button not found: " + str([t for _, t in buttons]))

        if print_btn_hwnd:
            log.info("Clicking Print...")
            win32api = __import__("win32api")
            win32api.SendMessage(print_btn_hwnd, win32con.BM_CLICK, 0, 0)
        else:
            pyautogui.press("enter")
    else:
        # Retry once rather than blindly pressing Enter on an unknown window --
        # a stray Enter could hit the wrong control and silently under-print.
        log.warning("Print dialog not found on first try, retrying Ctrl+P once...")
        pyautogui.hotkey("ctrl", "p")
        time.sleep(3)
        print_dlg = win32gui.FindWindow(None, "Print")
        if print_dlg:
            buttons = []
            def enum_cb2(h, _):
                if win32gui.GetClassName(h) == "Button":
                    buttons.append((h, win32gui.GetWindowText(h)))
            win32gui.EnumChildWindows(print_dlg, enum_cb2, None)
            all_h = next((h for h, t in buttons if t.replace('&','').lower().startswith('all records')), None)
            print_h = next((h for h, t in buttons if t.replace('&','').lower() == 'print'), None)
            win32api = __import__("win32api")
            if all_h:
                win32api.SendMessage(all_h, win32con.BM_CLICK, 0, 0)
                time.sleep(0.3)
            if print_h:
                win32api.SendMessage(print_h, win32con.BM_CLICK, 0, 0)
            else:
                raise RuntimeError("Print dialog reappeared but Print button not found -- aborting rather than guessing.")
        else:
            raise RuntimeError("Print dialog never appeared after two attempts -- job not printed.")

    time.sleep(4)

    hwnd = find_ptouch_hwnd(timeout=3)
    if hwnd and win32gui.IsWindow(hwnd):
        focus_window(hwnd)
        pyautogui.hotkey("alt", "f4")
        time.sleep(1)
        save_hwnd = find_ptouch_hwnd(timeout=2)
        if save_hwnd:
            pyautogui.press("n")

    log.info("Print cycle complete.")


def process_job(job):
    job_id = job["id"]
    log.info(f"Processing job {job_id}: {job['quantity']} label(s)")
    try:
        y, m, d = job["rcv"].split("-")
        rcv_fmt = f"{int(m)}/{int(d)}/{y}"
        ey, em, ed = job["exp"].split("-")
        exp_fmt = f"{int(em)}/{int(ed)}/{ey}"

        # Kill any lingering P-touch instance FIRST so it releases the CSV
        # file lock before we try to write to it (fixes intermittent
        # PermissionError when jobs land back-to-back).
        kill_ptouch()
        write_csv(rcv_fmt, exp_fmt, job["prefix"], job["start"], job["quantity"])
        automate_print()
        mark_complete(job_id)
        log.info(f"Job {job_id} complete.")
    except Exception as e:
        log.error(f"Job {job_id} failed: {e}")
        mark_failed(job_id, e)


def main():
    log.info("=" * 55)
    log.info("CSULB Lost & Found Label Poller")
    log.info(f"Worker   : {WORKER_URL}")
    log.info(f"Template : {TEMPLATE_PATH}")
    log.info(f"Poll every {POLL_INTERVAL}s")
    log.info("=" * 55)

    last_heartbeat = 0
    while True:
        jobs = fetch_pending_jobs()
        for job in jobs:
            process_job(job)

        # Heartbeat every ~30s, not every poll cycle -- keeps it lightweight.
        if time.time() - last_heartbeat > HEARTBEAT_INTERVAL:
            send_heartbeat()
            last_heartbeat = time.time()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
