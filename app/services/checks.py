"""Checks → Issues (plan M4 step 1). A registry of rules, each `(context) -> {code: fields}`. Issues are upserted by
code (one rule, one key), so re-running never duplicates; an open Issue a rule no longer finds is removed, while one a
person answered or resolved keeps its status. Rules are grouped by what they need, and only groups that can be
evaluated run (so a rule that was not evaluated never removes its Issues):

  drawings  a floor's layout and schedule have both been read
    size       layout label and schedule disagree on a beam's size (pilot: B5 9"X24" vs 9"X18")
    no-sched   a beam on the layout has no schedule reading
    no-layout  a beam in the schedule is not on the layout
    cant       a layout cantilever the schedule has no part for (doubtful)
    numbering  beam numbers missing from the sequence (pilot: B42, B46, B51, B89)
  schedule  a schedule has been read
    repeat     a schedule sheet repeats elevations of another (pilot: sheet 2 repeats B68–B73); counted once
  slab     bottom-slab panels have been read
    slab-thk   a panel marked thicker than the slab (pilot: one panel 6" on MC-SD-06-04)
    slab-avg   panels read but not yet checked: the slab is measured by average spacing meanwhile
  values   the project has uploaded drawings
    not-stated values the drawings don't give: concrete grade; clear cover and lap / anchorage length
  run      a run was just saved (its results), plus the latest site takeoff
    assumed    extra top bars with no drawn length: L/3 (or the project's value) used
    site       the site count differs by 25% or more on beam groups

Run after the layout, schedule and vision stages, after a change in Review or Values we need, and when a run is saved.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Assumption, BeamSpec, Issue, Member, Project, Sheet


def natural(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def listing(names, n=12) -> str:
    names = sorted(names, key=natural)
    return ', '.join(names[:n]) + (f' and {len(names) - n} more' if len(names) > n else '')


@dataclass
class Ctx:
    db: Session
    project: Project
    members: list            # beams on layouts, not rejected
    specs: list              # schedule readings, not rejected, not repeats
    repeats: list            # schedule readings that repeat another sheet's
    panels: list = None          # bottom-slab panels, not rejected
    results: dict | None = None
    site: dict | None = None  # notebook comparison of the run's results


@dataclass(frozen=True)
class Rule:
    name: str
    group: str               # drawings / values / run
    fn: Callable[[Ctx], dict]


# ---------- drawings ----------

def by_name(ctx: Ctx):
    by_m: dict = {}
    for m in ctx.members:
        by_m.setdefault(m.name, m)
    return by_m, {s.name: s for s in ctx.specs}


def plausible_label(m: Member, s: BeamSpec) -> bool:
    """A layout label size worth comparing: depths are drawn in steps of 1.5" (9", 12"… 22.5", 24"), and a width the
    measured beam contradicts while the schedule agrees with it is a misread (pilot: B32 9" read as 8")."""
    b, d = float(m.label_b_in), float(m.label_d_in)
    if b < 4 or d < 9 or round(d * 2) % 3:
        return False
    return not (m.b_in and abs(b - m.b_in) >= 1 and abs(float(s.b_in) - m.b_in) < 1)


def check_size(ctx: Ctx) -> dict:
    """Cantilever parts are skipped (the label is the main beam's), and so are beams with two depths (compared in
    Review)."""
    by_m, by_s = by_name(ctx)
    out = {}
    for name, m in by_m.items():
        s = by_s.get(name)
        if '-cant' in name or not (s and m.label_b_in and m.label_d_in and s.b_in and s.D_in) or s.D2_in:
            continue
        if (float(m.label_b_in), float(m.label_d_in)) != (float(s.b_in), float(s.D_in)) and plausible_label(m, s):
            out[f'X-SIZE-{name}'] = dict(
                severity='medium', kind='Drawings disagree', beams=[name],
                title=f'{name}: layout says {m.label_b_in:g}"×{m.label_d_in:g}", schedule says {s.b_in:g}"×{s.D_in:g}"',
                detail='The beam layout label and the beam schedule give different sizes for the same beam.',
                affects='Concrete, stirrups', assumption=f'Schedule size {s.b_in:g}"×{s.D_in:g}" used',
                ask=f'Confirm the size of {name}.')
    return out


def check_no_schedule(ctx: Ctx) -> dict:
    by_m, by_s = by_name(ctx)
    no_sched = sorted((n for n in by_m if '-cant' not in n and n not in by_s), key=natural)
    if not no_sched:
        return {}
    return {'X-NOSCHED': dict(
        severity='high', kind='Missing in schedule', beams=no_sched,
        title=f'{len(no_sched)} beam{"s" if len(no_sched) != 1 else ""} on the layout with no schedule reading',
        detail=f'On the beam layout but not read from any beam schedule: {listing(no_sched)}.',
        affects='Steel quantity', assumption='Not counted until a schedule reading exists',
        ask='Check the schedule sheets for these beams, or confirm they are not beams.')}


def check_not_on_layout(ctx: Ctx) -> dict:
    by_m, by_s = by_name(ctx)
    no_layout = sorted((n for n in by_s if n not in by_m), key=natural)
    if not no_layout:
        return {}
    return {'X-NOLAYOUT': dict(
        severity='high', kind='Missing on layout', beams=sorted({n.split('-')[0] for n in no_layout}, key=natural),
        title=f'{len(no_layout)} schedule beam{"s" if len(no_layout) != 1 else ""} not found on the layout',
        detail=f'In the beam schedule but not placed on the layout: {listing(no_layout)}.',
        affects='Steel quantity, spans', assumption='Not counted until placed on the layout',
        ask='Find these on the layout (Review → add a missed beam), or confirm they are not on this floor.')}


def check_cantilever(ctx: Ctx) -> dict:
    """The schedule says which beams have cantilever parts; a layout cantilever without one is doubtful."""
    by_m, by_s = by_name(ctx)
    doubtful = []
    for n, m in by_m.items():
        if '-cant' not in n:
            continue
        base = n.split('-')[0]
        if base in by_s and not any(s.startswith(base + '-cant') for s in by_s) and m.status == 'auto':
            m.confidence = min(m.confidence, 0.2)
            note = 'The schedule shows no cantilever part for this beam.'
            if note not in (m.note or ''):
                m.note = f'{m.note} {note}'.strip() if m.note else note
            doubtful.append(n)
    if not doubtful:
        return {}
    return {'X-CANT': dict(
        severity='low', kind='Layout vs schedule', beams=sorted({n.split('-')[0] for n in doubtful}, key=natural),
        title=f'{len(doubtful)} overhang{"s" if len(doubtful) != 1 else ""} on the layout with no cantilever in the schedule',
        detail=f'Read as cantilevers from the layout, but their beams have no cantilever part in the schedule: {listing(doubtful)}.',
        affects='Steel quantity', assumption='Shown in Review as doubtful',
        ask='Mark them not on drawing in Review, or confirm the overhang.')}


NUMBER = re.compile(r'^H?B(\d{1,3})[A-Z]?$')         # HB29 (hidden beam) takes number 29 in the same sequence


def check_numbering(ctx: Ctx) -> dict:
    """Beam numbers missing from the sequence B1…Bn, over the layout and the schedule together. Only reported when
    the gaps are few: many gaps mean the drawings are not all read yet, not that numbers were skipped."""
    nums = {int(m.group(1)) for n in [*(m.name for m in ctx.members), *(s.name for s in ctx.specs)]
            if (m := NUMBER.match(n.split('-')[0]))}
    if len(nums) < 5:
        return {}
    gaps = [f'B{k}' for k in range(1, max(nums) + 1) if k not in nums]
    if not gaps or len(gaps) > max(3, max(nums) // 10):
        return {}
    return {'X-NUMBERING': dict(
        severity='low', kind='Numbering', beams=[],
        title=f'Beam number{"s" if len(gaps) != 1 else ""} {listing(gaps)} do{"" if len(gaps) != 1 else "es"} not appear',
        detail=f'B1 to B{max(nums)} are used on the layout and schedules, but {listing(gaps)} '
               f'{"are" if len(gaps) != 1 else "is"} on neither.',
        affects='Completeness', assumption='Taken as numbers skipped, not beams missing',
        ask=f'Confirm {listing(gaps)} {"are" if len(gaps) != 1 else "is"} not used (no beam missing from the drawings).')}


def check_repeat(ctx: Ctx) -> dict:
    """One Issue per schedule sheet listing the elevations it repeats (pilot: sheet 2 repeats B68–B73)."""
    out: dict = {}
    sheets: dict = {}
    for r in ctx.repeats:
        sheets.setdefault(r.sheet_id, []).append(r.name)
    for sid, names in sheets.items():
        s = ctx.db.get(Sheet, sid)
        code = f'REP-{(s.drg_no if s else None) or sid.hex[:6]}'
        out[code] = dict(
            severity='low', kind='Drawing repeats', beams=sorted({n.split('-')[0] for n in names}, key=natural),
            title=f'{(s.drg_no if s else None) or "A schedule sheet"} repeats {len(names)} beam elevation{"s" if len(names) != 1 else ""}',
            detail=f'Already drawn on another schedule sheet: {listing(names, 20)}. Counted once.',
            affects='Steel quantity', assumption='Each beam counted once',
            ask='Confirm these are repeats, not separate beams with the same names.')
    return out


# ---------- slab ----------

def check_slab_thickness(ctx: Ctx) -> dict:
    from .values import BY_KEY
    t = next((a.value for a in ctx.project.assumptions if a.key == 'slab_t_in'), BY_KEY['slab_t_in'].default)
    out = {}
    for p in ctx.panels:
        thk = (p.data or {}).get('thk_in')
        if thk and t and thk != t and p.status != 'rejected':
            s = ctx.db.get(Sheet, p.sheet_id)
            drg = (s.drg_no if s else None) or 'the bottom slab layout'
            out[f'X-SLAB-THK-{p.name}'] = dict(
                severity='medium', kind='Drawings disagree', beams=[],
                where_json=dict(sheet=str(p.sheet_id), x=(p.x1 + p.x2) / 2, y=(p.y1 + p.y2) / 2, zoom=3),
                title=f'Slab panel {p.name} marked {thk:g}" thick on {drg}; the slab is {t:g}"',
                detail=f'One panel ({p.area_sqft or 0:,.0f} sq ft) carries a {thk:g}" thickness note; the rest of the slab '
                       f'is {t:g}".',
                affects='Slab concrete', assumption=f'{thk:g}" for this panel, {t:g}" elsewhere',
                ask=f'Confirm the {thk:g}" panel: its thickness and extent.')
    return out


def check_slab_waiting(ctx: Ctx) -> dict:
    waiting = [p for p in ctx.panels if p.status == 'auto']
    if not waiting:
        return {}
    return {'X-SLAB-AVG': dict(
        severity='low', kind='Waiting for Review', beams=[],
        title=f'{len(waiting)} slab panel{"s" if len(waiting) != 1 else ""} read but not yet checked',
        detail='The bottom slab is read panel by panel, but the readings are used only once every panel is checked '
               'in Review. Until then the slab bottom bars are measured by average spacing.',
        affects='Slab steel', assumption=f'Average spacing for the slab bottom mesh',
        ask='Check the slab panels in Review.')}


# ---------- values ----------

def read_from_drawing(a: Assumption | None) -> bool:
    """A value a reader found on the drawings carries a source starting 'Read from'."""
    return bool(a and (a.source or '').startswith('Read from'))


def check_not_stated(ctx: Ctx) -> dict:
    a = {x.key: x for x in ctx.project.assumptions}
    out = {}
    g = a.get('concrete_grade')
    if g and not read_from_drawing(g):
        out['X-STATED-GRADE'] = dict(
            severity='high', kind='Missing on drawing', beams=[],
            title='Concrete grade for beams and slab not stated',
            detail='The drawings read so far give no concrete grade for beams and slabs. It sets the cement content '
                   'and the development length.',
            affects='Cement, development length', assumption=f'Concrete grade M{g.value:g}',
            ask='Confirm the concrete grade for the beams and slab.')
    cover, ld = a.get('cover_mm'), a.get('ld_factor')
    if cover and ld and not (read_from_drawing(cover) and read_from_drawing(ld)):
        out['X-STATED-COVER'] = dict(
            severity='high', kind='Missing on drawing', beams=[],
            title='Clear cover and lap / anchorage length not stated for beams and slabs',
            detail='The drawings read so far give neither the clear cover nor the lap / development length for beams '
                   'and slabs.',
            affects='Stirrup lengths, top bar lengths', assumption=f'{cover.value:g} mm cover, Ld = {ld.value:g}d',
            ask='Confirm the clear cover and the lap / development length for beams and slabs.')
    return out


# ---------- run ----------

def check_assumed_length(ctx: Ctx) -> dict:
    hits = sorted({b['base'] for b in ctx.results['beams'] for x in b['bars']
                   if x['role'] == 'Extra top @ support' and 'drawn length' not in x['steps'][0]['formula']},
                  key=natural)
    if not hits:
        return {}
    share = next(x['steps'][0]['formula'] for b in ctx.results['beams'] for x in b['bars']
                 if x['role'] == 'Extra top @ support' and 'drawn length' not in x['steps'][0]['formula'])
    return {'X-ASSUMED-TOP': dict(
        severity='low', kind='Assumed length', beams=hits,
        title=f'Extra top bar length not drawn on {len(hits)} beam{"s" if len(hits) != 1 else ""} ({share.split(" +")[0]} used)',
        detail='Where the elevation shows extra top bars without a length, they are taken into the span from the '
               'support face by the share set in Values we need.',
        affects='Extra top bars', assumption=share, ask='Confirm the curtailment length for extra top bars.')}


def check_site_count(ctx: Ctx) -> dict:
    cmp = ctx.site
    if not cmp:
        return {}
    big = [r for r in cmp['rows'] if r['diff_pct'] is not None and abs(r['diff_pct']) >= 25 and abs(r['diff_kg']) >= 20]
    if not big:
        return {}
    return {'X-SITE': dict(
        severity='medium', kind='Site count differs', beams=sorted({n.split('-')[0] for r in big for n in r['beams']}, key=natural),
        title=f'Site count differs by 25% or more on {len(big)} beam groups',
        detail='Largest gaps: ' + ', '.join(f"{r['label']} ({r['diff_kg']:+.0f} kg)"
                                            for r in sorted(big, key=lambda r: -abs(r['diff_kg']))[:6]) + '.',
        affects='Steel quantity', assumption='Our count follows the drawn bar lengths',
        ask='Check these beams on site against the drawing.')}


RULES: tuple[Rule, ...] = (
    Rule('check_size', 'drawings', check_size),
    Rule('check_no_schedule', 'drawings', check_no_schedule),
    Rule('check_not_on_layout', 'drawings', check_not_on_layout),
    Rule('check_cantilever', 'drawings', check_cantilever),
    Rule('check_numbering', 'drawings', check_numbering),
    Rule('check_repeat', 'schedule', check_repeat),
    Rule('check_slab_thickness', 'slab', check_slab_thickness),
    Rule('check_slab_waiting', 'slab', check_slab_waiting),
    Rule('check_not_stated', 'values', check_not_stated),
    Rule('check_assumed_length', 'run', check_assumed_length),
    Rule('check_site_count', 'run', check_site_count),
)
RULE_NAMES = tuple(r.name for r in RULES) + ('schedule_repeat',)     # older name of check_repeat, cleaned up here


def run(db: Session, project_id, results: dict | None = None, site: dict | None = None) -> list[Issue]:
    """Evaluate every rule that can be, and bring the project's Issues in line. `results` (+ `site`) from a run just
    saved enables the run rules."""
    project = db.get(Project, project_id)
    members = db.scalars(select(Member).where(Member.project_id == project_id, Member.kind == 'beam',
                                              Member.status != 'rejected')).all()
    specs = db.scalars(select(BeamSpec).where(BeamSpec.project_id == project_id, BeamSpec.status != 'rejected')).all()
    panels = db.scalars(select(Member).where(Member.project_id == project_id, Member.kind == 'panel',
                                             Member.status != 'rejected')).all()
    ctx = Ctx(db, project, list(members), [s for s in specs if s.duplicate_of is None],
              [s for s in specs if s.duplicate_of is not None], list(panels), results, site)
    groups = set()
    if ctx.members and ctx.specs:                   # need both a layout and a schedule to compare
        groups.add('drawings')
    if ctx.specs or ctx.repeats:
        groups.add('schedule')
    if ctx.panels or db.scalar(select(Sheet.id).where(Sheet.project_id == project_id,
                                                      Sheet.slab_version.is_not(None)).limit(1)):
        groups.add('slab')
    if db.scalar(select(Sheet.id).where(Sheet.project_id == project_id).limit(1)):
        groups.add('values')
    if results is not None:
        groups.add('run')
    ran = [r for r in RULES if r.group in groups]
    found: dict[str, tuple[str, dict]] = {}
    for r in ran:
        for code, fields in r.fn(ctx).items():
            found[code] = (r.name, fields)
    names = {r.name for r in ran} | ({'schedule_repeat'} if 'schedule' in groups else set())
    existing = {i.code: i for i in db.scalars(select(Issue).where(Issue.project_id == project_id,
                                                                  Issue.rule.in_(RULE_NAMES)))}
    out = []
    for code, (rule, fields) in found.items():
        i = existing.pop(code, None)
        if i is None:
            i = Issue(project_id=project_id, code=code)
            db.add(i)
        i.rule = rule
        for k, v in fields.items():
            setattr(i, k, v)
        out.append(i)
    for i in existing.values():                     # no longer found: remove it if nobody acted on it
        if i.rule in names and i.status == 'open':
            db.delete(i)
    db.flush()
    return out
