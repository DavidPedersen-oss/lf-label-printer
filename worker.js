/**
 * CSULB Lost & Found -- Label Printer Worker
 * ============================================
 * Handles auth, print job queueing, and admin settings.
 * Storage: two KV namespaces -- LNF_SETTINGS and LNF_JOBS.
 *
 * Deploy:
 *   wrangler kv:namespace create LNF_SETTINGS
 *   wrangler kv:namespace create LNF_JOBS
 *   (add the returned IDs to wrangler.toml)
 *   wrangler deploy
 *
 * First-time setup: visit /api/bootstrap once to seed default settings.
 */

const DEFAULT_SETTINGS = {
  prefix: "26",
  userPassword: "lnf2026",
  adminPassword: "admin2026",
  maxQuantity: 100,
  maxLabelNumber: 9999,
  pollerApiKey: "change-me-poller-key",
  siteTitle: "Lost & Found Label Printer",
};

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

function randomToken(len = 32) {
  const bytes = crypto.getRandomValues(new Uint8Array(len));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function getSettings(env) {
  const raw = await env.LNF_SETTINGS.get("config");
  if (!raw) {
    await env.LNF_SETTINGS.put("config", JSON.stringify(DEFAULT_SETTINGS));
    return { ...DEFAULT_SETTINGS };
  }
  return { ...DEFAULT_SETTINGS, ...JSON.parse(raw) };
}

async function saveSettings(env, settings) {
  await env.LNF_SETTINGS.put("config", JSON.stringify(settings));
}

async function requireSession(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const token = auth.replace("Bearer ", "").trim();
  if (!token) return null;
  const raw = await env.LNF_SETTINGS.get(`session:${token}`);
  if (!raw) return null;
  return JSON.parse(raw);
}

async function requireAdminSession(request, env) {
  const session = await requireSession(request, env);
  if (!session || session.role !== "admin") return null;
  return session;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    // ---- Bootstrap (safe to call repeatedly, only seeds if empty) ----
    if (path === "/api/bootstrap" && request.method === "GET") {
      const settings = await getSettings(env);
      return json({ ok: true, message: "Settings initialized.", settings: redact(settings) });
    }

    // ---- User login ----
    if (path === "/api/login" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const settings = await getSettings(env);
      if (body.password !== settings.userPassword) {
        return json({ success: false, error: "Incorrect password." }, 401);
      }
      const token = randomToken();
      await env.LNF_SETTINGS.put(`session:${token}`, JSON.stringify({ role: "user" }), {
        expirationTtl: 60 * 60 * 12, // 12 hours
      });
      return json({ success: true, token });
    }

    // ---- Admin login ----
    if (path === "/api/admin/login" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const settings = await getSettings(env);
      if (body.password !== settings.adminPassword) {
        return json({ success: false, error: "Incorrect admin password." }, 401);
      }
      const token = randomToken();
      await env.LNF_SETTINGS.put(`session:${token}`, JSON.stringify({ role: "admin" }), {
        expirationTtl: 60 * 60 * 4, // 4 hours
      });
      return json({ success: true, token });
    }

    // ---- Get public-safe settings (prefix, limits, title) for the form ----
    if (path === "/api/settings" && request.method === "GET") {
      const session = await requireSession(request, env);
      if (!session) return json({ error: "Not authenticated." }, 401);
      const settings = await getSettings(env);
      return json({ success: true, settings: redact(settings) });
    }

    // ---- Update settings (admin only) ----
    if (path === "/api/settings" && request.method === "POST") {
      const session = await requireAdminSession(request, env);
      if (!session) return json({ error: "Admin authentication required." }, 403);
      const body = await request.json().catch(() => ({}));
      const current = await getSettings(env);
      const updated = { ...current, ...sanitizeSettingsInput(body) };
      await saveSettings(env, updated);
      return json({ success: true, settings: redact(updated) });
    }

    // ---- Submit a print job ----
    if (path === "/api/print" && request.method === "POST") {
      const session = await requireSession(request, env);
      if (!session) return json({ error: "Not authenticated." }, 401);

      const body = await request.json().catch(() => ({}));
      const settings = await getSettings(env);

      const { rcv, exp, start, quantity } = body;
      if (!rcv || !exp) return json({ success: false, error: "Missing dates." }, 400);

      const startNum = parseInt(start, 10);
      const qty = parseInt(quantity, 10);

      if (isNaN(startNum) || startNum < 1 || startNum > settings.maxLabelNumber) {
        return json({ success: false, error: `Starting label # must be 1-${settings.maxLabelNumber}.` }, 400);
      }
      if (isNaN(qty) || qty < 1 || qty > settings.maxQuantity) {
        return json({ success: false, error: `Quantity must be 1-${settings.maxQuantity}.` }, 400);
      }
      if (startNum + qty - 1 > settings.maxLabelNumber) {
        return json({ success: false, error: `Labels would exceed ${settings.maxLabelNumber}.` }, 400);
      }

      const jobId = randomToken(12);
      const job = {
        id: jobId,
        rcv,
        exp,
        start: startNum,
        quantity: qty,
        prefix: settings.prefix,
        status: "pending",
        createdAt: new Date().toISOString(),
      };

      await env.LNF_JOBS.put(`job:${jobId}`, JSON.stringify(job), {
        expirationTtl: 60 * 60 * 24, // auto-clean after 24h
      });
      await env.LNF_JOBS.put(`pending:${jobId}`, "1", { expirationTtl: 60 * 60 * 24 });

      return json({ success: true, jobId, message: "Print job queued." });
    }

    // ---- Poller: heartbeat (lets the admin panel show poller health) ----
    if (path === "/api/poller/heartbeat" && request.method === "POST") {
      const key = request.headers.get("X-Poller-Key");
      const settings = await getSettings(env);
      if (key !== settings.pollerApiKey) return json({ error: "Invalid poller key." }, 403);
      await env.LNF_SETTINGS.put("poller:lastSeen", new Date().toISOString(), {
        expirationTtl: 60 * 10, // if no heartbeat in 10 min, considered offline
      });
      return json({ success: true });
    }

    // ---- Poller status (admin panel reads this) ----
    if (path === "/api/poller/status" && request.method === "GET") {
      const session = await requireAdminSession(request, env);
      if (!session) return json({ error: "Admin authentication required." }, 403);
      const lastSeen = await env.LNF_SETTINGS.get("poller:lastSeen");
      const list = await env.LNF_JOBS.list({ prefix: "pending:" });
      return json({
        success: true,
        online: !!lastSeen,
        lastSeen,
        pendingJobs: list.keys.length,
      });
    }

    // ---- Poller: list pending jobs ----
    if (path === "/api/jobs/pending" && request.method === "GET") {
      const key = request.headers.get("X-Poller-Key");
      const settings = await getSettings(env);
      if (key !== settings.pollerApiKey) return json({ error: "Invalid poller key." }, 403);

      const list = await env.LNF_JOBS.list({ prefix: "pending:" });
      const jobs = [];
      for (const entry of list.keys) {
        const jobId = entry.name.replace("pending:", "");
        const raw = await env.LNF_JOBS.get(`job:${jobId}`);
        if (raw) jobs.push(JSON.parse(raw));
      }
      jobs.sort((a, b) => new Date(a.createdAt) - new Date(b.createdAt));
      return json({ success: true, jobs });
    }

    // ---- Poller: mark job complete ----
    if (path.match(/^\/api\/jobs\/[a-f0-9]+\/complete$/) && request.method === "POST") {
      const key = request.headers.get("X-Poller-Key");
      const settings = await getSettings(env);
      if (key !== settings.pollerApiKey) return json({ error: "Invalid poller key." }, 403);

      const jobId = path.split("/")[3];
      const raw = await env.LNF_JOBS.get(`job:${jobId}`);
      if (!raw) return json({ error: "Job not found." }, 404);

      const job = JSON.parse(raw);
      job.status = "printed";
      job.completedAt = new Date().toISOString();
      await env.LNF_JOBS.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: 60 * 60 * 24 });
      await env.LNF_JOBS.delete(`pending:${jobId}`);

      return json({ success: true });
    }

    // ---- Poller: mark job failed ----
    if (path.match(/^\/api\/jobs\/[a-f0-9]+\/fail$/) && request.method === "POST") {
      const key = request.headers.get("X-Poller-Key");
      const settings = await getSettings(env);
      if (key !== settings.pollerApiKey) return json({ error: "Invalid poller key." }, 403);

      const jobId = path.split("/")[3];
      const body = await request.json().catch(() => ({}));
      const raw = await env.LNF_JOBS.get(`job:${jobId}`);
      if (!raw) return json({ error: "Job not found." }, 404);

      const job = JSON.parse(raw);
      job.status = "failed";
      job.error = body.error || "Unknown error";
      await env.LNF_JOBS.put(`job:${jobId}`, JSON.stringify(job), { expirationTtl: 60 * 60 * 24 });
      await env.LNF_JOBS.delete(`pending:${jobId}`);

      return json({ success: true });
    }

    // ---- Check job status (frontend polls this after submitting) ----
    if (path.match(/^\/api\/jobs\/[a-f0-9]+$/) && request.method === "GET") {
      const session = await requireSession(request, env);
      if (!session) return json({ error: "Not authenticated." }, 401);
      const jobId = path.split("/")[3];
      const raw = await env.LNF_JOBS.get(`job:${jobId}`);
      if (!raw) return json({ error: "Job not found." }, 404);
      return json({ success: true, job: JSON.parse(raw) });
    }

    return json({ error: "Not found." }, 404);
  },
};

function redact(settings) {
  // Never send passwords or the poller key to the browser.
  const { userPassword, adminPassword, pollerApiKey, ...safe } = settings;
  return safe;
}

function sanitizeSettingsInput(body) {
  const allowed = [
    "prefix",
    "userPassword",
    "adminPassword",
    "maxQuantity",
    "maxLabelNumber",
    "pollerApiKey",
    "siteTitle",
  ];
  const out = {};
  for (const k of allowed) {
    if (body[k] !== undefined && body[k] !== "") out[k] = body[k];
  }
  if (out.maxQuantity) out.maxQuantity = parseInt(out.maxQuantity, 10);
  if (out.maxLabelNumber) out.maxLabelNumber = parseInt(out.maxLabelNumber, 10);
  return out;
}
