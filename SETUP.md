# CSULB Lost & Found -- Cloudflare-Hosted Label Printer

Fully remote-accessible version. Students/staff access a real URL from any
network or device. No IP addresses, no VLAN issues, no firewall rules.

## Architecture

- **Frontend** (`public/index.html`, `public/admin.html`) -- deployed to Cloudflare Pages
- **API** (`worker.js`) -- deployed to Cloudflare Workers
- **Storage** -- two Cloudflare KV namespaces (settings + print job queue)
- **Poller** (`label_poller.py`) -- runs on the print server, replaces the old Flask app

No Supabase needed -- everything lives in Cloudflare.

---

## Step 1 -- Deploy the Worker (API)

On your machine (or the print server), install Wrangler if you don't have it:

```
npm install -g wrangler
wrangler login
```

Create the two KV namespaces:

```
wrangler kv:namespace create LNF_SETTINGS
wrangler kv:namespace create LNF_JOBS
```

Each command prints an `id`. Open `wrangler.toml` and paste both IDs in:

```toml
[[kv_namespaces]]
binding = "LNF_SETTINGS"
id = "paste-id-here"

[[kv_namespaces]]
binding = "LNF_JOBS"
id = "paste-id-here"
```

Deploy:

```
wrangler deploy
```

It'll print your Worker URL, something like:

```
https://lnf-label-worker.YOUR-SUBDOMAIN.workers.dev
```

Visit `https://YOUR-WORKER-URL/api/bootstrap` once in a browser -- this seeds
the default settings (prefix `26`, password `lnf2026`, admin password
`admin2026`). **Change these immediately in the admin panel after first login.**

---

## Step 2 -- Point the frontend at your Worker

Open both `public/index.html` and `public/admin.html` and find this line near
the top of the `<script>`:

```js
const API = "https://lnf-labels-api.YOUR-SUBDOMAIN.workers.dev";
```

Replace it with your actual Worker URL from Step 1.

---

## Step 3 -- Deploy the frontend to Cloudflare Pages

From the `lnf-cloudflare` folder:

```
wrangler pages deploy public --project-name=lnf-labels
```

It'll give you a URL like `https://lnf-labels.pages.dev` -- that's what you
share with students and staff.

(Optional) Add a custom domain/subdomain in the Cloudflare Pages dashboard
under your project > Custom domains, e.g. `labels.yourdomain.com`.

---

## Step 4 -- Set up the poller on the print server

```
pip install requests pillow pywin32
```

P-touch Editor is **not** needed, and neither are `pyautogui`, `pygetwindow` or
`psutil`. The poller talks to the QL-1050 directly. See
**Upgrading the poller** at the end of this file.

The poller reads three settings from the environment:

| Variable | Value |
| --- | --- |
| `LNF_WORKER_URL` | your Worker's address, no trailing slash |
| `LNF_POLLER_KEY` | must match the Poller API Key in the admin panel |
| `LNF_PRINTER` | the exact Windows printer name -- see below |

**Set them in a launcher file, not in Windows.** Copy `run_poller.example.bat`
to `run_poller.bat`, fill in the three values, and point Task Scheduler at that
file. A scheduled task running as a different account cannot see another user's
"User variables", and when that happens the poller starts, finds the placeholder
URL, and quietly does nothing. A .bat keeps everything in one readable place and
means changing the key is editing one line and restarting.

`run_poller.bat` is gitignored -- it holds the key, so it stays on the print
server.

Find the exact printer name:

```
python label_poller.py --list-printers
```

Prove the label looks right before involving the printer:

```
python label_poller.py --preview label.png
```

Open `label.png`. That is the real label at print resolution. Then print one:

```
python label_poller.py --selftest
```

Run it:

```
run_poller.bat
```

Leave this running on the print server at all times -- it checks for new jobs
every few seconds.

### Start it automatically

Task Scheduler -> Create Task (not "Basic Task"):

- **General**: Run whether user is logged on or not; Run with highest
  privileges unchecked.
- **Triggers**: At log on (or At startup, if you used "whether logged on or
  not").
- **Actions**: Start a program -> `C:\path	oun_poller.bat`.
  Leave Arguments empty; set **Start in** to the same folder.
- **Settings**: untick "Stop the task if it runs longer than..." -- this is
  meant to run forever. Tick "If the task fails, restart every 1 minute".

The .bat already `cd`s to its own folder, so it works whatever directory the
scheduler starts it in.

---

## Step 5 -- First login and admin setup

1. Go to your Pages URL, e.g. `https://lnf-labels.pages.dev`
2. Log in with the default password `lnf2026`
3. Go to `https://lnf-labels.pages.dev/admin.html`
4. Log in with the default admin password `admin2026`
5. Set:
   - Year prefix (e.g. `26`)
   - A new staff/student password
   - A new admin password
   - A new Poller API Key -- **then update `LNF_POLLER_KEY` on the print
     server to match, and restart the poller**
6. Save

---

## Day-to-day use

- Students/staff go to your Pages URL, log in, fill in the form, click Print.
- The job is queued in Cloudflare KV.
- The poller on the print server picks it up within a few seconds, renders the
  labels, and sends them to the QL-1050.
- Works from any network, any device, anywhere -- no VLAN or firewall issues.

## Updating settings later

Go to `/admin.html` any time to change the prefix, passwords, or limits.
Changes apply immediately -- no redeploy needed.

## Auto-start the poller (optional)

Add a Windows Task Scheduler entry:
- Program: `python`
- Arguments: `C:\path\to\label_poller.py`
- Trigger: At log on
- Run as: the print server's user account


---

## Upgrading the poller (2026-08-31)

`label_poller.py` no longer drives P-touch Editor. The old version is kept as
`label_poller_ptouch.py` in case a rollback is needed.

### Why

The old poller opened `Item Code Layout (1).lbx` in P-touch Editor, rewrote a
CSV the template was linked to, sent `Ctrl+P` as simulated keystrokes, hunted
the print dialog for a button called **All Records**, clicked it, waited four
seconds, and reported success.

Every step there is a guess about the state of someone else's GUI. A screensaver
or a Windows toast steals focus and the keystrokes land elsewhere. P-touch takes
eight seconds to load on a good day, so the fixed sleep either wasted time or
fired early. The CSV is one file lock away from failing, which is why it needed
six retries.

And the last step is the real problem: **clicking Print is not evidence that
anything printed.** A printer that was off, jammed, or out of tape produced a
queue full of completed jobs and a counter with no labels on it.

### What replaces it

`ql_raster.py` renders the label itself, builds Brother QL raster commands, and
hands them to the Windows spooler in RAW mode. No GUI, no template file, no CSV,
no P-touch process, no focus to steal. A job is only reported complete once the
spooler says it left the queue without an error; anything else comes back as a
failure with a sentence someone at the printer can act on ("the printer cover is
open", "the printer is out of labels").

If the poller is killed mid-print, it writes `state/in-flight.json` first and
reports that job as an *unknown* outcome on its next start, rather than silently
printing the same range twice.

### To upgrade

1. Copy `label_poller.py` and `ql_raster.py` to the print server.
2. `pip install pillow` (requests and pywin32 are already there).
3. Copy `run_poller.example.bat` to `run_poller.bat` and fill in the three
   values (Step 4 above).
4. `python label_poller.py --preview label.png`, and look at it.
5. `python label_poller.py --selftest` -- one real label.
6. Stop the old poller, start the new one.

Nothing on the Worker, the KV queue, the web form, or the admin panel changes.
The two pollers speak the identical protocol, so you can switch back by running
`label_poller_ptouch.py` instead (it needs `pip install pyautogui pygetwindow
psutil` and P-touch Editor back).

### If the label lands in the wrong place

Four constants at the top of `ql_raster.py` control the artwork. They are plain
numbers rather than a library's internals precisely so they can be adjusted:

| Constant | Meaning | Symptom if wrong |
| --- | --- | --- |
| `PRINT_HEAD_BYTES` | 162 -- the QL-1050 head is 1296 dots wide | Nothing prints, or garbage |
| `PRINTABLE_DOTS` | 696 -- printable width of 62 mm tape | Label clipped or narrow |
| `RIGHT_MARGIN_DOTS` | 56 -- offset from the head's edge | Label shifted sideways |
| `LABEL_LENGTH_DOTS` | 300 (~25 mm) along the tape | Label too long or short |

Set with `LABEL_LENGTH_DOTS` in the environment, or edit the file. These settings
are for **DK-2205, 62 mm continuous**; a different roll needs `MEDIA_WIDTH_MM`
and `PRINTABLE_DOTS` changed together, and die-cut rolls also need the media type
byte set to `0x0B`.

`python label_poller.py --dump label.prn` captures exactly what would be sent,
with no printer attached.

### ql_raster.py is a mirrored file

An identical copy lives in `cairn/agent/ql_raster.py`. Both systems must put the
same ink in the same place on the same roll, so there is one implementation.
**When you change it here, copy it there** -- and the other way round.
