"""Drawing revisions (plan M5): the higher revision supersedes; superseded sheets count nowhere; people's decisions
carry over where the new revision reads the same, and changes go back to Review with a note."""
import pytest
from sqlalchemy import select

from app.db.models import Bar, BeamSpec, Member, Sheet, Upload
from app.pipeline import stages
from app.services import revisions as R
from app.services.inputs_builder import floor_members
from conftest import sign_in
from test_scale_ocr import floors  # noqa: F401 (fixture)
from test_uploads import proj  # noqa: F401 (fixture)


@pytest.mark.parametrize('rev, key', [('R0', (1, 0)), ('R1', (1, 1)), ('r 12', (1, 12)), ('3', (1, 3)), ('B', (0, 66)),
                                      ('Rev. C', (0, 67)), (None, None), ('draft', None)])
def test_revision_order(rev, key):
    assert R.rev_key(rev) == key


def sheet(session, p, rev, kind='beam_layout', drg='MC-SD-07', floor=None, n=[0]):
    n[0] += 1
    u = Upload(project_id=p.id, filename=f'{drg}-{rev}.pdf', sha256=f'{n[0]:064d}', size_bytes=1, storage_key='k', status='done')
    session.add(u)
    session.flush()
    s = Sheet(project_id=p.id, upload_id=u.id, page_no=1, page_hash=f'h{n[0]}', width_pt=1191, height_pt=842, status='ready',
              sheet_type=kind, drg_no=drg, revision=rev, pt_per_in=0.6, floor_id=floor.id if floor else None)
    session.add(s)
    session.flush()
    return s


def beam(session, s, name, x1, x2, status='auto', b=9, d=18, clear=120.0):
    m = Member(project_id=s.project_id, sheet_id=s.id, floor_id=s.floor_id, kind='beam', name=name, vertical=False,
               x1=x1, y1=100, x2=x2, y2=100, label_b_in=b, label_d_in=d, clear_in=clear, confidence=0.9, status=status)
    session.add(m)
    session.flush()
    return m


def test_the_higher_revision_supersedes_whichever_arrives_first(session, proj):  # noqa: F811
    r0 = sheet(session, proj, 'R0')
    r2 = sheet(session, proj, 'R2', drg='MC-SD-07 ')                       # spacing in the number doesn't matter
    assert R.link(session, r2) == r0 and r0.superseded_by == r2.id
    r1 = sheet(session, proj, 'R1')                                        # an older revision uploaded late
    assert R.link(session, r1) is None and r1.superseded_by == r2.id
    same = sheet(session, proj, 'R2')
    R.link(session, same)
    assert same.superseded_by is None and 'both are kept' in same.classify_note
    other = sheet(session, proj, 'R5', drg='MC-SD-06-01')
    assert R.link(session, other) is None and other.superseded_by is None


def test_decisions_carry_over_and_changes_go_back_to_review(client, session, proj, floors):  # noqa: F811
    fl = floors['Second floor']
    old = sheet(session, proj, 'R0', floor=fl)
    beam(session, old, 'B1', 100, 200, 'accepted')
    beam(session, old, 'B2', 100, 200, 'edited', d=24)
    beam(session, old, 'B3', 300, 400, 'rejected')
    beam(session, old, 'B4', 500, 600, 'added')
    sch0 = sheet(session, proj, 'R0', kind='beam_schedule', drg='MC-SD-06-01', floor=fl)
    for name, bars in (('B1', [('Bottom through', 2, 12)]), ('B2', [('Bottom through', 2, 12)])):
        session.add(BeamSpec(project_id=proj.id, sheet_id=sch0.id, floor_id=fl.id, name=name, b_in=9, D_in=18,
                             status='accepted', bars=[Bar(role=r, nos=n, dia=d, sort=0) for r, n, d in bars]))
    session.flush()

    new = sheet(session, proj, 'R1', floor=fl)
    assert R.link(session, new) == old
    b1 = beam(session, new, 'B1', 101, 201)                                # reads the same (within 3 pt)
    b2 = beam(session, new, 'B2', 100, 200, d=18)                          # the person had made it 24"; now reads 18"
    b3 = beam(session, new, 'B3', 300, 400)
    c = R.carry_over(session, new)
    assert (b1.status, b3.status, b2.status) == ('accepted', 'rejected', 'auto')
    assert 'Same as checked on R0' in b1.note and 'Marked not on drawing on R0' in b3.note
    assert 'Changed since R0: size 9"×18" (was 9"×24")' in b2.note and b2.confidence <= 0.3
    b4 = session.scalar(select(Member).where(Member.sheet_id == new.id, Member.name == 'B4'))
    assert b4.status == 'added' and 'Added by a person on R0' in b4.note
    assert c == dict(kept=2, changed=1, added=1) and '2 checked items carried over' in R.carried_note(c)

    sch1 = sheet(session, proj, 'R1', kind='beam_schedule', drg='MC-SD-06-01', floor=fl)
    assert R.link(session, sch1) == sch0
    s1 = BeamSpec(project_id=proj.id, sheet_id=sch1.id, floor_id=fl.id, name='B1', b_in=9, D_in=18, status='auto',
                  bars=[Bar(role='Bottom through', nos=2, dia=12, sort=0)])
    s2 = BeamSpec(project_id=proj.id, sheet_id=sch1.id, floor_id=fl.id, name='B2', b_in=9, D_in=18, status='auto',
                  bars=[Bar(role='Bottom through', nos=3, dia=12, sort=0)])
    session.add_all([s1, s2])
    session.flush()
    R.carry_over(session, sch1)
    assert s1.status == 'accepted' and s2.status == 'auto' and 'Changed since R0: bars' in s2.notes

    # Superseded sheets count nowhere: the floor's beams, Review and the stages use the new revision only.
    assert {m.sheet_id for m in floor_members(session, fl)} == {new.id}
    sign_in(client, 'new@example.com')
    q = client.get('/api/projects/site-b/review').json()
    assert {i['sheet']['id'] for i in q['items'] if i['item'] == 'member'} == {str(new.id)}
    assert {i['name'] for i in q['items']} == {'B2'}                       # only what changed is left to check
    old.layout_version = None
    stages.layout(session, None, old.id)                                   # a superseded sheet is not read again
    assert session.get(Sheet, old.id).layout_version is None
