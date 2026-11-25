# Plan: drawings uploaded by the user

Status: proposal · Owner: Jash · Last updated: 2026-11-25

## 1. Goal

A user uploads a project's structural drawing PDFs and gets everything the Work Lounge pilot has today, for their own project:

- every sheet viewable and zoomable, with members clickable on the drawing;
- a 3D model of the measured floors, every member clickable;
- beam / slab (later column, footing) quantities, BBS and cost, each number with its formula and working;
- Issues: what the drawings don't say, or say twice, with the value used meanwhile and a question list for the consultant;
- optionally, a site check against a site takeoff.

The pilot was built by hand-guided scripts (`tools/import_workbook.py`, `tools/extract_model.py`, OCR passes, manual hotspot fixes). This plan turns those steps into a pipeline that runs per upload, with a person confirming what the machine read.

### Principles (from the pilot)
1. **Never assume silently.** Every value the drawings don't give becomes a "value we need". Nothing is calculated until all of them are entered and confirmed.
2. **Show the source.** Every extracted value links back to the sheet region it came from.
3. **Human in the loop by design.** The machine proposes, with a confidence; the engineer accepts or edits. Low-confidence items are queued, never dropped or guessed.
4. **Drawn lengths, not rules of thumb.** Engine conventions stay as in `CLAUDE.md`, made configurable per project.
5. **Reproducible.** Every run is versioned: inputs, assumptions, engine version and results, so revisions can be compared.

### Not in scope for v1
DWG/DXF input (PDF only; DWG later via a converter), architectural takeoff (brickwork, finishes), MEP, scheduling, payments, multi-tenant organisations beyond simple sharing.

## 2. User flow

1. **Create project**: name, site city (sets rates), consultant, buildings → floors (levels, typical floor × n, identical copies), as in the mobile mocks (`CreateProject`, `AddBuilding`).
2. **Upload PDFs** per building (drag-drop many files; one PDF may hold many sheets).
3. **Processing** (progress per sheet): render → classify → read. Takes about a minute per sheet; user can leave and come back.
4. **Coverage**: grid of which floors/parts have which sheet types (beam layout, beam schedule, slab top/bottom, column schedule). Missing parts are listed, with "upload for this part".
5. **Review**: queue of items to check, sorted by confidence. Each shows the sheet crop next to the extracted values; accept / edit / mark "not on drawing".
6. **Values we need**: everything the drawings don't give (grade, cover, Ld, cement content, sunk depths…). Required before calculating.
7. **Confirm**: summary of all values, a "checked" tick, then **Calculate**.
8. **Results**: the views that exist today (Overview, 3D model, Drawings, Beams, Slab, Issues, Site check, Assumptions), now driven by the project's data.
9. **Revision**: uploading R1 of a sheet creates a new run; Takeoff shows what changed (members, bars, quantities, cost).

## 3. Architecture

```
browser ──► FastAPI (web)  ──► Postgres (projects, sheets, members, runs)
                │                     ▲
                ├──► Spaces (S3): PDFs, SVG/PNG renders, crops
                └──► Redis queue ──► worker(s): render · classify · extract · check · calculate
                                              └─► vision model API for hard reads (schedule callouts)
```

- **Web**: current FastAPI app plus auth and project CRUD. The frontend stays no-build ES modules; add views for Upload, Coverage, Review and Values we need.
- **Worker**: same codebase, `arq` or RQ on Redis. Each stage is idempotent and writes its output plus a confidence score.
- **Storage**: DigitalOcean Spaces (private bucket, signed URLs). Postgres: DO Managed Postgres with daily backups.
- **Deploy**: App Platform, one web service and one worker, from `.do/app.yaml`. **Jash's personal DO account only; no doctl, nothing in the LoopRL team.**
- **Auth**: email magic link (one table plus signed cookie); a project has an owner and invited members (viewer / editor).

## 4. Pipeline stages

Each stage reads the previous stage's records and writes its own, with `status`, `confidence` and `source` (sheet id + bbox in PDF points).

### 4.1 Ingest
Validate the PDF (size ≤ 50 MB, not encrypted), split into pages, SHA-256 per page for de-duplication, store originals.

### 4.2 Render
Per page: SVG with text as paths (`page.get_svg_image(text_as_path=True)`, served gzipped; about 2 MB raw / 200 KB gz on the pilot), a 150 dpi PNG thumbnail, and a 300 dpi grayscale PNG for OCR. Keep the PDF→points transform so all coordinates stay in PDF points.

### 4.3 Classify sheet
Read the title block (bottom-right region: drawing number, title, revision, date, scale, consultant) with OCR plus a vision-model fallback. Sheet type from title keywords ("shuttering layout", "beam schedule", "bottom slab reinforcement", "column schedule", "footing") and layout features (many elevations → schedule). Assign floor/building from the title ("SECOND FLOOR"). The user can correct the type in Coverage.

### 4.4 Scale calibration
Candidates: the printed scale (1:100) × sheet size, and known geometry (pilot: 9″ beam = 5.4 pt, so 0.6 pt/in). Confirm against at least two printed dimension strings matched to measured lengths (pilot spot checks: B1 15′-10″ measured vs 15′-11″ printed). Store `pt_per_in` with residual error; flag the sheet if error > 2%.

### 4.5 Layout extraction (beam layout / shuttering sheet)
Generalise `tools/extract_model.py`:
- Group vector segments by stroke width; find the beam-edge width class (most H/V segments forming parallel pairs at member-width spacing). Merge dashed segments (pilot gap tolerance 9.5 pt); pair lines into **strips** (centreline, extent, width).
- Columns: closed thick quads of plausible column size; also filled/hatched rectangles.
- Labels: OCR at 250–500 dpi, horizontal plus rotated −90°, union of passes; regex `^(H?B|CB)\d+[A-Z]?\(` with size `(9"X18")` parsed too. Pilot reached 72 of 93 automatically; the rest go to Review rather than manual hard-coding.
- Match labels to strips: same orientation, perpendicular distance < 16 pt, choose the cut interval (strip ends, crossing strips, columns) whose length best matches the clear span from the schedule (± 25%).
- Slab: rasterise the edges, close dash gaps, fill enclosed regions, keep the largest plate; trace the outline (`cv2.findContours` + `approxPolyDP`). Openings: hatched or X-marked panels become holes.
- Output per member: name, size, centreline (m), confidence, and the label bbox as hotspot.

### 4.6 Schedule extraction (beam schedule sheets)
The hardest step (in the pilot, bars were read by eye from images).
- Segment the sheet into beam elevations (long horizontal member outlines plus name label below).
- For each elevation crop: OCR the callouts (`2K12`, `3K16+1K12`, `K8@6"c/c`, `EQ`, extra-bar lengths `3'-0"`) and the stirrup-zone dimension line (clear span, to scale).
- **Vision model pass** per crop returns structured JSON: top through, bottom through, bottom extra, extra top at each support (with drawn length), stirrup dia and end/middle spacing, cantilever extensions, size. Prompt with the project's notation ("K = TOR steel") and the JSON schema. Keep the model's answer only where it agrees with the OCR tokens; otherwise send to Review.
- Cross-check: span from the elevation vs span from the layout (> 5% → Review); size vs layout label (pilot B5 conflict).
- Detect repeated rows across sheets (pilot: sheet 2 repeats B68–B73) by matching names plus bar sets; count once and raise an Issue.

### 4.7 Slab extraction (top / bottom slab sheets)
Pilot used average spacings. v1 reads panel by panel: panel polygons from the slab sheet (same enclosure method), callouts inside each panel (`K8@8"`, `K10@6"`, thickness notes like `6" THK`), and top-bar extensions over beams. Each panel gets its own steel; averages remain the fallback, shown as an assumption.

### 4.8 Checks → Issues
Rule set, each producing an Issue with severity, affected members, the value used meanwhile and the question to ask:
- not stated: concrete grade, cover, lap/Ld, cement content;
- conflict: size on layout vs schedule, slab thickness note vs general note, span mismatch;
- numbering: gaps (B42, B46…), duplicates, members on the layout with no schedule entry and the reverse;
- repeated schedule rows;
- assumed lengths (extra top bars with no drawn length → L/3).

### 4.9 Review and values we need
Queue items: low-confidence reads, failed matches, every Issue that needs a value. The UI shows the sheet crop (from `source.bbox`), the extracted fields (editable) and confidence. Accept / edit / "not on drawing". Editing writes a `correction` record (who, when, old → new), so we can measure accuracy and improve prompts.

### 4.10 Calculate
Engine as today, with inputs from the database instead of `inputs.json`. Each run stores inputs, assumptions, engine version and results. Later: columns (column schedule), footings, staircase, multiple floors × copies.

### 4.11 3D
Per floor: members from 4.5 at floor elevation; floor heights from the levels entered at project creation (or the arch section, later). Identical copies and typical floors are instanced. Floors without drawings are ghosted, as in the pilot.

### 4.12 Revisions and compare
New revision of a sheet → re-run its stages → new run → diff (members added/removed, bars changed, kg and ₹ deltas), shown on the existing `RevisionCompare` mock pattern.

## 5. Data model (Postgres)

```
users(id, email, name, created_at)
projects(id, owner_id, name, city, consultant, created_at)
project_members(project_id, user_id, role)
buildings(id, project_id, name, copies)
floors(id, building_id, name, level_m, typical_count)
uploads(id, project_id, filename, sha256, bytes, created_by, created_at)
sheets(id, upload_id, page_no, sha256, drawing_no, title, revision, sheet_type, floor_id,
       pt_per_in, scale_error, svg_key, png_key, status, confidence)
members(id, floor_id, name, kind[beam|slab_panel|column], size_json, geometry_json,
        source_sheet_id, source_bbox, confidence, status)
bars(id, member_id, role, nos, dia, extra_top_in, source_bbox, confidence, status)
stirrups(member_id, dia, end_in, mid_in, source_bbox, confidence, status)
issues(id, project_id, rule, severity, title, detail, assumption, ask, member_ids, where_json, status)
assumptions(project_id, key, value, unit, source, confirmed_by, confirmed_at)
corrections(id, entity, entity_id, field, old, new, user_id, at)
runs(id, project_id, engine_version, inputs_json, assumptions_json, results_json, created_at)
site_takeoffs(id, project_id, source, groups_json)
jobs(id, sheet_id, stage, status, error, started_at, finished_at)
```

## 6. API (additions)

```
POST /api/projects                       create
POST /api/projects/{id}/uploads          multipart PDFs → upload + sheets, enqueue
GET  /api/projects/{id}/sheets           status, type, floor, coverage
PATCH /api/sheets/{id}                   correct type / floor / scale
GET  /api/projects/{id}/review           queue (paged, by confidence)
PATCH /api/members/{id}, /api/bars/{id}  accept / edit (writes corrections)
GET  /api/projects/{id}/values-needed    required assumptions not yet confirmed
PUT  /api/projects/{id}/assumptions      confirm values
POST /api/projects/{id}/runs             calculate (409 while values are missing)
GET  /api/runs/{id}, /api/runs/{a}/diff/{b}
```
Existing read endpoints (`/calculate`, `/drawings`) keep their shapes and read from the latest run, so the current views keep working.

## 7. Frontend

- Project list (home) and Create project.
- Upload (drag-drop, per-file progress, re-upload revision).
- Coverage grid per building (pattern from the `Coverage` mock).
- Review queue: crop viewer (reuse `viewer.js` focused on the bbox) plus an editable form; keyboard shortcuts (A accept, E edit, N next).
- Values we need → Confirm (patterns from the `Issues` / `Confirm` mocks; disabled button until complete).
- Existing views, unchanged except that data comes from the selected project/run.

## 8. Quality and testing

- **Golden projects**: Work Lounge (second floor) and Building 2, Panvel. Fixtures: source PDFs, hand-verified members/bars, pinned totals (Work Lounge: beams 7,744 kg, slab 3,894 kg, 11,987 kg with wastage, 83.72 m³).
- **Stage metrics** on goldens in CI: sheets classified correctly, beam labels found (pilot baseline 72/93 by OCR alone), members placed (88/92), bars exactly right, span error, total kg error. Target before beta: ≥ 95% members matched, ≥ 90% bars right before review, total steel within 1% after review.
- Unit tests per stage; the engine tests stay as they are.
- Every correction in Review is logged for later prompt and rule tuning.

## 9. Security and privacy

Consultant drawings are confidential: private bucket, signed URLs (short expiry), per-project access checks on every endpoint, no public links in v1. Model API calls send crops only, never whole sheets; no training on customer data (API terms). Deleting a project deletes its files.

## 10. Milestones

| # | Deliverable | Rough size |
|---|---|---|
| M0 | Deploy the current app (personal DO account), auth, project list | 2–3 days |
| M1 | Upload, ingest, render, sheet viewer for any uploaded PDF; classify plus manual correction; Coverage | 1 week |
| M2 | Layout extraction (members, columns, slab outline, hotspots) with Review; 3D per floor | 1–1.5 weeks |
| M3 | Schedule extraction (OCR plus vision model) with cross-checks and Review | 2 weeks |
| M4 | Checks → Issues, values we need, Confirm, runs, results from the database | 1 week |
| M5 | Panel-by-panel slab, revisions and diff | 1 week |
| M6 | Columns and footings from schedules; multi-floor, typical × n, building copies | 2 weeks |

## 11. Risks

- **Drawing styles vary** by consultant (fonts, symbols, layer conventions, scanned vs vector PDFs). Mitigation: rules plus vision-model fallback, the Review step, and a growing golden set. Scanned PDFs (no vectors) skip 4.5's vector path and use raster line detection, at lower confidence.
- **Schedule reading accuracy**: the biggest unknown. Measure on goldens in M3 before committing to timelines beyond it.
- **Cost of model calls**: one crop per beam (about 100 per floor). Cache by crop hash; only uncertain crops go to the model.
- **Engine conventions differ from site practice** (pilot: QS counts extra bars full length). Make conventions per-project settings and show both in Site check.

## 12. Open questions

1. Who uploads first: Jash's team only (beta), or consultants and contractors directly?
2. DWG support: how many consultants share DWG instead of PDF?
3. Which vision model and budget per project?
4. Floor heights: ask at project creation, or read from the architectural section?
5. Pricing and limits (sheets per project) for the beta.
