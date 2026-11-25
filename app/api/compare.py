"""Compare a calculation with a site takeoff (the QS notebook), and raise automatic issues from the result."""
from __future__ import annotations

from ..engine import unit_wt

FT = 0.3048


def notebook_compare(nb: dict | None, result: dict) -> dict | None:
    if not nb:
        return None
    kg = {b['name']: b for b in result['beams']}
    rows = []
    for g in nb['groups']:
        ours = sum(kg[n]['kg'] for n in g['beams'] if n in kg)
        ours_dia = {}
        for n in g['beams']:
            if n in kg:
                for d, v in kg[n]['kg_by_dia'].items():
                    ours_dia[d] = ours_dia.get(d, 0) + v
        if 'feet' in g:
            theirs_dia = {int(d): sum(v) * FT * unit_wt(int(d)) for d, v in g['feet'].items()}
            theirs = sum(theirs_dia.values())
        else:
            theirs_dia, theirs = None, g['kg']
        rows.append(dict(g, theirs_kg=theirs, ours_kg=ours, diff_kg=theirs - ours,
                         diff_pct=(theirs - ours) / ours * 100 if ours else None,
                         theirs_by_dia=theirs_dia, ours_by_dia=ours_dia))
    used = {n for g in nb['groups'] for n in g['beams']}
    # Page 1 may be recorded only as a page total; `page1_bases` lists every beam that total covers.
    # Without it, page 1 covers the beams its own groups name.
    page1 = set(nb.get('page1_bases') or {n.split('-')[0] for g in nb['groups'] if g.get('page') == 1 for n in g['beams']})
    p1_ours = sum(b['kg'] for b in result['beams'] if b['base'] in page1)
    p2_theirs = sum(r['theirs_kg'] for r in rows if r['page'] == 2)
    p2_ours = sum(r['ours_kg'] for r in rows if r['page'] == 2)
    not_in_nb = [b['name'] for b in result['beams'] if b['name'] not in used and b['base'] not in page1]
    cb = result['chajja']['kg'] if result.get('chajja') else 0
    p1_theirs = nb.get('page1_total_kg', sum(r['theirs_kg'] for r in rows if r['page'] == 1))
    theirs_total = p1_theirs + p2_theirs
    ours_total = result['summary']['beam_steel_kg']
    return dict(source=nb['source'], page1_note=nb.get('page1_note'), rows=rows,
                totals=dict(page1=dict(theirs=p1_theirs, ours=p1_ours),
                            page2=dict(theirs=p2_theirs, ours=p2_ours),
                            chajja=dict(theirs=0, ours=cb),
                            not_in_notebook=not_in_nb,
                            beams=dict(theirs=theirs_total, ours=ours_total, diff=theirs_total - ours_total)))


def auto_issues(result: dict, cmp: dict | None) -> list:
    out = []
    l3 = sorted({b['base'] for b in result['beams'] for x in b['bars']
                 if x['role'] == 'Extra top @ support' and 'L/3' in x['steps'][0]['formula']})
    if l3:
        out.append(dict(id='A1', severity='low', kind='Assumed length', auto=True,
                        title=f'Extra top bar length not drawn on {len(l3)} beams (L/3 used)',
                        detail='Where the elevation shows extra top bars without a length, they are taken L/3 into the span from the support face.',
                        affects='Extra top bars', assumption='L/3 + b/2', ask='Confirm the curtailment length for extra top bars.', beams=l3))
    if cmp:
        big = [r for r in cmp['rows'] if r['diff_pct'] is not None and abs(r['diff_pct']) >= 25 and abs(r['diff_kg']) >= 20]
        if big:
            out.append(dict(id='A2', severity='medium', kind='Site count differs', auto=True,
                            title=f'Site QS notebook differs by 25% or more on {len(big)} beam groups',
                            detail='Largest gaps: ' + ', '.join(f"{r['label']} ({r['diff_kg']:+.0f} kg)" for r in sorted(big, key=lambda r: -abs(r['diff_kg']))[:6]) + '.',
                            affects='Steel quantity', assumption='Our count follows the drawn bar lengths',
                            ask='Check these beams on site against the drawing.',
                            beams=sorted({n.split('-')[0] for r in big for n in r['beams']})))
    return out
