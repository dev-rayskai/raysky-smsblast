# Deployment Notes — raysky-smsblast

This document explains why the app **in its current form does not deploy cleanly to
Vercel**, what to use instead, and how the app could be re-architected if Vercel
(or a serverless model in general) is a hard requirement.

Audience: the engineering team deciding where to host this.

---

## 1. What the app currently is

- **`send_campaign.py`** — a long-running Python CLI that sends a bulk SMS
  campaign via Twilio. It rate-limits (default 1 msg/sec), sends in batches
  (default 300) with a **60-second pause between batches**, retries transient
  Twilio errors with backoff, and writes progress to `results.csv` and
  `campaign.log` on the local filesystem.
- **`app.py`** — a small Flask web UI that, on a button click, **shells out to
  `send_campaign.py` as a subprocess** (`os.popen(... send_campaign.py ...)`)
  and returns its stdout.

The important property: **this is a stateful, long-running workload.** A single
campaign can run for many minutes to hours, and it persists state to disk as it
goes.

---

## 2. Why Vercel does not work as-is

Vercel runs application code as **serverless functions** — short-lived,
stateless, and read-only. The app violates all three assumptions:

| What the app does | Why it breaks on Vercel |
|---|---|
| Runs campaigns with 60s batch pauses + 1 msg/sec pacing | Serverless functions **time out**: ~10s default, max **60s** (Hobby) / **300s** (Pro). A single 300-message batch alone is ~300s. The function is killed mid-send. |
| Writes `bulk_upload.csv`, `results.csv`, `campaign.log` to disk | The function filesystem is **read-only** except `/tmp`, and `/tmp` is **wiped between invocations** — results never persist and the frontend can't read them back. |
| Shells out via `os.popen(... python send_campaign.py ...)` | There is **no persistent shell/process model**. Spawning a subprocess that runs for minutes is not supported in a serverless function. |

**Net effect:** even if the project "deploys successfully," the send buttons
will time out and lose their results. Serverless is the opposite of what this
workload needs.

> Note: a separate, pre-existing issue in `app.py` is that the Twilio template
> and filenames are interpolated directly into an `os.popen` shell string, which
> is a **shell-injection risk**. This should be fixed (use
> `subprocess.run([...])` with an argument list) regardless of where it's hosted.

---

## 3. Recommended option — a persistent host (no code changes)

The lowest-effort path that runs the app **exactly as written** is any host that
gives you a **persistent, always-on process**:

- **Railway** or **Render** — closest to "push and it runs a Flask app," have
  free tiers, and need no application changes. **This is the recommendation.**
- **Fly.io** — a bit more setup (Dockerfile), generous and fast.
- A small **VPS** (DigitalOcean / Hetzner) running `gunicorn` — most control,
  most manual work.

**Deploy flow (Render / Railway):**

1. Add `gunicorn` to `requirements.txt`.
2. Set the start command to `gunicorn app:app` (or a `Procfile`:
   `web: gunicorn app:app`).
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and
   `TWILIO_FROM_NUMBER` (or `TWILIO_MESSAGING_SERVICE_SID`) as **environment
   variables** in the dashboard. **Do not commit `.env`.**
4. Deploy.

**Caveat:** persistent-container filesystems on these platforms are usually
**ephemeral** (reset on redeploy/restart). `results.csv` / `campaign.log`
survive a run but should not be treated as durable storage. For a one-time
campaign this is fine; for a repeatedly-used tool, move results to a database or
object storage.

---

## 4. If Vercel (serverless) is a hard requirement — the Supabase re-architecture

Vercel *can* host the UI, but only if we stop running the campaign as one long
process and instead use the **queue + worker draining slices** pattern. Supabase
supplies the missing pieces (durable state, a scheduler, secrets).

```
┌─────────────┐   upload CSV    ┌──────────────────────┐
│  Vercel     │ ──────────────► │ Supabase             │
│  static     │                 │  • Postgres (queue)  │
│  frontend   │ ◄────────────── │  • Storage (CSVs)    │
└─────────────┘   live status   │  • Edge Functions    │
                                 │  • pg_cron scheduler │
                                 └──────────────────────┘
                                            │  HTTP POST
                                            ▼
                                     ┌──────────────┐
                                     │  Twilio API  │
                                     └──────────────┘
```

**Flow:**

1. **Frontend (static, on Vercel)** uploads the CSV. An Edge Function
   (`enqueue`) parses it, normalizes + dedupes phone numbers, and inserts rows
   into a `messages` table with `status = 'queued'`.
2. **pg_cron** (built into Supabase) invokes a `send-batch` Edge Function on a
   schedule (e.g. every minute).
3. Each `send-batch` run **claims a small slice** of `queued` rows — as many as
   safely fit in one invocation — POSTs them to Twilio, and updates each row to
   `sent` / `failed`. Rate limiting and batching become **"N per tick, every
   minute"** instead of `sleep()` inside a loop.
4. The frontend reads live status directly from Postgres (Supabase REST /
   realtime). `results.csv` is replaced by the `messages` table.

**Why this fixes the Vercel problems:**

- **Timeouts** — no single invocation runs the whole campaign; it drains over
  many cron ticks. (Edge Functions also have per-invocation CPU/wall-clock
  limits, which is exactly why the slice-per-tick design is required — do **not**
  loop-and-sleep inside one function.)
- **Persistence** — state lives in Postgres, not a wiped filesystem. Resumable
  by design.
- **Dedup** — a `UNIQUE` constraint on `phone` replaces the script's in-memory
  dedup.

**What it costs:**

1. **A language rewrite.** Edge Functions run on **Deno (TypeScript), not
   Python.** `send_campaign.py` must be ported. The Twilio send is just an HTTP
   POST to the REST API, but the CSV parsing, `normalize_canadian_phone`,
   retry/backoff, and dedup logic all need reimplementing in TypeScript.
2. **Restructuring from a loop to a queue.** Batch-pause / rate-limit logic moves
   out of the script and into the cron cadence plus a "claim N rows" query.
3. **Secrets & CORS.** Twilio credentials go in Edge Function secrets
   (`supabase secrets set`), never near the frontend. The function needs CORS
   headers so the Vercel page can call it.

---

## 5. Decision summary

| Goal | Recommended path |
|---|---|
| Get **this one-time campaign** out with the code we already have | **Render / Railway** (Section 3) — least effort, no rewrite. |
| A reusable, hosted, self-service tool for staff, no server to manage | **Vercel + Supabase** (Section 4) — more work, fully serverless & durable. |
| Must be on Vercel, minimal rebuild | Not possible without the Section 4 re-architecture. |

**Overall recommendation:** unless a fully-serverless, self-service tool is the
explicit goal, deploy the existing app to **Render or Railway**. Reserve the
Supabase rebuild for when the app needs to become a durable, multi-use product.
