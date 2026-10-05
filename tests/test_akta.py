import io
import socket
import time

import pytest

from akta_autosampler.akta import AktaLink, load_akta_config, matches
from akta_autosampler.akta.backends import LabJackBackend
from akta_autosampler.config import CONFIG_DIR
from akta_autosampler.sequence import RunnerState, Sequence, SequenceRunner


def wait_for(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def akta_config():
    cfg = load_akta_config(CONFIG_DIR / "akta.json")
    cfg["simulation"] = {"equilibrate_s": 0.3, "sample_time_s": 0.3}
    cfg["poll_s"] = 0.05
    return cfg


@pytest.fixture
def sim_akta(akta_config):
    link = AktaLink(akta_config, simulate=True)
    assert link.connect()
    yield link
    link.disconnect()


# ----------------------------------------------------------------------------
# Conditions / config
# ----------------------------------------------------------------------------

def test_matches():
    assert matches(0, {"equals": 0})
    assert matches(True, {"equals": 1})
    assert matches(" Hold ", {"equals": "hold"})
    assert matches("Sample Application", {"contains": "sample"})
    assert matches(2, {"in": [1, 2]})
    assert matches(1, {"not_equals": 0})
    assert not matches(None, {"equals": 0})


def test_example_handshake_sequence_loads(deck, akta_config):
    seq = Sequence.load(CONFIG_DIR / "sequences" / "akta_handshake_example.json", deck, akta_config["handshake"])
    assert [s.type for s in seq.steps].count("signal_akta") == 8  # active + idle per vial


def test_bad_signal_source_rejected(tmp_path):
    bad = tmp_path / "akta.json"
    bad.write_text('{"signals": {"x": {"source": "modbus"}}}', encoding="utf-8")
    with pytest.raises(ValueError, match="source"):
        load_akta_config(bad)


# ----------------------------------------------------------------------------
# Simulated ÄKTA handshake
# ----------------------------------------------------------------------------

def test_sim_link_signals(sim_akta):
    assert wait_for(lambda: sim_akta.get("sample_request") == 0, 5)
    assert sim_akta.get("run_state") == "Hold"
    sim_akta.write("needle_ready", 0)
    assert wait_for(lambda: sim_akta.get("phase") == "Sample Application", 5)
    assert wait_for(lambda: sim_akta.get("sample_request") == 1, 5)


def test_inputs_are_read_only(sim_akta):
    with pytest.raises(Exception, match="not an output"):
        sim_akta.write("sample_request", 0)


def test_handshake_sequence_runs(homed, deck, sim_akta, akta_config):
    seq = Sequence.from_dict({"name": "hs", "steps": [
        {"type": "samples", "slot": "bottles", "wells": "A1-A2", "akta_handshake": True,
         "end_position": "park"}]}, deck, akta_config["handshake"])
    types = [s.type for s in seq.steps]
    assert types[:6] == ["move_to_well", "wait_for_akta", "lower", "signal_akta", "wait_for_akta", "signal_akta"]

    runner = SequenceRunner(homed, deck, sim_akta)
    runner.start(seq)
    assert wait_for(lambda: not runner.is_active, 60)
    assert runner.state == RunnerState.COMPLETED, runner.message
    assert sim_akta.backends["sim"].samples_done == 2
    assert sim_akta.get("needle_ready") == 1


def test_abort_during_handshake_resets_outputs(homed, deck, sim_akta, akta_config):
    sim = sim_akta.backends["sim"]
    sim.sample_time_s = 30  # ÄKTA "loads" for a long time
    seq = Sequence.from_dict({"steps": [
        {"type": "samples", "slot": "bottles", "wells": ["B1"], "akta_handshake": True}]},
        deck, akta_config["handshake"])
    runner = SequenceRunner(homed, deck, sim_akta)
    runner.start(seq)
    assert wait_for(lambda: sim_akta.get("needle_ready") == 0, 20)
    runner.abort()
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.ABORTED
    assert sim_akta.get("needle_ready") == 1  # ÄKTA told the needle is no longer ready
    assert homed.get_position().z > 50  # STOP: needle left down where it was, no automatic move


def test_wait_timeout_fails(homed, deck, sim_akta):
    seq = Sequence.from_dict({"steps": [
        {"type": "wait_for_akta", "signal": "run_state", "equals": "End", "timeout_s": 0.3}]}, deck)
    runner = SequenceRunner(homed, deck, sim_akta)
    runner.start(seq)
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.FAILED
    assert "Timed out" in runner.message


def test_akta_steps_need_link(homed, deck):
    runner = SequenceRunner(homed, deck, akta=None)
    runner.start(Sequence.from_dict({"steps": [
        {"type": "signal_akta", "signal": "needle_ready", "value": 0}]}, deck))
    assert wait_for(lambda: not runner.is_active)
    assert runner.state == RunnerState.FAILED


# ----------------------------------------------------------------------------
# OPC UA backend against a local asyncua server (stand-in for UNICORN)
# ----------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def opc_server():
    from asyncua.sync import Server

    port = _free_port()
    server = Server()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}/unicorn/")
    idx = server.register_namespace("urn:test:unicorn")
    system = server.nodes.objects.add_object(idx, "AKTA pure")
    run_state = system.add_variable(f"ns={idx};s=System.RunState", "RunState", "Running")
    digital_out = system.add_variable(f"ns={idx};s=System.DigitalOut1", "Digital out 1", 1)
    digital_in = system.add_variable(f"ns={idx};s=System.DigitalIn1", "Digital in 1", 1)
    digital_in.set_writable()
    server.start()
    yield {"endpoint": f"opc.tcp://127.0.0.1:{port}/unicorn/", "idx": idx,
           "run_state": run_state, "digital_out": digital_out, "digital_in": digital_in}
    server.stop()


def test_opcua_backend_polls_and_writes(opc_server):
    idx = opc_server["idx"]
    cfg = {
        "poll_s": 0.05,
        "opcua": {"endpoint": opc_server["endpoint"]},
        "signals": {
            "run_state": {"source": "opcua", "node": f"ns={idx};s=System.RunState"},
            "sample_request": {"source": "opcua", "node": f"ns={idx};s=System.DigitalOut1"},
            "needle_ready": {"source": "opcua", "node": f"ns={idx};s=System.DigitalIn1",
                             "output": True, "idle": 1, "type": "Int64"},
        },
    }
    link = AktaLink(cfg)
    try:
        assert link.connect()
        assert wait_for(lambda: link.get("run_state") == "Running", 5)
        opc_server["digital_out"].write_value(0)
        opc_server["run_state"].write_value("Hold")
        assert link.wait_for("sample_request", {"equals": 0}, timeout=5)
        assert link.get("run_state") == "Hold"
        link.write("needle_ready", 0)
        assert opc_server["digital_in"].read_value() == 0
        assert link.status()["backends"]["opcua"]["connected"]
    finally:
        link.disconnect()


def test_opcua_browse_lists_nodes(opc_server):
    from akta_autosampler.tools.opcua_browse import browse

    out = io.StringIO()
    browse(opc_server["endpoint"], out, find="digital")
    text = out.getvalue()
    assert "System.DigitalOut1" in text
    assert "System.DigitalIn1" in text and "RW" in text
    assert "RunState" not in text.split("# path")[1]  # filtered out by --find


# ----------------------------------------------------------------------------
# LabJack backend (LJM mocked)
# ----------------------------------------------------------------------------

def test_labjack_open_drain(monkeypatch):
    from labjack import ljm

    calls = []
    monkeypatch.setattr(ljm, "eWriteName", lambda h, name, v: calls.append(("write", name, v)))
    monkeypatch.setattr(ljm, "eReadName", lambda h, name: calls.append(("read", name)) or 1)
    monkeypatch.setattr(ljm, "eReadNames", lambda h, n, names: [0] * n)

    lj = LabJackBackend("T4")
    lj._handle = 1
    lj.connected = True
    out = {"line": "FIO5", "output": True, "drive": "open_drain"}
    lj.write(out, 0)
    lj.write(out, 1)
    assert calls == [("write", "FIO5", 0), ("read", "FIO5")]  # 1 = release line (high-Z)

    calls.clear()
    lj.write({"line": "FIO6", "drive": "push_pull"}, 1)
    assert calls == [("write", "FIO6", 1)]

    # Output lines are never polled (a read would release them)
    values = lj.read_many({"req": {"line": "FIO4"}, "ready": out})
    assert values == {"req": 0}


def test_u3_line_index():
    assert LabJackBackend._u3_index("FIO4") == 4
    assert LabJackBackend._u3_index("EIO0") == 8
    assert LabJackBackend._u3_index("CIO1") == 17


def test_handshake_dwell_comes_after_akta_done(deck, akta_config):
    """With the handshake the ÄKTA sets the pace: ready is signalled at once, dwell only after 'done'."""
    seq = Sequence.from_dict({"steps": [
        {"type": "samples", "slot": "bottles", "wells": ["A1"], "dwell_s": 5, "akta_handshake": True}]},
        deck, akta_config["handshake"])
    types = [s.type for s in seq.steps]
    assert types == ["move_to_well", "wait_for_akta", "lower", "signal_akta", "wait_for_akta",
                     "dwell", "signal_akta", "raise"]
