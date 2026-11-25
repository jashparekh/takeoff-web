# CLAUDE.md — Takeoff web

Context for Claude working in this repo. Read this first.

## What Takeoff is
Takeoff reads structural (and architectural) drawing PDFs of Indian RCC buildings and produces steel, concrete
and cement quantities, a bar bending schedule (BBS) and cost per sq ft. Every number must show **how** it was
worked out (formula, then the formula with numbers filled in).

Owner: Jash Parekh (runs a construction business; developer). First users: Jash and his engineers, so full
technical detail is wanted. Later users: site QS / contractors and owners.

Product rules (non-negotiable):
- **Never assume silently.** Values the drawings don't give (cover, Ld, concrete grade, cement content…) are
  surfaced as Issues / Assumptions with the value used meanwhile. In the upload flow nothing is calculated
  until every missing value is entered and confirmed.
- Plain words in the UI ("Values we need", "Site check"), not system jargon.
- Quantities follow the **drawn** bar lengths, not site rules of thumb.

## Repo layout
- `app/engine.py` — quantity engine. `Calc(inputs, overrides).run()` returns summary, per-beam bars with
  `steps` (label, formula, working, value, unit), slab steps, chajja (CB1) and members (cantilever parts merged).
- `app/main.py` — FastAPI app, middleware, static frontend at `/static`, `/` serves `web/index.html`.
- `app/api/projects.py` — projects from the database, all behind sign-in: `GET /api/projects` (the user's projects,
  totals from the latest saved run), `POST /api/projects` (name, client, city, buildings → floors; creator is owner),
  `GET /api/projects/{pid}` (meta, role, ready, buildings, assumptions, issues, model, beams_input),
  `POST /api/projects/{pid}/calculate {overrides}` (engine + notebook comparison + auto issues; 409 until a floor is
  measured), `GET /drawings/{pid}/{name}.svg`. Drawings and `model.json` are still read from `app/data/<slug>/`
  until uploads store them (M1); `model.json` is cached — restart after changing it.
- `app/api/compare.py` — notebook comparison and automatic issues.
- `app/data/<project>/` — `project.json` (meta, sheets, per-sheet beam hotspots in PDF points),
  `inputs.json` (assumptions, beams, bars, chajja, slab openings), `issues.json`, `notebook.json` (site QS
  takeoff), `model.json` (3D: beam centrelines, columns, slab outline, metres), `drawings/*.svg` (sheets rendered
  from PDF with text as paths), `source/*.pdf` (consultant originals).
- `web/` — no-build frontend (ES modules, no framework). `js/main.js` (router + all views), `js/viewer.js`
  (pan/zoom SVG sheet viewer with hotspots), `js/beamdraw.js` (generated beam elevation + sections),
  `js/model3d.js` (three.js 3D view, lazy-loaded), `app.css` (tokens; light + dark), `vendor/` three.js r160
  (MIT; imports rewritten to relative paths).
- `tools/import_workbook.py` — imports the pilot workbook inputs. `tools/extract_model.py` — builds
  `model.json` from the beam layout PDF vectors (needs pymupdf, scipy, opencv-python, pillow).
- `docs/` — pilot workbook and the B47 explainer PDF.
- `tests/test_engine.py` — pins pilot totals; keep green.
- Database (upload pipeline M0, in progress): `app/settings.py` (env / `.env`), `app/db/models.py` (SQLAlchemy),
  `app/db/migrations/` (Alembic; `alembic upgrade head`), `app/services/inputs_builder.py` (DB rows → the engine's
  `inputs` dict, so the engine stays unchanged), `tools/import_pilot.py` (loads `app/data/<slug>` into the DB).
  Until members/bars tables exist (M2–M3), a floor's beams/chajja/slab sit in `floors.structure_json`.
  Re-run `python -m tools.import_pilot worklounge --replace` after changing the pilot's data files. Plan: `docs/plans/upload-pipeline-implementation.md`.
- Auth (M0): `app/auth/` — magic link (`POST /api/auth/link` → email → `GET /auth/callback` page → `POST
  /auth/verify` sets the `takeoff_session` cookie), DB-backed sessions (only token hashes stored), `GET /api/me`,
  `POST /api/auth/logout`. Use `Depends(project_role('viewer'|'editor'|'owner'))` on every `/api/projects/{pid}/…`
  route that reads the DB; non-members get 404. Beta is invite-only (`SIGNUP_ALLOWLIST`, `tools/users.py`).
  Cross-site writes are refused by an Origin check in `app/main.py`. Email: `MAIL_BACKEND=console|smtp`.

Hash routes: `#signin[/expired]`, `#projects`, `#new`, and per project `#p/<slug>/<view>` where view is `overview`,
`model[/B47]`, `drawings/<sheet>[/<beam>|/@x,y,zoom]`, `beams`, `beam/<name>`, `slab`, `issues`, `notebook`,
`assumptions`, `upload`. Old links without `#p/<slug>/` open the Work Lounge pilot. In `web/js/main.js`, build
in-project links with the `P` prefix (`href="${P}beams"`). A project with no measured floor shows only Overview
(buildings and floors) and Add drawings.

## Run / test
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000     # http://localhost:8000
python -m pytest -q tests
```
Database: `docker compose up -d db`, `cp .env.example .env`, `alembic upgrade head`,
`python -m tools.import_pilot worklounge`. DB tests need `TEST_DATABASE_URL` (skipped otherwise; CI sets it).
Docker: `docker build -t takeoff-web . && docker run -p 8000:8080 takeoff-web` (listens on `$PORT`, default 8080).

## Engine conventions (pilot 2 method)
Inputs in inches as drawn; outputs mm / kg / m³. Bar weight = d²/162 kg/m.
- Top through (span): `L + 2·Ld`, Ld = k·d (k = `ld_factor`, 50).  Cantilever top main: `2L + b + 12d`.
- Bottom through (span): `L + 2 × anchorage` (190 mm). Cantilever bottom: `L + b + 12d`.
- Bottom extra: `α·L` (α = 0.8).  Extra top @ support: drawn length (or L/3 if not drawn) `+ b/2`.
- Stirrups: nos = `2⌈(L/3)/s_end⌉ + ⌈(L/3)/s_mid⌉ + 1` (cantilever `⌈L/s⌉+1`);
  cut = `2(b−2c) + 2(D−2c) + 2·10d − 3·2d` (135° hooks).
- Beam concrete below slab: `b·(D − slab t)·L`. Slab: net area (footprint − openings − beam plan area) × t;
  bottom mesh 8φ both ways at average spacing × (1 + allowance); top bars over span beams; distribution bars.
- Totals: steel × (1 + 3% wastage); concrete × (1 + 2%); cement = concrete × 350 kg/m³ / 50.
- Rates: Hubli TMT ₹/kg per dia + ₹2 Fe550 premium; RMC M25 ₹5,400/m³ (indicative, Aug 2026).

## Pilot project: Work Lounge, Hubballi (`worklounge`)
Second floor slab + beams, Manohar Consultants, drawings MC-SD-07 (shuttering/beam layout), MC-SD-06-01/02
(beam schedules), MC-SD-06-04/05 (bottom/top slab), R0 16.09.2026. Fe550; grade not stated (M25 assumed).
Pinned results (tests): beams 7,744 kg (incl. CB1 chajja 10 × 54″ = 301 kg), slab 3,894 kg,
11,987 kg with wastage, concrete 83.72 m³ (slab 53.91 + beams 28.17, +2%), B47 31.50 kg (18 stirrups;
bars 4,477 / 3,657 / 2,621 mm). Floor area basis 5,650 sq ft.

Drawing issues on record: concrete grade not stated; cover and lap/Ld not stated; B5 9×24 on layout vs 9×18
in schedule; B42/B46/B51/B89 numbers missing; schedule sheet 2 repeats B68–B73; one panel marked 6″.

Site check: the site QS's handwritten notebook totals 9,382 kg of beam steel vs Takeoff 7,744 kg (+1,638 kg;
~+1,940 like-for-like since the QS did not count CB1). Causes: QS runs extra bars full length, uses overall beam
length (over columns) for every bar, and stirrup counts differ (often ~6″ spacing all along vs drawn 8″/10″).
Page 1 of the notebook was recorded only as group totals (page total 3,176 kg). Concrete gap 13.72 m³ is
unexplained until the QS's concrete page is received. Notebook readings flagged as uncertain are in
`notebook.json` notes (B47 split of 26′ lines, B84, B80, B67, B90, B89→B93, B42→B40A).

3D model: 88 of 92 beams placed (B67, B81, B83 not matched), 25 columns (9″×24″), slab outline traced from the
layout (5,131 sq ft inside beam edges; misses the curved corner panel and the panel by B47). Floor height 3.0 m is
a display assumption; floors other than the second are ghost copies (only second floor drawings received).
Some cantilever stubs sit on the wrong end. About 20 of the 2D layout hotspots were placed by hand.

## Status and next steps
1. Deploy on DigitalOcean App Platform from `.do/app.yaml` (region blr, Dockerfile, deploy on push to main).
2. PDF upload pipeline (the `#upload` page describes it): store PDFs, render sheets to SVG, OCR beam labels and
   schedule callouts, cross-sheet checks → Issues, "values we need" gate, then calculate with explanations.
3. Other floors and column schedule → replace ghost floors, bring columns into scope.
4. Fix cantilever placement and the unmatched beams in `tools/extract_model.py`.

## Hard constraints
- **Do not use doctl, and do not use the LoopRL org/team in DigitalOcean.** The DigitalOcean MCP connected on
  Jash's Mac is logged into the LoopRL Team — do not use it for this project. Deploy only to Jash's personal
  account, and ask before creating anything.
- Never commit secrets. App signing keys for the mobile demos live in `~/git/takeoff-keys` (outside this repo).
- End commit messages with the attribution lines your environment specifies.

## Related
- `jashparekh/takeoff-demo` (private): clickable Android + iOS demo apps with sample data (Kotlin/Compose and
  SwiftUI). iOS TestFlight external beta and Android internal testing are live; the Android closed test was
  waiting on a Google Group CAPTCHA.
- Pilot 1 was Building 2, Panvel (typical floor); its workbook is not in this repo.
