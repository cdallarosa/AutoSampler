import pytest

from akta_autosampler.config import CONFIG_DIR, load_gantry_config
from akta_autosampler.deck import Deck
from akta_autosampler.gantry import Gantry


@pytest.fixture
def config():
    cfg = load_gantry_config(CONFIG_DIR / "gantry.json")
    cfg.simulate = True
    cfg.simulation.speedup = 40
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
