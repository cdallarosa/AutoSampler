import pytest


def test_plate_corner_wells(deck):
    slot = deck.slot("plate1")
    ox, oy = slot.origin_mm
    ax, ay = slot.labware.a1_offset_mm
    assert deck.well_xy("plate1", "A1") == pytest.approx((ox + ax, oy + ay))
    assert deck.well_xy("plate1", "H12") == pytest.approx((ox + ax + 11 * 9.0, oy + ay + 7 * 9.0))


def test_well_z_depths(deck):
    lw = deck.slot("rack1").labware
    assert deck.well_xyz("rack1", "B2", "top")[2] == lw.z_top_mm
    assert deck.well_xyz("rack1", "B2", "sample")[2] == lw.z_sample_mm


def test_invalid_wells(deck):
    with pytest.raises(ValueError):
        deck.well_xy("rack1", "E1")  # rack has 4 rows
    with pytest.raises(ValueError):
        deck.well_xy("rack1", "A7")
    with pytest.raises(ValueError):
        deck.well_xy("nope", "A1")


def test_expand_wells(deck):
    lw = deck.slot("rack1").labware
    assert lw.expand_wells("A5-B2, D6") == ["A5", "A6", "B1", "B2", "D6"]
    assert lw.expand_wells("a1") == ["A1"]


def test_all_wells_inside_gantry_limits(deck, config):
    for slot in deck.slots.values():
        for well in slot.labware.wells():
            x, y, z = deck.well_xyz(slot.name, well, "sample")
            assert config.axes["x"].min_mm <= x <= config.axes["x"].max_mm
            assert config.axes["y"].min_mm <= y <= config.axes["y"].max_mm
            assert config.axes["z"].min_mm <= z <= config.axes["z"].max_mm


def test_teach_a1(deck):
    deck.teach_a1("rack1", 40.0, 50.0)
    assert deck.well_xy("rack1", "A1") == pytest.approx((40.0, 50.0))
