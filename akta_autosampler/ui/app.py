"""
NiceGUI operator interface.

Header: mode, state, live XYZ readout, STOP.
Tabs:   Control (connect/home/jog/go-to), Deck (well map, teach),
        Sequence (load/build/run/pause/abort + log).

Blocking gantry calls run in a worker thread via run.io_bound so the UI
(and the STOP button) stay responsive.
"""

import json
import logging
from collections import deque
from dataclasses import dataclass
from itertools import count
from typing import Callable, Deque, Dict, List, Optional, Tuple

from nicegui import app, run, ui

from ..config import CONFIG_DIR
from ..deck import Deck
from ..gantry import Gantry
from ..sequence import RunnerState, Sequence, SequenceRunner

logger = logging.getLogger(__name__)

SEQUENCE_DIR = CONFIG_DIR / "sequences"


# ============================================================================
# SHARED STATE
# ============================================================================

class LogBuffer(logging.Handler):
    """Keeps recent log lines so every open page can show them."""

    def __init__(self, maxlen: int = 1000):
        super().__init__(level=logging.INFO)
        self.lines: Deque[Tuple[int, str]] = deque(maxlen=maxlen)
        self._seq = count(1)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append((next(self._seq), self.format(record)))
        except Exception:
            pass

    def since(self, seq: int) -> List[Tuple[int, str]]:
        return [item for item in list(self.lines) if item[0] > seq]


@dataclass
class Machine:
    gantry: Gantry
    deck: Deck
    runner: SequenceRunner
    log: LogBuffer
    busy: bool = False
    selected: Optional[Tuple[str, str]] = None  # (slot, well)


MACHINE: Optional[Machine] = None


# ============================================================================
# PAGE
# ============================================================================

@ui.page("/")
def index():
    m = MACHINE
    g, deck, runner = m.gantry, m.deck, m.runner
    lockable: List[ui.element] = []  # disabled while busy / sequence running

    async def act(fn: Callable, *args, fail: str = "Command failed", **kwargs):
        """Run a blocking gantry call in a worker thread, one at a time."""
        if m.busy or runner.is_active:
            ui.notify("Gantry is busy", type="warning")
            return None
        m.busy = True
        try:
            result = await run.io_bound(fn, *args, **kwargs)
        except Exception as e:
            logger.exception(fail)
            ui.notify(f"{fail}: {e}", type="negative")
            return None
        finally:
            m.busy = False
        if result is False:
            ui.notify(f"{fail} - see log", type="negative")
        return result

    def stop():
        runner.abort()
        g.stop()
        ui.notify("STOPPED", type="negative")

    def motors_off():
        runner.abort()
        g.stop(emergency=True)
        ui.notify("Motors de-energised - re-home before moving", type="warning")

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    with ui.header().classes("items-center gap-4 px-4 py-2 bg-slate-800"):
        ui.label("ÄKTA Autosampler").classes("text-lg font-bold")
        mode = ui.badge("SIM" if g.simulated else "HARDWARE",
                        color="orange" if g.simulated else "green")
        state_lbl = ui.label().classes("text-sm uppercase")
        homed_badge = ui.badge()
        ui.space()
        pos_lbls: Dict[str, ui.label] = {}
        with ui.row().classes("gap-4 font-mono text-lg"):
            for name in ("x", "y", "z"):
                pos_lbls[name] = ui.label()
        ui.space()
        ui.button("Motors off", on_click=motors_off).props("outline color=white").tooltip(
            "De-energise all motors (Z may drop if it has no brake)")
        ui.button("STOP", on_click=stop).props("color=negative size=lg").classes("font-bold px-8").tooltip(
            "Stop all motion and abort the sequence (motors hold position)")

    # Operator prompt banner (sequence pause steps)
    with ui.card().classes("w-full bg-amber-100 text-black") as banner:
        with ui.row().classes("items-center w-full"):
            ui.icon("pan_tool", size="md")
            banner_lbl = ui.label().classes("text-lg grow")
            ui.button("Resume", on_click=runner.resume).props("color=primary")
            ui.button("Abort", on_click=runner.abort).props("color=negative flat")
    banner.set_visibility(False)

    with ui.tabs().classes("w-full") as tabs:
        t_control = ui.tab("Control", icon="open_with")
        t_deck = ui.tab("Deck", icon="grid_on")
        t_seq = ui.tab("Sequence", icon="playlist_play")

    with ui.tab_panels(tabs, value=t_control).classes("w-full"):

        # --------------------------------------------------------------
        # CONTROL
        # --------------------------------------------------------------
        with ui.tab_panel(t_control):
            with ui.row().classes("w-full gap-4 items-start"):
                with ui.card():
                    ui.label("Connection & homing").classes("font-bold")
                    with ui.row():
                        lockable.append(ui.button("Connect", on_click=lambda: act(g.connect, fail="Connect failed")))
                        lockable.append(ui.button("Disconnect", on_click=lambda: act(g.disconnect)).props("flat"))
                    with ui.row():
                        lockable.append(ui.button("Home all", icon="home",
                                                  on_click=lambda: act(g.home_all, fail="Homing failed")))
                        for name in ("x", "y", "z"):
                            lockable.append(ui.button(f"Home {name.upper()}",
                                                      on_click=lambda n=name: act(g.home_axis, n, fail="Homing failed"))
                                            .props("flat"))
                    lockable.append(ui.button("Reset faults", icon="restart_alt",
                                              on_click=lambda: act(g.reset_all)).props("flat color=warning"))

                with ui.card():
                    ui.label("Jog").classes("font-bold")
                    step = ui.toggle({0.1: "0.1", 1.0: "1", 10.0: "10", 50.0: "50 mm"}, value=1.0)

                    def jog_btn(label, axis, sign):
                        b = ui.button(label, on_click=lambda: act(g.jog, axis, sign * step.value,
                                                                  fail="Jog failed"))
                        b.classes("w-20")
                        lockable.append(b)

                    with ui.row().classes("items-center gap-6"):
                        with ui.grid(columns=3).classes("gap-1"):
                            ui.label()
                            jog_btn("Y−", "y", -1)
                            ui.label()
                            jog_btn("X−", "x", -1)
                            ui.icon("open_with", size="lg").classes("self-center justify-self-center")
                            jog_btn("X+", "x", 1)
                            ui.label()
                            jog_btn("Y+", "y", 1)
                            ui.label()
                        with ui.column().classes("gap-1"):
                            jog_btn("Z ↑", "z", -1)
                            jog_btn("Z ↓", "z", 1)
                    ui.label("Jog works before homing (no limit checks until homed).").classes("text-xs text-gray-500")

                with ui.card():
                    ui.label("Go to (safe: Z up → XY → Z)").classes("font-bold")
                    with ui.row():
                        gx = ui.number("X mm", value=0, format="%.2f").classes("w-24")
                        gy = ui.number("Y mm", value=0, format="%.2f").classes("w-24")
                        gz = ui.number("Z mm", value=0, format="%.2f").classes("w-24")
                    with ui.row():
                        lockable.append(ui.button("Go", icon="near_me", on_click=lambda: act(
                            g.safe_move_to, gx.value, gy.value, gz.value, fail="Move failed")))
                        lockable.append(ui.button("Raise Z", icon="vertical_align_top",
                                                  on_click=lambda: act(g.raise_z, fail="Raise failed")).props("flat"))
                    ui.label("Named positions").classes("font-bold mt-2")
                    with ui.row():
                        for pname, xyz in deck.positions.items():
                            lockable.append(ui.button(pname, on_click=lambda p=xyz: act(
                                g.safe_move_to, *p, fail="Move failed")).props("outline"))

            with ui.card().classes("w-full"):
                ui.label("Axes").classes("font-bold")
                axis_table = ui.table(
                    columns=[{"name": c, "label": c.capitalize(), "field": c, "align": "left"}
                             for c in ("axis", "state", "position", "velocity", "torque", "homed", "errors")],
                    rows=[], row_key="axis").classes("w-full").props("dense flat")

        # --------------------------------------------------------------
        # DECK
        # --------------------------------------------------------------
        with ui.tab_panel(t_deck):
            well_buttons: Dict[Tuple[str, str], ui.button] = {}

            with ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("gap-4"):
                    for slot in deck.slots.values():
                        lw = slot.labware
                        with ui.card():
                            ui.label(f"{slot.name} — {lw.name}").classes("font-bold")
                            with ui.grid(columns=lw.cols + 1).classes("gap-0.5"):
                                ui.label()
                                for c in range(lw.cols):
                                    ui.label(str(c + 1)).classes("text-xs text-center text-gray-500")
                                for r in range(lw.rows):
                                    ui.label(lw.well_name(r, 0)[:-1]).classes("text-xs text-gray-500 self-center")
                                    for c in range(lw.cols):
                                        well = lw.well_name(r, c)
                                        b = ui.button(well, on_click=lambda s=slot.name, w=well: select_well(s, w))
                                        b.props("dense unelevated size=sm color=grey-4 text-color=black").classes(
                                            "min-w-[2.6rem]")
                                        x, y = deck.well_xy(slot.name, well)
                                        b.tooltip(f"{slot.name}:{well}  X={x:.2f} Y={y:.2f}")
                                        well_buttons[(slot.name, well)] = b

                with ui.card().classes("min-w-[22rem]"):
                    ui.label("Selected well").classes("font-bold")
                    sel_lbl = ui.label("Click a well").classes("text-lg font-mono")
                    sel_xyz = ui.label().classes("text-sm font-mono text-gray-600")

                    def need_sel() -> Optional[Tuple[str, str]]:
                        if not m.selected:
                            ui.notify("Select a well first", type="warning")
                        return m.selected

                    async def go_well(depth: str):
                        if sel := need_sel():
                            x, y, z = deck.well_xyz(*sel, depth)
                            await act(g.safe_move_to, x, y, z, fail="Move failed")

                    with ui.row():
                        lockable.append(ui.button("Go (top)", icon="near_me", on_click=lambda: go_well("top")))
                        lockable.append(ui.button("Lower to sample", icon="south", on_click=lambda: go_well("sample")))
                        lockable.append(ui.button("Raise", icon="north",
                                                  on_click=lambda: act(g.raise_z, fail="Raise failed")).props("flat"))
                    ui.button("Add to sample list", icon="playlist_add",
                              on_click=lambda: add_to_samples()).props("flat")

                    ui.separator()
                    ui.label("Teach (calibrate from current position)").classes("font-bold")
                    ui.label("Jog the needle onto the target first, then save.").classes("text-xs text-gray-500")

                    def teach_a1():
                        if sel := need_sel():
                            pos = g.get_position()
                            deck.teach_a1(sel[0], pos.x, pos.y)
                            deck.save()
                            refresh_tooltips()
                            show_selection()
                            ui.notify(f"{sel[0]} origin set to {deck.slots[sel[0]].origin_mm} (saved deck.json)")

                    def teach_z(which: str):
                        if sel := need_sel():
                            slot = deck.slot(sel[0])
                            z = round(g.get_position().z - slot.z_offset_mm, 3)
                            setattr(slot.labware, f"z_{which}_mm", z)
                            save_labware_z(slot.labware)
                            show_selection()
                            ui.notify(f"{slot.labware.name}: z_{which}_mm = {z} (saved)")

                    with ui.row():
                        ui.button("A1 is here", on_click=teach_a1).props("outline").tooltip(
                            "Current XY becomes well A1 of the selected well's slot")
                        ui.button("Z top here", on_click=lambda: teach_z("top")).props("outline")
                        ui.button("Z sample here", on_click=lambda: teach_z("sample")).props("outline")

        # --------------------------------------------------------------
        # SEQUENCE
        # --------------------------------------------------------------
        with ui.tab_panel(t_seq):
            current: Dict[str, Optional[Sequence]] = {"seq": None}

            with ui.row().classes("w-full gap-4 items-start"):
                with ui.column().classes("gap-4"):
                    with ui.card():
                        ui.label("Load sequence").classes("font-bold")

                        def seq_files() -> List[str]:
                            return sorted(p.name for p in SEQUENCE_DIR.glob("*.json"))

                        with ui.row().classes("items-center"):
                            file_sel = ui.select(seq_files(), label="File").classes("w-64")
                            ui.button(icon="refresh", on_click=lambda: file_sel.set_options(seq_files())).props("flat")
                            ui.button("Load", on_click=lambda: load_file(file_sel.value))

                    with ui.card():
                        ui.label("Sample list builder").classes("font-bold")
                        slot_names = list(deck.slots)
                        pos_names = list(deck.positions)
                        b_slot = ui.select(slot_names, value=slot_names[0] if slot_names else None, label="Slot")
                        b_wells = ui.input("Wells", placeholder="A1-A6, B1").classes("w-72")
                        with ui.row():
                            b_depth = ui.select(["sample", "top"], value="sample", label="Depth").classes("w-28")
                            b_dwell = ui.number("Dwell s", value=10, min=0).classes("w-24")
                        b_pause = ui.checkbox("Pause in each vial for ÄKTA (operator resumes)")
                        with ui.row():
                            b_wash = ui.select(["(none)"] + pos_names, value="(none)", label="Wash").classes("w-32")
                            b_wash_dwell = ui.number("Wash s", value=3, min=0).classes("w-24")
                            b_end = ui.select(["(none)"] + pos_names,
                                              value="park" if "park" in pos_names else "(none)",
                                              label="End at").classes("w-32")
                        b_home = ui.checkbox("Home first", value=True)
                        b_name = ui.input("Name", value="Sample run").classes("w-72")
                        with ui.row():
                            ui.button("Build", icon="build", on_click=lambda: build())
                            ui.button("Save", icon="save", on_click=lambda: save_current()).props("flat")

                with ui.card().classes("grow min-w-[24rem]"):
                    with ui.row().classes("items-center w-full"):
                        seq_title = ui.label("No sequence loaded").classes("font-bold grow")
                        run_state = ui.badge("idle")
                    with ui.row():
                        run_btn = ui.button("Run", icon="play_arrow", on_click=lambda: start_run()).props("color=positive")
                        pause_btn = ui.button("Pause", icon="pause", on_click=runner.pause)
                        resume_btn = ui.button("Resume", icon="play_arrow", on_click=runner.resume)
                        abort_btn = ui.button("Abort", icon="stop", on_click=runner.abort).props("color=negative")
                    run_msg = ui.label().classes("text-sm")
                    steps_box = ui.column().classes("gap-0 w-full max-h-[28rem] overflow-auto font-mono text-sm")
                    step_rows: List[ui.label] = []

            ui.label("Log").classes("font-bold mt-4")
            log_view = ui.log(max_lines=500).classes("w-full h-64")

    # ------------------------------------------------------------------
    # Deck helpers
    # ------------------------------------------------------------------
    painted: Dict[str, Optional[Tuple[str, str]]] = {"sel": None}

    def paint(key: Optional[Tuple[str, str]], on: bool):
        if key in well_buttons:
            b = well_buttons[key]
            b.props(remove="color text-color")
            b.props("color=primary text-color=white" if on else "color=grey-4 text-color=black")

    def select_well(slot: str, well: str):
        paint(painted["sel"], False)
        m.selected = (slot, well)
        painted["sel"] = m.selected
        paint(m.selected, True)
        show_selection()

    def show_selection():
        if not m.selected:
            return
        slot, well = m.selected
        tx, ty, tz = deck.well_xyz(slot, well, "top")
        sz = deck.well_xyz(slot, well, "sample")[2]
        sel_lbl.set_text(f"{slot}:{well}")
        sel_xyz.set_text(f"X={tx:.2f}  Y={ty:.2f}  Z top={tz:.2f}  Z sample={sz:.2f}")

    def refresh_tooltips():
        for (slot, well), b in well_buttons.items():
            x, y = deck.well_xy(slot, well)
            for child in b.default_slot.children:
                if isinstance(child, ui.tooltip):
                    child.text = f"{slot}:{well}  X={x:.2f} Y={y:.2f}"

    def save_labware_z(lw):
        path = CONFIG_DIR / "labware" / f"{lw.id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["z_top_mm"], data["z_sample_mm"] = lw.z_top_mm, lw.z_sample_mm
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def add_to_samples():
        if not m.selected:
            ui.notify("Select a well first", type="warning")
            return
        slot, well = m.selected
        if b_slot.value != slot:
            b_slot.value = slot
            b_wells.value = ""
        b_wells.value = f"{b_wells.value}, {well}" if b_wells.value else well
        ui.notify(f"Added {slot}:{well} to sample list")

    # ------------------------------------------------------------------
    # Sequence helpers
    # ------------------------------------------------------------------
    def show_sequence(seq: Optional[Sequence]):
        current["seq"] = seq
        steps_box.clear()
        step_rows.clear()
        seq_title.set_text(seq.name if seq else "No sequence loaded")
        if not seq:
            return
        with steps_box:
            for i, s in enumerate(seq.steps):
                step_rows.append(ui.label(f"{i + 1:>3}. {s.describe()}").classes("px-2 py-0.5 w-full"))

    def load_file(name: Optional[str]):
        if not name:
            ui.notify("Choose a file", type="warning")
            return
        try:
            show_sequence(Sequence.load(SEQUENCE_DIR / name, deck))
            ui.notify(f"Loaded {name}")
        except Exception as e:
            ui.notify(f"Load failed: {e}", type="negative")

    def build():
        try:
            spec = {"type": "samples", "slot": b_slot.value, "wells": b_wells.value or "",
                    "depth": b_depth.value, "dwell_s": b_dwell.value or 0,
                    "pause_in_sample": b_pause.value}
            if b_wash.value != "(none)":
                spec["wash"] = {"position": b_wash.value, "dwell_s": b_wash_dwell.value or 0}
            if b_end.value != "(none)":
                spec["end_position"] = b_end.value
            items = ([{"type": "home"}] if b_home.value else []) + [spec]
            seq = Sequence.from_dict({"name": b_name.value or "Sample run", "steps": items}, deck)
            if not any(s.type == "move_to_well" for s in seq.steps):
                raise ValueError("No wells - enter e.g. A1-A6")
            show_sequence(seq)
        except Exception as e:
            ui.notify(f"Build failed: {e}", type="negative")

    def save_current():
        seq = current["seq"]
        if not seq:
            ui.notify("Nothing to save", type="warning")
            return
        fname = "".join(c if c.isalnum() or c in "-_" else "_" for c in seq.name).strip("_") + ".json"
        seq.save(SEQUENCE_DIR / fname)
        file_sel.set_options(seq_files(), value=fname)
        ui.notify(f"Saved sequences/{fname}")

    def start_run():
        seq = current["seq"]
        if not seq:
            ui.notify("Load or build a sequence first", type="warning")
            return
        if m.busy:
            ui.notify("Gantry is busy", type="warning")
            return
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        if not g.is_homed and seq.steps and seq.steps[0].type != "home":
            ui.notify("Gantry not homed - home first or start the sequence with a home step", type="warning")
            return
        runner.start(seq)

    # ------------------------------------------------------------------
    # Periodic refresh (10 Hz)
    # ------------------------------------------------------------------
    last = {"log": 0, "index": None, "seq": None}

    def tick():
        st = g.get_status()
        pos = st["position"]
        for name in ("x", "y", "z"):
            pos_lbls[name].set_text(f"{name.upper()} {round(pos[name], 2) + 0.0:8.2f}")  # +0.0 drops "-0.00"
        state_lbl.set_text(st["state"] + (" · busy" if m.busy else ""))
        homed_badge.set_text("HOMED" if st["is_homed"] else "NOT HOMED")
        homed_badge.props(f"color={'green' if st['is_homed'] else 'red'}")
        axis_table.rows = [
            {"axis": n.upper(), "state": a["state"], "position": f"{a['position']:.2f}",
             "velocity": f"{a['velocity']:.1f}", "torque": f"{a['torque']:.2f}",
             "homed": "yes" if a["is_homed"] else "no", "errors": "; ".join(a["errors"][-2:])}
            for n, a in st["axes"].items()
        ]

        active = runner.is_active
        for el in lockable:
            el.set_enabled(not active and not m.busy)

        # Runner
        if runner.sequence is not None and runner.sequence is not current["seq"] and active:
            show_sequence(runner.sequence)
        run_state.set_text(runner.state.value)
        run_state.props(f"color={_STATE_COLORS[runner.state]}")
        run_btn.set_enabled(not active)
        pause_btn.set_enabled(runner.state == RunnerState.RUNNING)
        resume_btn.set_enabled(runner.state == RunnerState.PAUSED)
        abort_btn.set_enabled(active)
        if runner.state == RunnerState.PAUSED:
            run_msg.set_text(runner.message)
        elif runner.state in (RunnerState.FAILED, RunnerState.ABORTED):
            run_msg.set_text(runner.message)
        elif active and runner.sequence:
            run_msg.set_text(f"Step {runner.index + 1} of {len(runner.steps)}")
        else:
            run_msg.set_text("")
        banner.set_visibility(runner.state == RunnerState.PAUSED)
        banner_lbl.set_text(runner.message)

        idx = runner.index if current["seq"] is runner.sequence else None
        if idx != last["index"]:
            for i, row in enumerate(step_rows):
                row.classes(remove="bg-blue-200 text-black font-bold text-gray-400")
                if idx is not None and i == idx:
                    row.classes(add="bg-blue-200 text-black font-bold")
                elif idx is not None and i < idx:
                    row.classes(add="text-gray-400")
            last["index"] = idx

        for seq_no, line in m.log.since(last["log"]):
            log_view.push(line)
            last["log"] = seq_no

    ui.timer(0.1, tick)


_STATE_COLORS = {
    RunnerState.IDLE: "grey",
    RunnerState.RUNNING: "blue",
    RunnerState.PAUSED: "amber",
    RunnerState.COMPLETED: "green",
    RunnerState.ABORTED: "orange",
    RunnerState.FAILED: "red",
}


# ============================================================================
# ENTRY
# ============================================================================

def main(gantry: Gantry, deck: Deck, *, host: str = "127.0.0.1", port: int = 8080,
         native: bool = False, show: bool = True, auto_connect: bool = False):
    global MACHINE
    log = LogBuffer()
    logging.getLogger().addHandler(log)
    MACHINE = Machine(gantry, deck, SequenceRunner(gantry, deck), log)

    if auto_connect:
        app.on_startup(lambda: run.io_bound(gantry.connect))
    app.on_shutdown(lambda: gantry.disconnect() if gantry.is_connected else None)

    ui.run(host=host, port=port, title="ÄKTA Autosampler", native=native, show=show,
           reload=False, dark=None, favicon="🧪", show_welcome_message=True)
