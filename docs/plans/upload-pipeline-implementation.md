# Implementation plan: upload pipeline

Status: draft · Builds on: [`upload-pipeline.md`](upload-pipeline.md) (the *what* and *why*) · Last updated: 2026-11-25

This document is the *how*: the order of work, the files each step touches, the decisions to take first, and
what "done" means for each milestone. Section numbers like §4.5 refer to `upload-pipeline.md`.

---

## 0. Where the code is today (what the plan has to change)

| Area | Today | What blocks the upload pipeline |
|---|---|---|
| Data source | `app/main.py` `load(pid)` reads `app/data/<pid>/*.json`, cached with `lru_cache` | No database; cache never invalidates; projects are folders |
| Engine input | `Calc(inputs)` takes `{assumptions[], beams[], chajja{}, slab{}}`; beams `{name, b_in, D_in, clear_in, kind S/C, stirrup{dia,end_in,mid_in}, bars[{role,nos,dia,extra_top_in?}]}` | Fine as is. The DB must **assemble this exact dict** so the engine and its pinned tests stay untouched |
| Site check | `notebook_compare` hard-codes page 1 = `B1…B35 + HB29` | Pilot-specific; must come from the notebook data |
| Auto issues | `auto_issues` (L/3 extra top, notebook gaps) | Becomes part of the checks stage (§4.8) |
| Layout extraction | `tools/extract_model.py`: CLI script; hard-coded `PT_IN = 0.6`, beam edge stroke `0.84`, column stroke `2.04`, strip spacing 4.6–6.4 pt (9″ beams only), needs hand-placed hotspots from `project.json` | Needs to be a library function with parameters found per sheet, and to find labels itself |
| Schedule extraction | none (bars read by eye) | All new (§4.6) |
| Frontend | `web/js/main.js` has `const PID = 'worklounge'`; all views in one 453-line file; `#upload` is a static description | Needs a project selector, routes per project, four new views |
| Deploy | `.do/app.yaml`: one web service, `apps-s-1vcpu-0.5gb`, no DB, no worker | Add worker, Postgres, Redis, Spaces, secrets |
| Tests / CI | 3 engine tests; CI runs pytest + docker build | Add DB tests (Postgres service in CI), stage tests, golden metrics |

Guiding rule for the whole plan: **the pilot keeps working at every commit.** Work Lounge becomes the first
project in the database (imported from its JSON files), and `tests/test_engine.py` stays green throughout.

---

## 1. Decisions to take before M0 (blocking)

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | PDF library licence | **Decided (Nov 2026): pypdfium2.** Sheets are drawn as PNG (300 dpi); sharp vector zoom later via pdf.js in the browser. Was: **Check PyMuPDF's AGPL now.** For a hosted product either buy an Artifex commercial licence or switch rendering/vector reading to `pypdfium2` (Apache/BSD) + `pdfplumber`/`pdfminer.six` for vectors | PyMuPDF is used everywhere in §4.1–4.5; swapping later is expensive |
| D2 | Queue | **arq** (async, Redis) | FastAPI is async; arq has retries, job ids, cron; tiny |
| D3 | ORM / migrations | **SQLAlchemy 2.0 + Alembic + psycopg 3** | Standard; Alembic needed from the first table |
| D4 | OCR engine (**Tesseract in use since M1; RapidOCR measured worse on the pilot schedules, Nov 2026**) | **Tesseract 5** (`pytesseract`, `apt install tesseract-ocr`) as baseline; evaluate **RapidOCR (ONNX)** on the goldens in M2 | The pilot reached 72/93 with OCR alone; pick by measured recall, not by taste |
| D5 | Vision model | Claude via the Messages API, tool-use with a JSON schema for structured output; start with `claude-sonnet-5-5`, escalate disagreements to `claude-opus-5-5`. Measure on goldens in M3 | Open question 3; budget set after M3 numbers |
| D6 | Magic-link email sender | A transactional provider (Postmark / Resend / SES) on Jash's personal account | Needed in M0 for auth; needs a sending domain |
| D7 | Local dev stack | `docker-compose.yml` with Postgres 16, Redis 7, MinIO (S3-compatible) | Same code path as Spaces; no DO account needed to develop |
| D8 | Who uploads in beta (open question 1) | Jash's team only; invite-only sign-up | Lets M1–M3 skip abuse controls and per-plan limits |

**Infra on DigitalOcean** (Jash creates these on his **personal** account; no doctl, nothing in the LoopRL team):
Managed Postgres (smallest, blr), Managed Redis/Valkey (smallest, blr) — or Redis as an App Platform dev
component for beta — one private Spaces bucket in blr with a CORS rule for the app origin, and app-level secrets.

---

## 2. Target code layout

```
app/
  main.py                 # FastAPI app factory, mounts routers, static (slimmed down)
  settings.py             # pydantic-settings: DATABASE_URL, REDIS_URL, S3_*, SECRET_KEY, MODEL_API_KEY …
  engine.py               # unchanged (later: conventions as settings, §5 M4)
  db/
    base.py session.py    # engine/session, async or sync (sync is fine for v1)
    models.py             # SQLAlchemy models for §5 of the plan
    migrations/           # Alembic
  auth/                   # magic link, session cookie, current_user, project access deps
  storage.py              # put/get/presign for S3/Spaces/MinIO; key scheme below
  api/
    projects.py uploads.py sheets.py review.py assumptions.py runs.py legacy.py
  services/
    inputs_builder.py     # DB rows → engine `inputs` dict (the seam that keeps the engine untouched)
    site_check.py         # generalised notebook_compare
    diff.py               # run A vs run B
  pipeline/
    jobs.py               # arq WorkerSettings, stage dispatch, retries, job rows
    ingest.py render.py classify.py calibrate.py layout.py schedule.py slab.py checks.py model3d.py
    ocr.py vision.py      # wrappers with caching by crop hash
    geometry.py           # strips, cuts, enclosure fill (moved from tools/extract_model.py)
  data/worklounge/        # stays as the import source + golden fixture
tools/
  import_pilot.py         # load app/data/worklounge into the DB (replaces the file read path)
  extract_model.py        # thin CLI over app.pipeline.layout (kept for debugging)
  golden_report.py        # run pipeline on goldens, print stage metrics
tests/
  test_engine.py          # unchanged
  test_inputs_builder.py  # DB → inputs gives the same totals as inputs.json
  test_api_*.py  test_pipeline_*.py
  golden/worklounge/expected/*.json   # hand-verified members, bars, sheet types
web/js/
  main.js                 # router + shell; PID from route
  api.js                  # fetch wrapper with project id, 401 → sign-in
  views/projects.js upload.js coverage.js review.js values.js   # new views as modules
docker-compose.yml  .do/app.yaml (web + worker + db)  Dockerfile (tesseract, opencv deps)
```

**Storage key scheme** (all private, presigned GET ≤ 10 min):
`p/{project_id}/u/{upload_id}/original.pdf`, `p/{project_id}/s/{sheet_id}/sheet.svg.gz`, `…/thumb.png`,
`…/ocr300.png`, `…/crops/{sha256}.png`. Deleting a project deletes the `p/{project_id}/` prefix.

---

## 3. Milestones in detail

Each milestone ends with: CI green, deployed, pilot views unchanged, and the acceptance checks listed.

### M0 — Foundation: deploy, database, auth, project list (2–3 days)

1. **Settings and local stack.** `app/settings.py` (pydantic-settings), `docker-compose.yml` (postgres, redis,
   minio, web, worker), `.env.example` (no secrets committed). README "Run" section updated.
2. **Database.** Alembic init; first migration creates `users`, `projects`, `project_members`, `buildings`,
   `floors`, `assumptions`, `issues`, `runs`, `site_takeoffs`. Conventions: UUID primary keys, `created_at`
   on every table, JSONB for `*_json`, `status` as text enums with check constraints.
3. **Pilot import.** `tools/import_pilot.py` loads `app/data/worklounge/*` into the DB: project + one building +
   floors (second floor measured, others ghost), assumptions (with `source`, `confirm` flags), issues, notebook →
   `site_takeoffs`, and a first `runs` row. Members/bars/sheets tables arrive in M1–M2; until then the legacy
   path still reads `inputs.json` for beams.
4. **`services/inputs_builder.py`** — builds the engine dict from DB rows. Test: for Work Lounge, totals from the
   built dict equal the pinned totals (7,744 / 3,894 / 11,987 kg / 83.72 m³).
5. **Auth.** `POST /api/auth/link {email}` (invite list in beta) → email with a one-time token (hashed in DB,
   15 min expiry) → `GET /auth/callback?token=` sets a signed, HttpOnly, SameSite=Lax session cookie.
   Dependencies `current_user` and `project_role(min='viewer'|'editor'|'owner')` on every `/api/projects/{id}/…`.
6. **Keep legacy endpoints.** Move `get_project`, `calculate`, `drawing` into `api/legacy.py`; resolve `pid` to a
   project the user can see; Work Lounge keeps its file-backed drawings. Drop `lru_cache` for DB projects.
   Generalise `notebook_compare`: the page-1 beam set comes from the notebook's own groups, not `B1…B35`.
7. **Frontend.** `web/js/api.js`; route `#p/<pid>/<view>` (old routes redirect to `#p/worklounge/…`);
   Projects list (home) and Create project form (name, city, consultant, buildings → floors with level,
   typical × n, copies) per the `CreateProject` / `AddBuilding` mocks; sign-in page.
8. **Deploy.** `.do/app.yaml`: web service + `databases:` (Postgres) + env vars referencing secrets; a
   `PRE_DEPLOY` job running `alembic upgrade head`. Jash creates the app in his personal account from the
   dashboard; health check `/api/health` also checks DB.
9. **CI.** Add Postgres and Redis service containers; run migrations then tests.

**Acceptance:** signed-in user sees the project list with Work Lounge; every existing view looks the same;
a new empty project can be created; unauthenticated requests to `/api/projects/*` return 401; engine tests green.

### M1 — Upload, ingest, render, classify, Coverage (1 week)

1. **Migration:** `uploads`, `sheets`, `jobs`.
2. **Upload API.** `POST /api/projects/{id}/uploads` multipart (many files). Stream to storage while hashing;
   reject > 50 MB, non-PDF magic bytes, encrypted PDFs; per-project file-count limit. Duplicate upload (same
   SHA-256) returns the existing upload. Enqueue `ingest(upload_id)`. Response lists the created sheets.
   (Later, if uploads get large: presigned direct-to-Spaces PUT.)
3. **Worker** (`pipeline/jobs.py`): arq settings, one function per stage, each **idempotent** (keyed by
   `sheet_id, stage, input_hash`) and writing a `jobs` row (`queued/running/done/failed`, error text). Stage
   chain per sheet: `ingest → render → classify → calibrate` (M1), then `layout|schedule|slab` by sheet type
   (M2–M5). Retries 3× with backoff; a failed stage marks the sheet `needs_attention`, never blocks others.
4. **Ingest** (§4.1): split pages, per-page SHA-256 (de-dup across uploads → link, don't reprocess),
   `sheets` row per page.
5. **Render** (§4.2): SVG with text as paths, gzipped; 150 dpi thumbnail; 300 dpi grayscale for OCR; store the
   PDF→points transform. Memory: render one page at a time; worker instance ≥ 2 GB (`apps-s-1vcpu-2gb`).
6. **Classify** (§4.3): crop title block (try bottom-right, then right strip, then bottom strip), OCR; parse
   drawing no., title, revision, date, scale. Sheet type from a keyword table (`config/sheet_types.yaml`) plus
   features (count of elevation-like outlines → schedule). Floor from title words mapped to the project's
   floors (fuzzy: "SECOND FLOOR", "2ND FLR", "SF"). Low confidence → vision-model fallback on the title-block
   crop only (§9: crops, never whole sheets).
7. **Calibrate** (§4.4): candidates from printed scale × paper size and from member widths; confirm with ≥ 2
   dimension strings (OCR'd dims near dimension lines matched to measured lengths). Store `pt_per_in`,
   `scale_error`; > 2% → review item.
8. **APIs:** `GET /api/projects/{id}/sheets` (status, type, floor, revision, thumb URL), `PATCH /api/sheets/{id}`
   (type, floor, scale; writes `corrections`), `GET /api/sheets/{id}/svg` (presigned redirect).
   Server-sent events or 3 s polling for progress (polling is fine for beta).
9. **Frontend:** Upload view (drag-drop, per-file progress, per-sheet stage chips); sheet viewer works for any
   project (viewer.js takes a URL instead of a pilot file name); Coverage grid per building (floors × sheet
   types, missing cells with "upload for this part"; click a sheet to correct type/floor).

**Acceptance:** uploading the 5 Work Lounge PDFs on a fresh project gives 5 sheets, all rendered, viewable,
classified to the right type and to "Second floor"; `scale_error` < 2% on the layout; re-uploading the same PDF
creates no new work. Golden test `test_classify_worklounge` pins sheet types.

### M2 — Layout extraction, Review, 3D per floor (1–1.5 weeks)

1. **Migration:** `members`, `corrections`, `review_items` (or a view over low-confidence rows; a table is
   simpler for ordering and assignment).
2. **Move and generalise `extract_model.py` into `pipeline/geometry.py` + `pipeline/layout.py`:**
   - *Width classes:* histogram stroke widths of H/V segments; the beam-edge class is the one whose segments
     form most parallel pairs at member-width spacing (replaces hard-coded `0.84`).
   - *Strip spacing:* from calibration, accept widths 4.5″–15″ (`b_in × pt_per_in` ± tolerance) instead of
     4.6–6.4 pt; record `b_in` per strip.
   - *Columns:* thick closed quads/rects of plausible size (6″–36″) or filled/hatched rects (replaces `2.04`).
   - *Labels:* OCR at 250 and 500 dpi, rotations 0° and −90°, union and de-dup by position; regex
     `^(H?B|CB)\d+[A-Z]?` plus optional `\((\d+)"?\s*X\s*(\d+)"?\)`. Unread labels are fine — they become review items.
   - *Matching:* label → nearest parallel strip within 16 pt → cut interval best matching the schedule's clear
     span (± 25%). Before M3 there is no schedule: match by the label's size and the interval containing the
     label; re-run matching after schedule extraction.
   - *Cantilevers:* fix the wrong-end bug: choose the end where the strip continues past the last column/support
     and is free (no perpendicular strip), not by `room − L` alone.
   - *Slab outline and openings* as today (raster fill, `findContours`, `approxPolyDP`); also fill curved
     panels (bridge arcs before fill — the pilot missed the curved corner panel).
   - Output `members` rows (kind beam/column/slab_panel, geometry in PDF points + metres, label bbox as hotspot,
     confidence = f(OCR conf, match error, perpendicular distance)).
3. **Review queue API:** `GET /api/projects/{id}/review?cursor=` sorted by confidence then sheet;
   `PATCH /api/members/{id}` (accept, edit fields, move hotspot, "not on drawing"); `POST /api/members` to add
   one by drawing a box on the sheet. Every edit writes `corrections(entity, field, old, new, user, at)`.
4. **Hotspots and 3D from DB:** `GET /api/projects/{id}` returns `meta.sheets[].hotspots` and `model` built from
   `members`, same shapes the frontend uses today. Floors without drawings stay ghosted; typical × n and copies
   instanced in `model3d.js`.
5. **Frontend Review view:** left = sheet crop (viewer.js focused on `source_bbox`), right = editable form +
   confidence; keys A accept, E edit, N next, X not on drawing; progress bar "34 of 41 checked".
6. **Golden fixture:** `tests/golden/worklounge/expected/members.json` (92 beams + 25 columns, hand-verified
   centrelines from current `model.json` plus fixes). `tools/golden_report.py` prints labels found, members
   placed, mean centreline error.

**Acceptance:** on Work Lounge, ≥ 85 of 92 beams placed with no hand-placed hotspots, 25/25 columns, cantilevers
on the right end, outline includes the curved panel; every unplaced beam shows in Review. Metrics printed in CI
(not yet a gate).

### M3 — Schedule extraction with OCR + vision model (2 weeks)

1. **Migration:** `bars`, `stirrups`, `model_calls` (crop hash, model, prompt version, request/response JSON,
   tokens, cost, latency) for caching and cost tracking.
2. **Segmentation** (`schedule.py`): find elevation outlines (long horizontal rectangles with supports), name
   labels beneath (`B12`, `B12 (9"X18")`), and the section boxes; one crop per beam elevation incl. callouts.
   Repeated elevations across sheets (B68–B73) detected by name + identical callout tokens.
3. **OCR pass** on each crop → tokens with boxes: bar callouts (`2K12`, `3K16+1K12`, `K8@6"c/c`, `EQ`),
   lengths (`3'-0"`, `15'-10"`), size; normalise notation per project (`K`=TOR/TMT, `#`, `Y`, `T`, `φ`).
4. **Vision pass** (`vision.py`): crop + project notation + JSON schema → `{size, clear_span_in, top_through[],
   bottom_through[], bottom_extra[], extra_top[{support, nos, dia, length_in?}], stirrup{dia, end_in, mid_in},
   cantilever{side, length_in}?}`. Cached by `(crop_sha256, model, prompt_version)`. Only crops where OCR is
   incomplete or ambiguous go to the model (cost control, §11).
5. **Reconcile:** field-by-field agreement between OCR tokens and the model answer. Agree → confidence high;
   model-only → medium (review); disagree → low (review, both readings shown). Never pick silently.
6. **Cross-checks:** schedule span vs layout strip length (> 5% → review), size vs layout label (B5 case),
   layout member without schedule and vice versa. Re-run layout matching using schedule spans.
7. **Map to engine input:** `inputs_builder` emits `beams[]` from `members` + `bars` + `stirrups` (roles exactly
   `Top through | Top main | Bottom through | Bottom extra | Extra top @ support`, `kind` S/C, `extra_top_in`
   when drawn). Chajja (CB1) as its own kind.
8. **Review UI** gains a bar editor (rows of role/nos/dia/length) next to the elevation crop, with
   `beamdraw.js` rendering the *read* bars so the engineer compares drawing vs reading side by side.
9. **Measure before committing further** (risk §11): golden report on Work Lounge schedules: bars exactly right
   per beam before review, model calls per floor, cost per floor. Gate for continuing to M4 timeline:
   ≥ 80% beams fully right before review; if lower, spend the next week on prompts/OCR before moving on.

**Acceptance:** Work Lounge from upload → review gives beam inputs equal to `inputs.json` (after review) and the
pinned 7,744 kg; B68–B73 counted once with an Issue; B5 conflict raised.

### M4 — Checks → Issues, Values we need, Confirm, runs (1 week)

1. **Checks** (`checks.py`): rule registry, each rule `(project) -> [Issue]` with id, severity, members,
   `where` (sheet + bbox), assumption used, question to ask. Rules from §4.8, plus move `auto_issues` here.
   Issues are upserted by `(rule, key)` so re-runs don't duplicate and a resolved issue stays resolved.
2. **Values we need:** a registry of required assumptions with defaults *proposed* but `confirmed_at = null`
   (`concrete_grade`, `cover_mm`, `ld_factor`, `cement_kg_m3`, rates by city, wastage…). Values read from
   drawings (e.g. slab thickness note) arrive pre-filled with their source and still need a tick.
   `GET /values-needed`, `PUT /assumptions` (sets `confirmed_by/at`).
3. **Conventions as settings:** engine conventions (α = 0.8, 190 mm anchorage, 135° hooks, L/3 fallback) become
   project assumptions with today's values as defaults — engine reads them from `self.a`; pinned tests unchanged.
4. **Runs:** `POST /api/projects/{id}/runs` → 409 with the missing list while any required value or blocking
   review item is open; otherwise snapshot inputs + assumptions + engine version (git SHA) + results into
   `runs`. Legacy `calculate` with overrides stays as a "what-if" that is never stored.
5. **Results from DB:** all existing views read the latest run; Site check uses `site_takeoffs`.
6. **Frontend:** Values we need form (grouped, unit-aware, source shown), Confirm summary with "I have checked
   these" tick, Calculate button disabled until complete; Issues view reads DB issues with "question list for the
   consultant" export (copy / PDF).

**Acceptance:** a fresh Work Lounge upload, reviewed and confirmed, produces a run with the pinned totals; the
Calculate button stays disabled while any value is unconfirmed; the 6 recorded drawing issues all appear.

### M5 — Panel-by-panel slab, revisions and diff (1 week)

1. **Slab panels** (`slab.py`): panel polygons via the same enclosure method on slab sheets; callouts inside each
   panel (`K8@8"`, `K10@6"`, `6" THK`); top-bar extensions over beams. Engine gains a per-panel slab path; the
   average-spacing path stays as fallback and is shown as an assumption. Golden: per-panel total within ±3% of
   the averaged 3,894 kg (difference explained, not forced).
2. **Revisions:** an upload whose sheet matches an existing drawing number with a higher revision creates a new
   sheet version; stages re-run only for that sheet; members are matched old→new by name, then position.
3. **Diff** (`services/diff.py`): members added/removed/changed, bars changed, kg and ₹ deltas by dia and member;
   `GET /api/runs/{a}/diff/{b}`; UI on the `RevisionCompare` mock pattern.

### M6 — Columns, footings, multi-floor (2 weeks)

Column schedule and footing schedule sheets (tabular: OCR table detection + vision fallback), engine sections
for columns (main bars, ties, laps per floor) and footings; floor heights from project levels; typical × n and
building copies multiply quantities and are shown as such in the working ("× 4 typical floors").

---

## 4. Testing strategy

- **Unit:** each stage on small synthetic PDFs generated in tests (pymupdf/pdf drawing API) — strips, columns,
  dashed-line merging, label regexes, callout parser (`3K16+1K12` → two bar rows), title-block parser.
- **Contract:** `inputs_builder` output validated against a pydantic model of the engine input.
- **API:** auth required, per-project access (viewer can't PATCH; other users get 404), 409 on missing values,
  upload validation (oversize, encrypted, non-PDF).
- **Golden (nightly + on `pipeline/` changes):** Work Lounge now, Building 2 Panvel when its PDFs are added.
  `golden_report.py` writes metrics JSON; CI fails if a metric drops more than 2 points from `metrics_baseline.json`.
  Targets before beta (§8): ≥ 95% members matched, ≥ 90% bars right before review, total steel within 1% after review.
- **Vision calls in CI:** replay from the recorded `model_calls` cache (no live API in CI); a manual workflow
  re-records.

## 5. Security checklist (§9)

Private bucket, presigned URLs ≤ 10 min; `project_role` dependency on every project route (tested); session
cookie HttpOnly/Secure/SameSite; CSRF token on state-changing requests; upload validation and size limits;
PDF parsing in the worker only (never in the web process), with a per-job time and memory limit; only crops sent
to the model API; secrets only in App Platform env; delete project = delete rows + storage prefix (background job).

## 6. Sequence and parallelism

```
M0 ──► M1 ──► M2 ──► M3 ──► M4 ──► M5 ──► M6
              │            ▲
              └─ Review UI ┘ (built once in M2, extended in M3)
```
Parallel once M1 lands: schedule segmentation/OCR prototyping (M3 steps 2–3) can start against the golden PDFs
while M2 is in progress, since it only needs rendered crops. The M3 accuracy gate decides the timeline after it.

## 7. First week, concretely

1. D1 (PyMuPDF licence) and D6 (email provider) decided; Jash creates DO resources on the personal account.
2. PR 1: settings, docker-compose, Alembic, M0 tables, `import_pilot.py`, `inputs_builder` + test.
3. PR 2: auth + project access deps + tests.
4. PR 3: legacy routes moved, `PID` routing in the frontend, Projects list + Create project.
5. PR 4: `.do/app.yaml` with Postgres + pre-deploy migration; first deploy.
6. PR 5: M1 uploads + worker skeleton (ingest, render) behind the Upload view.

## 8. Open questions added by this plan

1. PyMuPDF commercial licence vs switching to pypdfium2 (D1) — affects every pipeline stage.
2. Sending domain for magic-link emails (D6).
3. Is Redis as an App Platform dev component acceptable for beta, or managed Redis from day one?
4. Should Work Lounge stay file-backed for its drawings (simplest) or be re-ingested through the pipeline as the
   first golden run (better test, more storage)? Recommendation: re-ingest in M1 and keep the files as fixtures.
