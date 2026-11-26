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
- `app/api/uploads.py` — `POST /api/projects/{pid}/uploads` (editor; multipart `files`; checks size and `%PDF-` only,
  never opens the PDF; same SHA-256 → existing upload), `GET …/uploads`, `GET …/sheets`, `GET …/sheets/{id}/thumb.png|
  display.png` (302 to a 10-minute storage link), `PATCH …/sheets/{id}` (editor: `sheet_type`, `floor_id`; marks the sheet
  confirmed and writes `corrections`), `GET /api/sheet-types`.
- `app/pipeline/` — worker (`arq app.pipeline.jobs.WorkerSettings`). `stages.py`: `ingest` (pages → `sheets`, a page
  already in the project by render fingerprint is skipped) → `render` (300 dpi viewer PNG, thumbnail, 300 dpi grayscale
  for OCR) and `classify` (`classify.py`: title block from the PDF text layer → drawing no., title, revision, scale,
  date; sheet type from title keywords; floor by matching "SECOND FLOOR" / "2ND FLR" / "SF" to the project's floor
  names). No title block in the text layer (scans, text as outlines) → Tesseract OCR of the corner (`ocr.py`,
  `text_source='ocr'`). Unclear type or floor → `needs_review` with a plain note; a `confirmed` sheet is never re-read.
  Then `calibrate` (`calibrate.py`, layouts/slabs/columns/footings only): OCR the feet-inch dimension strings, measure
  each dimension line, and take the largest group agreeing within 2% (≥ 2 needed) as `pt_per_in`. Statuses: `ok`
  (matches the printed scale), `differs` (pilot layout: printed 1:100, measures 1:119 — printed to fit A3), `borrowed`
  (no usable dimensions; scale of a measured sheet with the same printed scale and paper size), `unchecked`,
  `confirmed` (a person set it via `PATCH … {scale_ratio}` or `{accept_scale}`). Only `ok`/`confirmed` are settled.
  Then `layout` (beam layouts with a scale; `geometry.py` + `layout.py`, M2): vectors via pypdfium2 (stroke widths
  scaled by each object's matrix); beam edges = the stroke width whose lines pair up most at 4.5–15" spacing (0.84 pt
  on the pilot, found not hard-coded); columns = small rectangles drawn heavier than edges; labels = OCR of a render
  holding only the lettering strokes, at 200/250/300 dpi, upright and turned (89 of 91 pilot labels). A label goes to
  the nearest parallel strip; spans end at columns and the line's ends. Beams that frame in (T) or cross (X) do not end a
  span (the pilot's spans run through them) except where two labels share a line (split at the crossings that put
  each label nearest the middle of its piece) or past the last crossing beam with no column and no label for 12"–110"
  (an overhang: the span stops at that root beam). Between two labels a crossing beam is preferred to one framing in.
  Labels: different names read on the same spot are settled to one (more reads, then a name not read more clearly
  elsewhere, then the longer name — B85 not B5); a label read once whose name is read clearly elsewhere is doubtful
  (confidence ≤ 0.3, noted) and takes no span on a line with clear labels. Results in `members` (sheet points); people's decisions
  (status ≠ auto) survive re-reads. Cantilevers: a beam's line followed outward past its root (across a column gap) to where it
  runs out with no further column and no label placed on that line → `B7-cant` / `-cantL` / `-cantR` (confidence
  ≤ 0.5 so Review shows them early). Once the floor's schedule is read it settles them (`stages.schedule_cants` →
  `read_layout(cants=)`; the layout job key holds a hash of it and the schedule job re-queues the floor's layouts): an
  overhang the schedule has no part for is joined into its span if it starts at a crossing beam, is ≥ 2 ft and its
  tip sits on a beam passing across or a column (B5), else dropped. Pilot: 19 of the schedule's 21 cantilever beams,
  none extra (B21 and B88 are not drawn as overhangs). A change to specs in Review does not re-read the layout yet. Floor plate: every beam-edge segment at any angle within 6 ft of the
  frame, dash gaps closed, filled, traced (OpenCV) → `slab_panel` member with `points` (5,355 sq ft incl. the curved
  corner the pilot missed). `python -m tools.golden_report` compares with the pilot (83 of 88 beams within 4 ft,
  median 0.1"; 25/25 columns; spans 74 of 87 equal the workbook or the drawn 4–5" over it; the 7 short and 4 long are
  face-to-face spans where the workbook took the pilot's c/c − 9").
- `app/pipeline/schedule.py` — beam schedules (M3, OCR pass): names/dims and bar callouts are drawn in two lettering
  widths (0.48 / 0.6 pt on the pilot), each rendered alone and OCR'd (callouts: 4 passes, voted); elevations = the
  outline width whose rows sit over the most labels, split at supports; size-only labels next to a beam are its
  cantilever parts. Roles by position: top callout not under a bracket → Top through, under an EQ/length bracket or at a
  support → Extra top @ support (bracket = length), bottom left→right → Bottom through then Bottom extra, stirrup row →
  dia, end and mid spacing. Stored in `beam_specs` + `bars` (source ocr, confidence = completeness); repeats on another
  sheet are kept with `duplicate_of` and raise one Issue (pilot: B68–B73). Pilot baseline (`golden_report
  --schedules`): 105 of 115 elevations, size 103, stirrups 69, bars 28, fully right 16 (14%) — OCR misses callouts
  (73 of 77 bar errors); RapidOCR measured worse (8 vs 35). The vision pass (M3 PR 2) is meant to close the gap.
- `app/pipeline/vision.py` + `reconcile.py` — vision pass (M3, D5): elevations whose OCR reading is under
  `VISION_BELOW_CONFIDENCE` are cropped (300 dpi, all strokes) and read by `VISION_MODEL` (Sonnet 5.5) via
  `client.messages.parse` into `ElevationReading`; refusals, errors and fields that disagree go to
  `VISION_ESCALATE_MODEL` (Opus 5.5). Reconcile per field: agree / model_only / ocr_only / resolved / disagree /
  missing — all readings and statuses kept on the spec and shown in the sheet viewer; never picked silently.
  `model_calls` caches by (crop sha, model, `PROMPT_VERSION`) with tokens, cost, latency; an elevation tried once is
  not sent again (failures too). **Off unless `VISION_ENABLED=true` and an Anthropic key are set** — it sends drawing
  crops to the API. `tools/record_vision.py --send-crops` records pilot answers to `tests/fixtures/vision/`;
  `golden_report --vision-replay` and CI replay them, never calling the API. No recordings yet (Nov 2026).
- Engine from the drawings (M3): `inputs_builder.beams_from_drawings` builds `beams[]` from layout members (clear span)
  + schedule specs (size, kind, stirrups, bars) when a floor has members; **every member and spec used must be checked
  in Review** (accepted / edited / added), else `NotReady` lists what is left (the API's `not_ready`). Chajja and slab
  still come from `floors.structure_json` until slab sheets are read (M5). Clear span = the **drawn dimension** when one is read
  (`dimensions.py`: dimension text OCR'd from the lettering render with lines removed, paired with its dimension line,
  kept only if the line length agrees allowing for the line stopping short of the faces — 4" each end on the pilot —
  then given to the parallel beams on both sides of the panel; `clear_source='dimension'`), else centre-to-centre less
  half the support where the span ends on a support's centre line (`layout.clear_span_in`, `'layout'`). A drawn span
  that disagrees with the measured one by > 3" lowers confidence and is noted. Pilot: 85 dimensions, 49 beams spanned
  from them, 47 agree with the geometry within 1". Drawn dimensions count (Jash, Nov 2026); the pilot workbook is
  4–5" under the drawn span on 16 beams (B6, B12, B14…; no rule found) and counts some beams through a support the
  layout splits (B13, B24…), so only ~20% of spans are within 1" of it. Review takes a typed span (`clear_in`, ft-in). `services/checks.py`: size conflicts (B5), layout beams without a
  schedule reading and vice versa, layout cantilevers the schedule doesn't have → Issues (rule `check_*`), re-run
  after stages and Review edits. Review also lists schedule readings with a bar editor (`PATCH …/specs/{id}`,
  `POST …/sheets/{id}/specs`). `tests/test_acceptance.py`: upload → review via the API → inputs equal `inputs.json`
  and 7,744 kg (review effort printed: mostly spans and bars).
- `app/services/model_builder.py` — the 3D model from `members` (same shape as `model.json` plus `b_in`, `D_in`,
  `floors`, `level`, `floor_h`); `GET /api/projects/{pid}` serves it, falling back to the pilot's `model.json`. The 3D
  view opens without a calculation (element colours; sizes from labels; slab drawn 5″ for the picture only).
  Bump `CLASSIFY_VERSION` / `CALIBRATE_VERSION` / `LAYOUT_VERSION` when a reader changes. Tesseract is needed locally (`brew install
  tesseract`) and is in the Docker image; OCR tests skip without it. Every stage writes a `jobs` row keyed `stage:subject:input_hash` and does nothing if it already ran.
  `pdf.py` uses **pypdfium2** (D1: chosen over PyMuPDF for the licence) and holds a lock around every pdfium call —
  pdfium crashes the process if two threads use it. Retries 3×; a cron re-queues stuck work every minute.
- `app/api/review.py` — Review (M2): `GET /api/projects/{pid}/review` (members still `auto`, least confident first,
  plus counts), `PATCH …/members/{id}` (editor: `action` accept / reject / reopen; `name`, `label_b_in`, `label_d_in`;
  `x1,y1,x2,y2` redraw, snapped to horizontal or vertical), `POST …/sheets/{id}/members` (add a missed beam). Every
  change writes `corrections` (now with `member_id`). Frontend `#p/<slug>/review[/<member id>]`: keys A accept,
  E edit, R redraw (click both ends on the sheet), X not on drawing, N / P next / back.
- `app/storage.py` — S3 API (Spaces / S3Mock); keys `p/{project}/u/{upload}/original.pdf`, `p/{project}/s/{sheet}/…`.
- `app/data/<project>/` — `project.json` (meta, sheets, per-sheet beam hotspots in PDF points),
  `inputs.json` (assumptions, beams, bars, chajja, slab openings), `issues.json`, `notebook.json` (site QS
  takeoff), `model.json` (3D: beam centrelines, columns, slab outline, metres), `drawings/*.svg` (sheets rendered
  from PDF with text as paths), `source/*.pdf` (consultant originals).
- `web/` — no-build frontend (ES modules, no framework). `js/main.js` (router + all views), `js/viewer.js`
  (pan/zoom SVG sheet viewer with hotspots), `js/beamdraw.js` (generated beam elevation + sections),
  `js/model3d.js` (three.js 3D view, lazy-loaded), `app.css` (tokens; light + dark), `vendor/` three.js r160
  (MIT; imports rewritten to relative paths).
- `tools/import_workbook.py` — imports the pilot workbook inputs. `tools/extract_model.py` — the pilot's one-off
  `model.json` builder (PyMuPDF, hand-placed labels); superseded by `app/pipeline/layout.py`, kept as the golden source.
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
`assumptions`, `upload[/<sheet id>]`, `review[/<member id>]`. Old links without `#p/<slug>/` open the Work Lounge pilot. In `web/js/main.js`, build
in-project links with the `P` prefix (`href="${P}beams"`). A project with no measured floor shows only Overview
(buildings and floors) and Add drawings.

## Run / test
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000     # http://localhost:8000
python -m pytest -q tests
```
Database: `docker compose up -d` (Postgres, Redis, S3Mock), `cp .env.example .env`, `alembic upgrade head`,
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
1. Deploy on DigitalOcean App Platform from `.do/app.yaml` (region blr, Dockerfile, managed Postgres `takeoff-db`,
   pre-deploy `alembic upgrade head`, deploy on push to main). Jash does this in the dashboard: `docs/deploy.md`.
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
