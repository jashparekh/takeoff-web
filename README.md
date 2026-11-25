# Takeoff web

Web app for checking structural drawings and the steel, concrete and cement taken off them. Every number shows the formula and the working behind it.

Pilot project loaded: **Work Lounge, Hubballi**, second floor slab + beams (Manohar Consultants, R0 16.09.2026).

## Views
- **Add drawings**: upload PDFs; each page becomes a sheet, drawn at 300 dpi and viewable as soon as the worker finishes. The same file or page is never processed twice. Each sheet's title block is read to find its type and floor; the **Coverage** grid shows floors × drawings (beam layout, beam schedule, slab bottom and top bars) and what is missing, and any sheet can be corrected in the viewer.
- **Sign in / Projects / New project**: email sign-in link, the projects you've been added to, and a form to start a project (buildings → floors) before its drawings arrive.
- **3D model**: the measured floor's beams, slab and columns in 3D (zoom, orbit, pan, top/below presets), other floors ghosted. Click any member to see its quantities and the working; colour by element, steel per metre, site check or issues.
- **Overview**: steel by bar size, concrete and cement, cost per sq ft, open questions.
- **Drawings**: the consultant's sheets at full resolution with every beam clickable, coloured by steel per metre, site-check difference or issues.
- **Beams / beam detail**: generated elevation and sections, bar bending schedule; expand any bar to see its formula with numbers filled in.
- **Slab**: measurement steps.
- **Issues**: what the drawings don't say or say twice, the value used meanwhile, and a copyable question list for the consultant.
- **Site check**: Takeoff against the site QS's handwritten notebook, entry by entry.
- **Assumptions**: change any assumed value or rate and everything recalculates.

## Run locally
```
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
python -m pytest -q tests
```

### Database and sign-in
Projects are read from Postgres and every page needs sign-in:
```
docker compose up -d                         # Postgres 16, Redis 7, S3Mock (stands in for Spaces)
cp .env.example .env                         # local values only
alembic upgrade head
python -m tools.import_pilot worklounge      # --replace to re-import
arq app.pipeline.jobs.WorkerSettings         # in a second terminal: reads and draws uploaded PDFs
createdb -h localhost -U takeoff takeoff_test
TEST_DATABASE_URL=postgresql+psycopg://takeoff:takeoff@localhost:5432/takeoff_test python -m pytest -q tests
```
Grant yourself access with `python -m tools.users add you@example.com --project worklounge --role owner`, open
http://localhost:8000 and ask for a sign-in link. With `MAIL_BACKEND=console` (the default) the link is printed in
the server log instead of emailed; `COOKIE_SECURE=false` (in `.env.example`) is needed for local http.

DB tests are skipped when `TEST_DATABASE_URL` is not set; CI always runs them. After changing `app/db/models.py`,
add a migration with `alembic revision --autogenerate -m "..."` (a test fails if models and migrations differ).

## Layout
- `app/engine.py`: quantity engine (explained steps).
- `app/main.py`: FastAPI app + static frontend. `app/api/`: projects and uploads API (database-backed, signed-in only).
- `app/pipeline/`: the background worker (arq): `stages.py` (ingest → render, idempotent), `pdf.py` (pypdfium2), `jobs.py`.
- `app/storage.py`: S3 API storage (Spaces in production, S3Mock locally); private bucket, 10-minute links.
- `app/data/<project>/`: `project.json` (sheets, hotspots), `inputs.json` (beams, bars, assumptions), `issues.json`, `notebook.json`, `drawings/*.svg`.
- `web/`: no-build frontend (ES modules).
- `tools/import_workbook.py`: imports the pilot workbook inputs.
- `tools/extract_model.py`: builds `model.json` (beam centrelines, columns, slab outline) from the beam layout PDF vectors (needs pymupdf, scipy, opencv).
- `web/vendor/`: three.js r160 (MIT) with OrbitControls.

## Deploy
DigitalOcean App Platform, spec in `.do/app.yaml`: Dockerfile build, managed Postgres, migrations before each release,
deploys on push to `main`. Steps: `docs/deploy.md`.
