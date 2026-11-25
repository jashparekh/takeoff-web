"""Reconcile the OCR reading of an elevation with the vision model's (plan M3 step 5), field by field.

  agree       both read the same                          → used, confident
  model_only  OCR read nothing for the field               → model's value, to review
  ocr_only    the model could not read it                  → OCR's value, to review
  resolved    they differed and the escalation model sided with one of them → that value, to review
  disagree    still split                                  → escalation (or model) value, flagged; Review shows all
  missing     nobody read it                               → nothing; Review must fill it

Never picks silently: every field's status and every reading is stored on the spec and shown in Review.
"""
from __future__ import annotations

from .vision import ElevationReading

ROLES = ('Top through', 'Top main', 'Extra top @ support', 'Bottom through', 'Bottom extra')
FIELDS = ('size', 'stirrup_dia', 'stirrup_end_in', 'stirrup_mid_in') + ROLES
NEEDED = ('size', 'Bottom through')                  # without these the beam can't be quantified


def from_spec(spec) -> dict:
    """The OCR reading of a BeamSpec in comparable form. A role OCR found nothing for is unknown (None), since
    'no callout read' and 'no such bars' look the same to OCR."""
    out = {'size': (spec.b_in, spec.D_in) if spec.b_in and spec.D_in else None,
           'stirrup_dia': spec.stirrup_dia, 'stirrup_end_in': spec.stirrup_end_in, 'stirrup_mid_in': spec.stirrup_mid_in}
    for role in ROLES:
        groups = sorted((b.nos, b.dia) for b in spec.bars if b.role == role)
        out[role] = tuple(groups) if groups else None
    return out


def from_model(r: ElevationReading) -> dict:
    """The model's reading in the same form. Empty lists are answers ('none drawn'); fields the model says it could
    not read are unknown."""
    st = r.stirrups
    out = {'size': (r.b_in, r.D_in) if r.b_in and r.D_in else None,
           'stirrup_dia': st.dia, 'stirrup_end_in': st.end_in, 'stirrup_mid_in': st.mid_in}
    lists = {'Top through': r.top_through, 'Top main': r.top_main, 'Extra top @ support': r.extra_top,
             'Bottom through': r.bottom_through, 'Bottom extra': r.bottom_extra}
    for role, groups in lists.items():
        out[role] = tuple(sorted((g.nos, g.dia) for g in groups))
    return out


def norm(v):
    """Compare bar lists as totals per diameter, so 2K20+1K20 equals 3K20; empty and unknown stay distinct."""
    if isinstance(v, tuple) and v and isinstance(v[0], tuple):
        tot: dict = {}
        for n, d in v:
            tot[d] = tot.get(d, 0) + n
        return tuple(sorted(tot.items(), key=lambda kv: kv[0]))
    return v


def reconcile(ocr: dict, model: dict, escalated: dict | None = None) -> tuple[dict, dict, float]:
    """Returns (merged values, status per field, confidence)."""
    merged, status = {}, {}
    for f in FIELDS:
        o, m, e = ocr.get(f), model.get(f), (escalated or {}).get(f)
        empty_role = f in ROLES and m == ()                 # the model says none drawn
        if o is None and (m is None or empty_role):
            merged[f], status[f] = (m if empty_role else None), ('agree' if empty_role else 'missing')
        elif o is None:
            merged[f], status[f] = m, 'model_only'
        elif m is None:
            merged[f], status[f] = o, 'ocr_only'
        elif norm(o) == norm(m):
            merged[f], status[f] = m, 'agree'
        elif escalated is not None and e is not None and norm(e) in (norm(o), norm(m)):
            merged[f], status[f] = e, 'resolved'
        else:
            merged[f], status[f] = (e if e is not None else m), 'disagree'
    conf = 0.95
    if any(s in ('model_only', 'ocr_only', 'resolved') for s in status.values()):
        conf = 0.75
    if any(status[f] == 'missing' for f in NEEDED) or any(s == 'missing' for f, s in status.items()
                                                          if f.startswith('stirrup')):
        conf = min(conf, 0.5)
    if 'disagree' in status.values():
        conf = 0.35
    return merged, status, conf


def extra_top_lengths(r: ElevationReading | None) -> dict:
    """(nos, dia) → length in inches from the model's bracket reading, where it read one."""
    if r is None:
        return {}
    return {(g.nos, g.dia): g.length_in for g in r.extra_top if g.length_in}
