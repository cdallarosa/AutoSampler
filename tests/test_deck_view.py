"""Process-picture geometry: bottles drawn at true size/position, all inside the drawn frame."""

import re
from types import SimpleNamespace

from akta_autosampler.ui.deck_view import DeckView, plate_extent

INCH = 25.4


def _svg(config, deck):
    view = object.__new__(DeckView)  # skip the NiceGUI widgets, only build the SVG
    view.gantry, view.deck = SimpleNamespace(config=config), deck
    view.W, view.D = config.axes["x"].max_mm, config.axes["y"].max_mm
    view.plate = plate_extent(config)
    return view._svg()


def test_plate_is_the_stop_to_stop_area(config):
    (x0, x1), (y0, y1) = plate_extent(config)
    assert x0 < 0 < config.axes["x"].max_mm < x1
    assert y0 < 0 < config.axes["y"].max_mm < y1
    assert x1 - x0 >= config.axes["x"].max_mm and y1 - y0 >= config.axes["y"].max_mm


def test_bottles_true_size_and_inside_view(config, deck):
    svg = _svg(config, deck)
    vx, vy, vw, vh = map(float, re.search(r'viewBox="([^"]+)"', svg).group(1).split())
    circles = re.findall(r'<g id="b-([^"]+)"[^>]*>.*?<circle class="body" cx="([-\d.]+)" cy="([-\d.]+)" r="([\d.]+)"',
                         svg)
    assert len(circles) == sum(s.labware.rows * s.labware.cols for s in deck.slots.values())
    for key, cx, cy, r in circles:
        slot, well = key.split("-", 1)
        bx, by = deck.well_xy(slot, well)
        lw = deck.slot(slot).labware
        assert abs(float(cx) - bx) < 0.01 and abs(float(cy) - by) < 0.01
        assert abs(float(r) - lw.well_diameter_mm / 2) < 0.01
        # never clipped by the viewBox (A1 sits at home, so its bottle reaches past 0, 0)
        assert vx < float(cx) - float(r) and float(cx) + float(r) < vx + vw
        assert vy < float(cy) - float(r) and float(cy) + float(r) < vy + vh


def test_inch_rulers_label_whole_plate(config, deck):
    svg = _svg(config, deck)
    labels = re.findall(r'<text x="([-\d.]+)" y="[-\d.]+" text-anchor="middle" style="font-size:16px[^>]*>(\d+)"',
                        svg)
    assert labels and all(abs(float(x) - int(i) * INCH) < 0.05 for x, i in labels)
    (_, x1), _ = plate_extent(config)
    assert max(int(i) for _, i in labels) * INCH > x1 - 4 * INCH
