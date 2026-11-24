@echo off
REM ===========================================================================
REM  CSULB Lost & Found -- label poller launcher
REM
REM  Copy this file to run_poller.bat, fill in the three values below, and point
REM  Task Scheduler at run_poller.bat instead of at python.exe directly.
REM
REM  Why a .bat rather than Windows environment variables:
REM
REM    * A scheduled task running as a different account cannot see another
REM      user's "User variables". That failure is silent -- the poller starts,
REM      finds the placeholder Worker URL, and does nothing.
REM    * Everything the poller needs is visible in one file you can open and
REM      read, instead of spread across a settings dialog nobody remembers
REM      changing.
REM    * Changing the poller key is editing one line here and restarting,
REM      rather than a sign-out/sign-in cycle.
REM
REM  run_poller.bat is gitignored because it holds the poller key. Do not
REM  commit it, and do not put the key in this example file.
REM ===========================================================================

REM -- Your Worker's address (no trailing slash).
set "LNF_WORKER_URL=https://lf-label-printer.david-pedersen.workers.dev"

REM -- Must match the Poller API Key in the admin panel at /admin.html.
set "LNF_POLLER_KEY=change-me-poller-key"

REM -- The EXACT Windows printer name. Find it with:
REM        python label_poller.py --list-printers
set "LNF_PRINTER=Brother QL-1050"

REM -- Optional. Length of each label along the tape, in dots at 300 dpi.
REM    300 is about 25 mm. Only change this if the labels come out the wrong
REM    length; see "If the label lands in the wrong place" in SETUP.md.
REM set "LABEL_LENGTH_DOTS=300"

REM Run from this file's own folder, so it works whatever directory Task
REM Scheduler happens to start in.
cd /d "%~dp0"

python label_poller.py
