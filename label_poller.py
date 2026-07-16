"""
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
WORKER_URL    = "https://lf-label-printer.david-pedersen.workers.dev"
POLLER_KEY    = "change-me-poller-key"     # Must match pollerApiKey in admin panel
TEMPLATE_PATH = r"C:\Users\1\Desktop\Item Code Layout (1).lbx"
PTOUCH_CSV    = r"C:\Users\1\Desktop\p_touch_clean_database.csv"
POLL_INTERVAL = 4   # seconds between checks
# ─────────────────────────────────────────────────────────────────────────────

pyautogui.FAILSAFE = False

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("LabelPoller")


def fetch_pending_jobs():
    try:
        r = requests.get(
            f"{WORKER_URL}/api/jobs/pending",
            headers={"X-Poller-Key": POLLER_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Poll failed: {r.status_code} {r.text}")
            return []
        data = r.json()
        return data.get("jobs", [])
    except Exception as e:
        log.error(f"Poll error: {e}")
        return []


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


def write_csv(rcv_fmt, exp_fmt, prefix, start, quantity):
    lines = ["Exp Date,Code,ReceivedDate"]
    for i in range(quantity):
        code = f"{prefix}-{str(start + i).zfill(4)}"
        lines.append(f"{exp_fmt},{code},{rcv_fmt}")
    with open(PTOUCH_CSV, "w", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    log.info(f"CSV written: {quantity} row(s)  |  {prefix}-{str(start).zfill(4)} to {prefix}-{str(start+quantity-1).zfill(4)}")


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
    kill_ptouch()
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
    for _ in range(10):
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
        log.warning("Print dialog not found, pressing Enter as fallback...")
        pyautogui.press("enter")

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

    while True:
        jobs = fetch_pending_jobs()
        for job in jobs:
            process_job(job)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
