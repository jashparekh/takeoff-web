"""Values we need (plan M4 step 2): every value the calculation uses, with a proposed default, and whether a person
must confirm it before anything is calculated (the drawings don't give it, or it is a method or price choice).

A project's `assumptions` rows hold the values used. `ensure_values` adds any registry value a project lacks, with
its proposed default and `confirmed_at = None`; values already there (imported from the pilot workbook, entered by a
person) are left alone. Values read from the drawings arrive with their source and, if they need a tick, still need
it.
"""
from __future__ import annotations
from dataclasses import dataclass

from ..engine import CONVENTIONS


@dataclass(frozen=True)
class Value:
    key: str
    label: str
    unit: str
    group: str
    default: float
    confirm: bool                 # a person must confirm it before a run
    lo: float
    hi: float
    why: str = ''                 # what it changes, in plain words


G, D, S, M, W, R = 'Geometry', 'Detailing', 'Slab', 'Materials', 'Wastage', 'Rates'
REGISTRY: tuple[Value, ...] = (
    Value('slab_t_in', 'Slab thickness', 'in', G, 5, False, 3, 12, 'Beam concrete below the slab, slab concrete.'),
    Value('floor_sqft', 'Floor footprint (slab outline incl. beams)', 'sq ft', G, 0, True, 1, 1_000_000,
          'Slab area and the cost per sq ft.'),
    Value('concrete_grade', 'Concrete grade (M)', 'N/mm²', M, 25, True, 15, 60,
          'Sets the cement content and the development length the consultant intends.'),
    Value('steel_grade', 'Steel grade (Fe)', 'N/mm²', M, 550, True, 415, 600, 'Bar rates and the development length.'),
    Value('cover_mm', 'Beam clear cover', 'mm', D, 25, True, 15, 75, 'Stirrup cut length.'),
    Value('ld_factor', 'Anchorage / lap length factor', '× bar dia', D, 50, True, 30, 80,
          'Top through bar length (Ld at each end).'),
    Value('bot_anch_mm', 'Beam bottom-bar anchorage per end', 'mm', D, 190, True, 50, 600,
          'Bottom through bar length.'),
    Value('extra_bot_frac', 'Extra bottom bar length', '× clear span', D, 0.8, True, 0.5, 1,
          'Extra bottom bars are curtailed to this share of the span.'),
    Value('extra_top_frac', 'Extra top bar length into the span, when not drawn', '× clear span', D,
          CONVENTIONS['extra_top_frac'], True, 0.2, 0.5, 'Extra top bars over supports with no length on the elevation.'),
    Value('stirrup_hook_d', 'Stirrup hook length (135°), each', '× bar dia', D, CONVENTIONS['stirrup_hook_d'], True,
          6, 16, 'Stirrup cut length.'),
    Value('cant_hook_d', 'Cantilever bar end hook', '× bar dia', D, CONVENTIONS['cant_hook_d'], True, 6, 20,
          'Cantilever top and bottom bar lengths.'),
    Value('slab_bot_sp_in', 'Slab bottom bar spacing (avg, both ways)', 'in', S, 8, True, 3, 18, 'Slab bottom mesh.'),
    Value('slab_top_sp_in', 'Slab top bar spacing over beams (avg)', 'in', S, 8, True, 3, 18, 'Slab top bars.'),
    Value('slab_top_ext_in', 'Slab top bar extension each side of beam', 'in', S, 30, True, 6, 96, 'Slab top bars.'),
    Value('slab_dist_sp_in', 'Slab top distribution spacing', 'in', S, 9, True, 3, 18, 'Slab distribution bars.'),
    Value('slab_anch_allow', 'Slab bar anchorage/lap allowance', 'fraction', S, 0.08, True, 0, 0.3, 'Slab bottom mesh.'),
    Value('steel_wastage', 'Steel wastage / laps', 'fraction', W, 0.03, True, 0, 0.2, 'Steel total.'),
    Value('conc_wastage', 'Concrete wastage', 'fraction', W, 0.02, True, 0, 0.2, 'Concrete total.'),
    Value('cement_kg_m3', 'Cement content (design mix)', 'kg/m³', M, 350, True, 250, 550, 'Cement bags.'),
    Value('bag_kg', 'Cement bag weight', 'kg', M, 50, False, 25, 50, 'Cement bags.'),
    *(Value(f'rate_{d}', f'TMT {d} mm', '₹/kg', R, r, True, 1, 500, 'Steel cost.')
      for d, r in ((8, 74), (10, 61), (12, 60), (16, 59), (20, 59), (25, 59))),
    Value('fe550_premium', 'Fe550 premium over Fe500D', '₹/kg', R, 2, True, 0, 50, 'Steel cost.'),
    Value('rate_rmc', 'Ready-mix concrete', '₹/m³', R, 5400, True, 1000, 20000, 'Concrete cost.'),
    Value('rate_cement_bag', 'OPC 53 cement', '₹/bag', R, 427, True, 100, 1500, 'Cement cost.'),
)
BY_KEY = {v.key: v for v in REGISTRY}


def ensure_values(project) -> int:
    """Add registry values the project lacks (proposed default, unconfirmed). Returns how many were added."""
    from ..db.models import Assumption
    have = {a.key for a in project.assumptions}
    start = max((a.sort for a in project.assumptions), default=-1) + 1
    added = 0
    for v in REGISTRY:
        if v.key in have:
            continue
        project.assumptions.append(Assumption(
            key=v.key, label=v.label, value=v.default, unit=v.unit, group=v.group, needs_confirm=v.confirm,
            source='Proposed default; not given on the drawings.' if v.confirm else 'Standard value.',
            sort=start + added))
        added += 1
    return added


def needed(project) -> list:
    """Values a person still has to confirm before a run."""
    return [a for a in project.assumptions if a.needs_confirm and a.confirmed_at is None]
