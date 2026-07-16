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

Visit `https://lf-label-printer.david-pedersen.workers.dev/api/bootstrap`
once in a browser -- this seeds the default settings (prefix `26`, password
`lnf2026`, admin password `admin2026`). **Change these immediately in the
admin panel after first login.**

---

## Step 2 -- Point the frontend at your Worker

The `API` constant inside `public/index.html` and `public/admin.html` has
already been wired to the deployed Worker URL from Step 1:

```js
const API = "https://lf-label-printer.david-pedersen.workers.dev";
```

---

## Step 3 -- Deploy the frontend to Cloudflare Pages

Create a Pages project connected to your GitHub repo (Workers Builds only
deploys the Worker, not the static HTML). Use the Cloudflare dashboard:

1. Cloudflare dashboard → Workers & Pages → Create application → Pages →
   Connect to Git
2. Pick the `DavidPedersen-oss/lf-label-printer` repository
3. **Production branch:** `main`
4. **Framework preset:** `None`
5. **Build command:** (leave empty)
6. **Build output directory:** `public`
7. Click Save and Deploy

It'll give you a URL like `https://lf-label-printer.pages.dev` -- that's what
you share with students and staff.

(Optional) Add a custom domain/subdomain in the Cloudflare Pages dashboard
under your project > Custom domains, e.g. `labels.yourdomain.com`.

---

## Step 4 -- Set up the poller on the print server

```
pip install requests pyautogui pygetwindow pywin32 psutil
```

Open `label_poller.py` and set:

```python
WORKER_URL    = "https://lf-label-printer.david-pedersen.workers.dev"
POLLER_KEY    = "change-me-poller-key"   # must match the admin panel's Poller API Key
```

(`WORKER_URL` is already wired -- you only need to change `POLLER_KEY` after
you set a real one in the admin panel.)

Run it:

```
python label_poller.py
```

Leave this running on the print server at all times -- it checks for new jobs
every few seconds and runs P-touch Editor automation exactly like before.

---

## Step 5 -- First login and admin setup

1. Go to your Pages URL, e.g. `https://lf-label-printer.pages.dev`
2. Log in with the default password `lnf2026`
3. Go to `https://lf-label-printer.pages.dev/admin.html`
4. Log in with the default admin password `admin2026`
5. Set:
   - Year prefix (e.g. `26`)
   - A new staff/student password
   - A new admin password
   - A new Poller API Key -- **then update `POLLER_KEY` in `label_poller.py`
     on the print server to match, and restart the poller**
6. Save

---

## Day-to-day use

- Students/staff go to your Pages URL, log in, fill in the form, click Print.
- The job is queued in Cloudflare KV.
- The poller on the print server picks it up within a few seconds and
  automates P-touch Editor exactly as before.
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
