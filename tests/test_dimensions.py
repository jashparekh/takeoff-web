"""Clear spans from drawn dimensions (dimensions.py): pairing text with its line, the line offset, beam matching."""
import pytest

from app.pipeline import dimensions as D
from app.pipeline.layout import Beam, Label

PPI = 0.6


def test_dimension_line_is_the_long_segment_beside_the_text():
    box = (100, 90, 114, 96)                               # text sits just above y = 96.3
    H = [(96.3, 60, 160), (96.4, 101, 104),                # the dimension line; a glyph stroke
         (120.0, 0, 300)]                                  # a beam edge further away
    assert D.dim_line(False, box, H, []) == (96.3, 60, 160)
    assert D.dim_line(False, box, [(96.3, 105, 160)], []) is None          # does not run past the text


def test_line_offset_is_the_median_gap():
    # Lines stop 4" short of each face on the pilot: written - measured ≈ 8; a misread far off is ignored.
    assert D.line_offset([(191, 183), (178, 170.4), (194, 186.2), (205, 137.4)]) == pytest.approx(7.8)
    assert D.line_offset([(191, 183)]) == 0.0                              # too few to tell


def beam(name, vertical, p1, p2, cant=False):
    return Beam(name, vertical, p1, p2, 9, Label(name, vertical, 0, 0, (0, 0, 0, 0)), 0.9, cantilever=cant)


def test_a_panel_dimension_serves_the_beams_on_both_sides():
    # Two horizontal beams 120" c/c long (72 pt) at y=100 and y=160; a 112" dimension between them.
    top, bot = beam('B1', False, (0, 100), (72, 100)), beam('B2', False, (0, 160), (72, 160))
    other = beam('B3', True, (0, 100), (0, 160))
    d = D.Dim(112, False, 130, 4.8, 67.2, '9\'-4"')
    got = D.assign([d], [top, bot, other], PPI)
    assert set(got) == {0, 1} and got[0].value_in == 112


def test_a_dimension_too_short_for_the_span_is_not_used():
    b = beam('B1', False, (0, 100), (150, 100))            # 250" c/c
    d = D.Dim(112, False, 110, 4.8, 67.2, '9\'-4"')       # one panel of a longer beam
    assert D.assign([d], [b], PPI) == {}
