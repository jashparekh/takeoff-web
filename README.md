# Takeoff web

Web app for checking structural drawings and the steel, concrete and cement taken off them. Every number shows the formula and the working behind it.

Pilot project loaded: **Work Lounge, Hubballi**, second floor slab + beams (Manohar Consultants, R0 16.09.2026).

## Views
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

## Layout
- `app/engine.py`: quantity engine (explained steps).
- `app/main.py`: FastAPI API + static frontend.
- `app/data/<project>/`: `project.json` (sheets, hotspots), `inputs.json` (beams, bars, assumptions), `issues.json`, `notebook.json`, `drawings/*.svg`.
- `web/`: no-build frontend (ES modules).
- `tools/import_workbook.py`: imports the pilot workbook inputs.

## Deploy
DigitalOcean App Platform, spec in `.do/app.yaml` (Dockerfile build, deploys on push to `main`).
