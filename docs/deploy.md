# Deploying Takeoff on DigitalOcean App Platform

Everything here is done by Jash in the DigitalOcean **dashboard**, on his **personal** account. Do not use `doctl`,
and do not use the LoopRL team (or the DigitalOcean MCP connected to it).

The app is described by `.do/app.yaml`:
- **Database:** a managed Postgres 16 cluster.
- **Migration job:** `migrate`, a pre-deploy job that runs `alembic upgrade head` before each release.
- **Web service:** `web`. Its health check (`/api/health`) also checks the database.
- **Queue:** a managed Valkey (Redis-compatible) cluster.
- **Files:** a private Spaces bucket.
- **Worker:** `worker`, which reads and draws uploaded PDFs (2 GB instance).

Every push to `main` deploys.

## First deploy

1. **Create the database.** Databases → Create → PostgreSQL **16**, region **Bangalore (BLR1)**, the smallest
   size, name it **`takeoff-db`**. In the cluster's *Users & Databases* tab add a database **`takeoff`** and a
   user **`takeoff`**. The spec looks for these exact names.
   Also create:
   - **Queue:** Databases → Create → **Valkey**, region BLR1, the smallest size, named **`takeoff-queue`**.
   - **Files:** Spaces Object Storage → Create bucket, region BLR1, named **`takeoff-files`**. Leave file listing
     **restricted** (private). No CORS rule is needed, because the app shows images through short-lived links.
   - **Access key:** Spaces Object Storage → Access keys → create a key with access to `takeoff-files` only.
     Keep the secret for step 3.
2. **Create the app.** Apps → Create App → choose GitHub `jashparekh/takeoff-web` (branch `main`), then on the
   review screen use *Edit spec* / upload `.do/app.yaml`. Check that:
   - the database components point at `takeoff-db` and `takeoff-queue`;
   - the app is added to both clusters' *Trusted sources* (the dashboard offers this; without it the migrate job
     can't connect and the deploy fails).
3. **Settings.** Under App → Settings, on **both** `web` and `worker`, add `S3_ACCESS_KEY` and `S3_SECRET_KEY`
   (the Spaces key from step 1) as **encrypted** variables. Optionally, set `SIGNUP_ALLOWLIST` on `web`, if people
   without an account should be able to sign up (for example `@yourfirm.in`). People added with `tools.users`
   can always sign in.
4. **Deploy.** The `migrate` job creates the tables, then `web` starts. The deploy only goes live once
   `/api/health` answers `{"ok": true, "db": "ok"}`.
5. **Load the pilot and add yourself.** App → Console → `web`:
   ```
   python -m tools.import_pilot worklounge
   python -m tools.users add you@example.com --name "Your name" --project worklounge --role owner
   ```
6. **Sign in.** Open the app URL, enter your email, then open App → Runtime Logs → `web`. The sign-in link is
   printed there, because `MAIL_BACKEND=console` (see below). Open it in the same browser and press **Sign in**.

## Email

Until an email provider is chosen (plan decision D6), sign-in links appear only in the runtime logs. Anyone who
can read the app's logs can use them, so keep the account's team small.

To send real email, pick a provider that speaks SMTP (Postmark, Resend or SES) and verify a sending domain. Then
set these on the `web` component:

| Variable | Value |
|---|---|
| `MAIL_BACKEND` | `smtp` |
| `MAIL_FROM` | for example `Takeoff <no-reply@yourdomain>` |
| `SMTP_HOST` | the provider's SMTP host |
| `SMTP_PORT` | 587 (STARTTLS) |
| `SMTP_USER` | **encrypted** variable |
| `SMTP_PASSWORD` | **encrypted** variable |

Never put the SMTP username or password in `.do/app.yaml`.

## Vision model (schedules)

Off by default. When switched on, the worker sends crops of schedule elevations that OCR could not fully read to the
Anthropic API (billed per call; cached so a crop is never read twice). To switch on, on the `worker` component set
`VISION_ENABLED=true` and add `ANTHROPIC_API_KEY` as an **encrypted** variable. `VISION_MAX_CALLS_PER_SHEET`
(default 150) caps calls per sheet. Every call's tokens and cost are in the `model_calls` table.

## Later deploys

- **Push to `main`.** The `migrate` job runs first. If a migration fails, the old version keeps serving.
- **Pilot data.** After changing the pilot's files in `app/data/worklounge`, re-run
  `python -m tools.import_pilot worklounge --replace` in the console. This also removes the pilot's members, so
  re-run the `tools.users grant` lines afterwards.
- **Migrations must work against the old code.** The old version keeps serving while the job runs, so avoid
  dropping or renaming a column the running version still reads in the same release.

## Custom domain

Add the domain under App → Settings → Domains. `APP_URL` is bound to `${APP_URL}`, the app's live URL, so
sign-in links follow the domain. Sessions are cookies on that host, so everyone signs in again after the switch.
