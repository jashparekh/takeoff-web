# Deploying Takeoff on DigitalOcean App Platform

Everything here is done by Jash in the DigitalOcean **dashboard**, on his **personal** account. Do not use `doctl`,
and do not use the LoopRL team (or the DigitalOcean MCP connected to it).

The app is described by `.do/app.yaml`:
- **Database:** a managed Postgres 16 cluster.
- **Migration job:** `migrate`, a pre-deploy job that runs `alembic upgrade head` before each release.
- **Web service:** `web`. Its health check (`/api/health`) also checks the database.

Every push to `main` deploys.

## First deploy

1. **Create the database.** Databases → Create → PostgreSQL **16**, region **Bangalore (BLR1)**, the smallest
   size, name it **`takeoff-db`**. In the cluster's *Users & Databases* tab add a database **`takeoff`** and a
   user **`takeoff`**. The spec looks for these exact names.
2. **Create the app.** Apps → Create App → choose GitHub `jashparekh/takeoff-web` (branch `main`), then on the
   review screen use *Edit spec* / upload `.do/app.yaml`. Check that:
   - the database component points at `takeoff-db`;
   - the app is added to the cluster's *Trusted sources* (the dashboard offers this; without it the migrate job
     can't connect and the deploy fails).
3. **Optional settings.** Set `SIGNUP_ALLOWLIST` under App → Settings → `web` → Environment variables, if people
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
