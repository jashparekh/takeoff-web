# Docs

- `Work_Lounge_Hubballi_SF_Slab_Beams_Estimate.xlsx`: pilot 2 workbook (inputs and formulas). `tools/import_workbook.py` reads it into `app/data/worklounge/inputs.json`.
- `B47_Steel_Count_Explained.pdf`: owner explanation and engineer drawing sheet comparing the site QS notebook with Takeoff for beam B47.

Consultant drawings (Manohar Consultants, R0 16.09.2026) are in `app/data/worklounge/source/`; `tools/extract_model.py` builds `model.json` from the beam layout.
