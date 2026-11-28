"""Build the 3D framing model from `members` (plan M2 step 4), in the shape `web/js/model3d.js` reads (the same as the
pilot's `app/data/<slug>/model.json`), plus member sizes and the project's floors.

One measured floor is drawn solid (the floor whose beam layout has the most members); the project's other floors
are ghosted at their levels, as in the pilot. Members marked "not on drawing" are left out. Sizes come from each
label (e.g. B13(9"X18")); a beam whose label gave no depth is drawn at a display depth and says so.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Building, Floor, Member, Project, Sheet

DISPLAY_DEPTH_IN = 18          # only for drawing a beam whose depth is not known yet; never used in quantities
DISPLAY_FLOOR_H = 3.0          # m, when floor levels are not given


def measured_sheet(db: Session, p: Project) -> Sheet | None:
    row = db.execute(select(Sheet, func.count(Member.id)).join(Member, Member.sheet_id == Sheet.id)
                     .where(Sheet.project_id == p.id, Sheet.sheet_type == 'beam_layout', Member.status != 'rejected',
                            Sheet.superseded_by.is_(None))
                     .group_by(Sheet.id).order_by(func.count(Member.id).desc()).limit(1)).first()
    return row[0] if row else None


def floors(db: Session, p: Project, measured_floor_id) -> tuple[list[dict], int, float]:
    """All floors bottom to top with their levels, the index of the measured one, and the floor height used."""
    fl = db.scalars(select(Floor).join(Building).where(Building.project_id == p.id)
                    .order_by(Building.sort, Floor.sort)).all()
    levels = [f.level_m for f in fl if f.level_m is not None]
    diffs = sorted(b - a for a, b in zip(levels, levels[1:]) if b > a)
    h = diffs[len(diffs) // 2] if diffs else DISPLAY_FLOOR_H
    out = [dict(id=str(f.id), name=f.name, level_m=f.level_m, measured=f.id == measured_floor_id) for f in fl]
    idx = next((i for i, f in enumerate(fl) if f.id == measured_floor_id), len(fl) - 1 if fl else 0)
    return out, idx, h


def build_model(db: Session, p: Project) -> dict | None:
    s = measured_sheet(db, p)
    if s is None or not s.pt_per_in:
        return None
    ms = db.scalars(select(Member).where(Member.sheet_id == s.id, Member.status != 'rejected')).all()
    beams = [m for m in ms if m.kind == 'beam' and (m.x1, m.y1) != (m.x2, m.y2)]
    cols = [m for m in ms if m.kind == 'column']
    slab = next((m for m in ms if m.kind == 'slab_panel' and m.points), None)
    xs = [v for m in beams + cols for v in (m.x1, m.x2)] + ([q[0] for q in slab.points] if slab else [])
    ys = [v for m in beams + cols for v in (m.y1, m.y2)] + ([q[1] for q in slab.points] if slab else [])
    if not xs:
        return None
    ox, oy = min(xs), min(ys)
    k = 0.0254 / s.pt_per_in
    M = lambda x, y: [round((x - ox) * k, 3), round((y - oy) * k, 3)]                # noqa: E731
    fl, idx, h = floors(db, p, s.floor_id)
    return dict(
        units='m', source=f'{s.drg_no or "beam layout"} (read from the drawing)', m_per_pt=k, origin_pt=[ox, oy],
        size=M(max(xs), max(ys)), sheet_id=str(s.id), from_members=True,
        beams=[dict(name=m.name, vertical=bool(m.vertical), p1=M(m.x1, m.y1), p2=M(m.x2, m.y2),
                    cant='-cant' in (m.name or ''), b_in=m.label_b_in or m.b_in,
                    D_in=m.label_d_in or DISPLAY_DEPTH_IN, depth_known=m.label_d_in is not None,
                    status=m.status, member_id=str(m.id)) for m in beams],
        columns=[dict(c=M((m.x1 + m.x2) / 2, (m.y1 + m.y2) / 2), w=round((m.x2 - m.x1) * k, 3),
                      d=round((m.y2 - m.y1) * k, 3), member_id=str(m.id)) for m in cols],
        slab=dict(outline=[M(*q) for q in slab.points] if slab else [], footprint_sqft=slab.area_sqft if slab else None),
        unplaced=sorted({m.name for m in ms if m.kind == 'beam' and (m.x1, m.y1) == (m.x2, m.y2) and m.name}),
        floors=fl, level=idx, floor_h=h)
