import pytest

from akta_autosampler.config import CONFIG_DIR, load_gantry_config
from akta_autosampler.deck import Deck
from akta_autosampler.gantry import Gantry


@pytest.fixture
def config():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    cfg.simulate = True
    cfg.simulation.speedup = 40
    cfg.homing_mode = "sensorless"  # tests exercise the hard-stop homing; manual homing has its own tests
    cfg.axes["x"].homing_direction = -1  # the simulator tests assume X homes at its low motor end
    cfg.axes["z"].homing_direction = 1   # ... and Z at its high motor end
    cfg.speed_percent = 100
    cfg.arrive_tolerance_mm = 0.1  # the simulator tests check end positions closely (2 mm rule has its own test)
    return cfg


@pytest.fixture
def gantry(config):
    g = Gantry(config)
    assert g.connect()
    yield g
    g.disconnect()


@pytest.fixture
def homed(gantry):
    assert gantry.home_all()
    return gantry


@pytest.fixture
def deck():
    return Deck.load(CONFIG_DIR)
