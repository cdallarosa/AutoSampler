"""
NiceGUI operator interface, laid out like UNICORN 7 System Control.

  title bar   module tabs: System Control | Sample Manager | Method Editor | Administration
  menu bar    File / View / Manual / System / ÄKTA / Help
  toolbar     Play / Pause / Continue / End, Home, Raise, Park, Connect, Reset ... Motors off, STOP
  systems     Autosampler / ÄKTA status tabs
  banners     not connected / not homed · operator pause · stopped (recovery)
  System Control
    Process Picture (2D deck)   Run order + run status / Run Log   Manual instructions
  status bar  run, step, gantry, ÄKTA, mode

Safety rules in the UI:
  - Play / Create & Run ask for confirmation (summary of what will happen).
  - End is graceful (finish the current bottle, raise, park) and asks first.
  - STOP halts in place; nothing moves again until the operator picks an
    action in the recovery banner.
  - Positioning controls are disabled until the gantry is homed.

Blocking gantry calls run in a worker thread via run.io_bound so the UI
(and STOP) stay responsive.
"""

import copy
import inspect
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from nicegui import app, run, ui

from ..akta import AktaLink
from ..config import CONFIG_DIR, save_turns_per_mm
from ..deck import Deck
from ..gantry import Gantry
from ..samples import KINDS, ROLES, SAMPLE_LIST_DIR, BottleRegistry, SampleQueue, pos_key, split_key
from ..tuning import GAIN_LIMITS, delete_preset, load_presets, save_preset
from ..sequence import MoveToPositionStep, RunnerState, Sequence, SequenceRunner, resume_from, where
from .deck_view import DeckView, bottle_key

logger = logging.getLogger(__name__)

SEQUENCE_DIR = CONFIG_DIR / "sequences"
MM_PER_IN = 25.4

_STATE_COLORS = {
    RunnerState.IDLE: "grey-6",
    RunnerState.RUNNING: "green-7",
    RunnerState.PAUSED: "amber-8",
    RunnerState.COMPLETED: "teal-7",
    RunnerState.ENDED: "teal-7",
    RunnerState.ABORTED: "red-7",
    RunnerState.FAILED: "red-7",
}

CSS = """
html, body { background: #e9e9e9; font-family: 'Segoe UI', Tahoma, Arial, sans-serif; font-size: 13px; color: #1f1f1f; }
/* Page = exactly one window: header rows + banners at their natural height, the module panel takes
   the rest and scrolls inside itself; the bottom padding keeps content clear of the fixed status bar. */
.nicegui-layout .q-page { min-height: 0 !important; }
.nicegui-content { padding: 0 0 26px 0 !important; gap: 0 !important; height: 100vh; height: 100dvh;
                   flex-wrap: nowrap !important; overflow: hidden; }
.nicegui-content > * { flex-shrink: 0; }
.nicegui-content > .u-modules { flex: 1 1 0; min-height: 0; }
.u-modules > .q-panel { height: 100%; }
.u-fill { height: 100%; }
.u-sys-deck { flex: 0 0 42%; min-width: 420px; }
.u-sm-positions { width: 440px; }
.u-admin-main { flex: 1 1 640px; }
/* Narrower than 1280 px: the middle column drops below and the panel scrolls */
@media (max-width: 1279px) {
  .u-sys-row, .u-sm-row { flex-wrap: wrap !important; align-content: flex-start; }
  .u-sys-row > .u-sys-deck { flex: 1 1 0; min-width: 0; }
  .u-sys-row > .u-sys-runcol { order: 3; flex: 0 0 100%; }
  .u-sm-row > .u-sm-positions { order: 3; flex: 0 0 100%; width: 100%; }
  .u-admin-row > .u-admin-side { flex: 1 1 100%; }
}
.u-titlebar { background: #ffffff; border-bottom: 1px solid #d0d0d0; height: 40px; }
.u-brand { font-size: 17px; font-weight: 700; color: #2b2b2b; letter-spacing: .02em; }
.u-module { color: #6b6b6b; font-size: 15px; }
.u-menubar { background: #f7f7f7; border-bottom: 1px solid #dadada; height: 28px; }
.u-menubar .q-btn { font-size: 13px; color: #262626; min-height: 26px; padding: 0 10px; }
.u-toolbar { background: #f7f7f7; border-bottom: 1px solid #cfcfcf; height: 38px; }
.u-toolbar .q-btn { color: #3a3a3a; }
.u-systems { background: #e3e3e3; border-bottom: 1px solid #c4c4c4; height: 30px; }
.u-systab { background: #ffffff; border: 1px solid #bdbdbd; border-bottom: none; border-radius: 4px 4px 0 0;
            padding: 3px 12px; font-weight: 600; font-size: 12px; margin-top: 4px; }
.u-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.u-pane { background: #ffffff; border: 1px solid #c3c3c3; display: flex; flex-direction: column; min-height: 0; }
.u-pane-title { background: linear-gradient(#f6f6f6, #e6e6e6); border-bottom: 1px solid #c9c9c9; font-weight: 600;
                font-size: 12px; padding: 4px 8px; display: flex; align-items: center; gap: 6px; color: #2b2b2b; }
.u-pane-body { padding: 8px; flex: 1; min-height: 0; overflow: auto; }
.u-value { background: #1c1c1c; color: #ffffff; border-radius: 4px; padding: 4px 9px; line-height: 1.15; min-width: 104px; }
.u-value .lbl { font-size: 11px; color: #cfcfcf; }
.u-value .num { font-size: 19px; font-weight: 700; font-family: 'Segoe UI', Arial, sans-serif; }
.u-value .unit { font-size: 11px; color: #cfcfcf; margin-left: 4px; }
.u-dialog { background: #ffffff; border: 1px solid #8a8a8a; border-radius: 4px; box-shadow: 0 6px 18px rgba(0,0,0,.25);
            max-width: calc(100vw - 24px); max-height: calc(100vh - 24px); overflow: auto; }
.u-dialog-title { background: #474747; color: #ffffff; font-weight: 600; padding: 6px 10px; border-radius: 3px 3px 0 0;
                  display: flex; align-items: center; }
.u-group { border: 1px solid #d6d6d6; border-radius: 3px; padding: 8px; background: #fbfbfb; }
.u-group-title { font-size: 11px; font-weight: 700; color: #4a4a4a; text-transform: uppercase; letter-spacing: .04em; }
.u-set.q-btn { background: #1d6b45 !important; color: #fff !important; font-weight: 600; min-width: 64px; }
.u-btn.q-btn { background: #ffffff !important; color: #262626 !important; border: 1px solid #b9b9b9; }
.q-btn.disabled, .q-btn[disabled] { opacity: .4 !important; filter: grayscale(.6); }
.u-danger.q-btn { background: #c62828 !important; color: #fff !important; font-weight: 600; }
.u-banner { width: 100%; display: flex; align-items: center; gap: 12px; padding: 7px 16px; font-weight: 600; }
.u-banner-info { background: #e8f1fb; border-bottom: 1px solid #8fb6de; color: #0b3d6b; }
.u-banner-pause { background: #fff4d6; border-bottom: 1px solid #e0b84f; color: #6d4c00; }
.u-banner-stop { background: #fde7e7; border-bottom: 1px solid #e08a8a; color: #8a1c1c; }
.u-statusbar { background: #f4f4f4; border-top: 1px solid #c4c4c4; height: 26px; font-size: 12px; color: #333;
               overflow: hidden; white-space: nowrap; }
.u-statusbar > * { flex-shrink: 0; }
.u-statusbar > .truncate { flex-shrink: 1; min-width: 0; }
.u-statusbar .sep { width: 1px; height: 16px; background: #c4c4c4; }
.u-log { font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; background: #ffffff; border: none; }
.u-run-status { background: #f3f8f4; border: 1px solid #cfe3d6; border-radius: 3px; padding: 5px 8px; font-size: 12.5px; }
.u-step-current { background: #dff0d8; font-weight: 700; }
.u-step-done { color: #9a9a9a; }
.u-row-wash { background: #eaf4fb !important; }
.u-row-blank { background: #f4f4f4 !important; }
.u-qbtn.q-btn { min-width: 44px; width: 44px; height: 44px; border-radius: 50%; font-weight: 700; font-size: 12px; }
.ag-theme-balham { --ag-font-family: 'Segoe UI', Tahoma, Arial, sans-serif; --ag-font-size: 12px; }
.ag-theme-balham .ag-checkbox-input-wrapper { transform: scale(1.25); }
.q-field--dense .q-field__control { min-height: 30px; }
"""


# ============================================================================
# SHARED STATE
# ============================================================================

class LogBuffer(logging.Handler):
    """Keeps recent log lines so every open page can show them."""

    def __init__(self, maxlen: int = 1000):
        super().__init__(level=logging.INFO)
        self.lines: Deque[Tuple[int, str]] = deque(maxlen=maxlen)
        self._seq = count(1)
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        # Only this app's messages; asyncio/uvicorn noise (e.g. browser reconnects) stays in the console
        if not record.name.startswith("akta_autosampler"):
            return
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
    akta: Optional[AktaLink] = None
    registry: Optional[BottleRegistry] = None
    queue: Optional[SampleQueue] = None
    samples_version: int = 0  # bumped on any registry/queue change so every page refreshes
    queue_undo: List[Tuple[str, list]] = field(default_factory=list)  # snapshots for Undo
    busy: bool = False
    selected: Optional[Tuple[str, str]] = None  # (slot, well)


MACHINE: Optional[Machine] = None


def pane(title: str, classes: str = "", icon: Optional[str] = None):
    """A UNICORN-style docking pane: grey title bar + white body. Returns (pane, body)."""
    with ui.element("div").classes(f"u-pane {classes}") as p:
        with ui.element("div").classes("u-pane-title"):
            if icon:
                ui.icon(icon, size="16px").classes("text-gray-600")
            ui.label(title)
        body = ui.element("div").classes("u-pane-body")
    return p, body


def group(title: str):
    with ui.element("div").classes("u-group w-full flex flex-col gap-2") as g:
        ui.label(title).classes("u-group-title")
    return g


def set_button(text: str, on_click, icon: Optional[str] = None):
    return ui.button(text, icon=icon, on_click=on_click).props("unelevated dense no-caps").classes("u-set px-3")


def plain_button(text: str, on_click, icon: Optional[str] = None):
    return ui.button(text, icon=icon, on_click=on_click) \
        .props("unelevated dense no-caps color=white text-color=grey-10").classes("u-btn px-3")


def value_box(label: str):
    with ui.element("div").classes("u-value"):
        ui.label(label).classes("lbl")
        with ui.row().classes("items-baseline gap-0"):
            num = ui.label("—").classes("num")
            unit = ui.label().classes("unit")
    return num, unit


def well_label(slot: str, well: str) -> str:
    return "Sample position" if slot == "sample" else f"Bottle {well}"


def attr(text: str) -> str:
    """Make text safe inside a Quasar prop value."""
    return text.replace('"', "'")


# ============================================================================
# PAGE
# ============================================================================

@ui.page("/")
def index():
    m = MACHINE
    g, deck, runner, akta = m.gantry, m.deck, m.runner, m.akta
    reg, queue = m.registry, m.queue
    handshake = akta.handshake if akta else None
    lockable: List[Tuple[ui.element, bool]] = []  # (element, needs_homed): disabled while busy / running
    current: Dict[str, Any] = {"seq": None, "from_queue": None}  # loaded method; queue version it was built from
    last: Dict[str, Any] = {"log": 0, "index": None, "akta": None, "n": 0, "ver": -1, "ro": None,
                            "ro_key": None, "roles": None, "dismissed": None, "moving": False, "homed": None}

    ui.dark_mode(False)
    ui.colors(primary="#1d6b45", secondary="#0b5a8f", positive="#2e7d32", negative="#c62828",
              warning="#ef8f00", info="#0b5a8f")
    ui.add_css(CSS)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def act(fn: Callable, *args, fail: str = "Command failed", **kwargs):
        """Run a blocking gantry call in a worker thread, one at a time."""
        if m.busy or runner.is_active:
            ui.notify("The gantry is busy", type="warning")
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
            ui.notify(f"{fail} - see the Run Log", type="negative")
        return result

    def lock(el, needs_homed: bool = False):
        lockable.append((el, needs_homed))
        return el

    dialog_host = ui.element("div")  # stable parent for dialogs (banners re-render while visible)
    deck_view: Optional[DeckView] = None

    def confirm(title: str, lines: List[str], ok_text: str, on_ok: Callable, danger: bool = False):
        """UNICORN-style confirmation dialog."""
        dialog_host.clear()
        # persistent: a stray click outside must not silently cancel; close only via the buttons
        with dialog_host, ui.dialog().props("persistent") as dlg, ui.element("div").classes("u-dialog w-[440px]"):
            with ui.element("div").classes("u-dialog-title"):
                ui.label(title)
            with ui.column().classes("p-4 gap-1 w-full"):
                for line in lines:
                    ui.label(line).classes("text-[13px]")
                with ui.row().classes("w-full justify-end gap-2 mt-3"):
                    plain_button("Cancel", dlg.close)

                    async def ok():
                        dlg.close()
                        r = on_ok()
                        if inspect.isawaitable(r):
                            await r
                    b = ui.button(ok_text, on_click=ok).props("unelevated dense no-caps") \
                        .classes(("u-danger" if danger else "u-set") + " px-4")
                    b.props("autofocus")
        dlg.open()

    HOME_CORNER = "X at the left end, Y at the top end (row A side), needle up"

    def set_home_ui(names: Optional[List[str]] = None):
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        which = ", ".join(n.upper() for n in (names or ["x", "y", "z"]))
        confirm(f"Set home here - {which}",
                [f"Make the current position of {which} home (0)?",
                 f"The gantry must be at the home corner: {HOME_CORNER}.",
                 "Afterwards check with a 10 mm jog that X+ / Y+ move AWAY from that corner. If one goes the "
                 "wrong way, press STOP and tell us (its homing_direction gets flipped)."],
                "Set home", lambda: act(g.set_home_here, names, fail="Set home failed"))

    def calibrate_all_ui():
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        confirm("Calibrate (encoder offset)",
                ["Runs the start-up calibration on every axis that needs it (needed after each power-up).",
                 "Each motor TURNS slightly back and forth (a few mm) - make sure nothing is in the way.",
                 "", "STOP aborts it. Run it now?"],
                "Calibrate", lambda: act(g.prepare_all, fail="Calibration failed"))

    def find_limits_ui():
        """Limit search per axis: stall into the home-end stop (-> 0), then the far stop (-> max). Z only if ticked."""
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        dialog_host.clear()
        with dialog_host, ui.dialog().props("persistent") as dlg, ui.element("div").classes("u-dialog w-[500px]"):
            with ui.element("div").classes("u-dialog-title"):
                ui.label("Find limits (torque)")
            with ui.column().classes("p-4 gap-2 w-full"):
                ui.label("Each ticked axis drives slowly into its home-end stop (that becomes 0 after a short "
                         "back-off), then to its far stop; the measured travel becomes its soft limit (saved to "
                         "gantry.json). Z goes first and is raised again afterwards; X and Y are measured together.") \
                    .classes("text-[13px]")
                ticks = {}
                with ui.row().classes("gap-4"):
                    for n in ("x", "y", "z"):
                        stand_in = n in g.simulated_axes and not g.simulated
                        ticks[n] = ui.checkbox(n.upper() + (" (stand-in)" if stand_in else ""), value=n != "z")
                z_warn = ui.label("Z: its far stop is the BOTTOM - the needle is driven down until it stalls. "
                                  "Only with nothing under the needle.") \
                    .classes("text-[12px] font-semibold text-red-700")
                z_warn.bind_visibility_from(ticks["z"], "value")
                ui.label("Keep a hand on STOP. About a minute per stage.").classes("text-[12px] text-gray-600")

                async def run_it():
                    names = [n for n, cb in ticks.items() if cb.value]
                    if not names:
                        ui.notify("Tick at least one axis", type="warning")
                        return
                    dlg.close()
                    found = await act(g.find_limits, names, CONFIG_DIR / "gantry.json", fail="Limit search failed")
                    if found:
                        ui.notify("Limits found and saved: " + ", ".join(f"{n.upper()} 0-{v:.1f} mm"
                                                                         for n, v in found.items()),
                                  type="positive", multi_line=True, timeout=15000)
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    plain_button("Cancel", dlg.close)
                    set_button("Find limits", run_it, icon="straighten")
        dlg.open()

    def home_ui():
        if g.manual_homing:
            set_home_ui(None)
        else:
            return act(g.home_all, fail="Homing failed")

    def home_axis_ui(name: str):
        if g.manual_homing:
            set_home_ui([name])
        else:
            return act(g.home_axis, name, fail="Homing failed")

    def needs_calibration() -> List[str]:
        return [n.upper() for n, a in g.axes.items()
                if g.is_connected and not a.is_prepared and n not in g.simulated_axes]

    def stop():
        if runner.is_active:
            runner.abort()  # also stops the gantry
            ui.notify("STOPPED - nothing will move until you choose an action", type="negative", position="top")
        else:
            g.stop()
            ui.notify("Stopped", type="negative", position="top")

    def motors_off():
        def do():
            runner.abort()
            g.stop(emergency=True)
            ui.notify("Motors de-energised - home before moving", type="warning")
        confirm("Motors off", ["De-energise all motors now?",
                               "Z may drop if it has no brake. The gantry must be homed again afterwards."],
                "Motors off", do, danger=True)

    def confirm_end():
        if not runner.is_active:
            return
        confirm("End run", ["End the run now?",
                            "The needle finishes the bottle it is in (if any), raises, and the gantry parks.",
                            "Use STOP instead for an immediate halt."],
                "End run", runner.end)

    # ------------------------------------------------------------------
    # Title bar + module tabs
    # ------------------------------------------------------------------
    with ui.row().classes("u-titlebar w-full items-center px-3 gap-3 flex-nowrap"):
        ui.icon("precision_manufacturing", size="24px").classes("text-[#1d6b45]")
        ui.label("AUTOSAMPLER").classes("u-brand")
        ui.label("|").classes("text-gray-300")
        module_lbl = ui.label("System Control").classes("u-module")
        ui.space()

        def on_module(e):
            module_lbl.set_text(e.value)
            if e.value == "System Control" and deck_view is not None:  # DOM re-created: restore marks/position
                ui.timer(0.3, deck_view.reapply, once=True)

        with ui.tabs(on_change=on_module) \
                .props("dense no-caps active-color=primary indicator-color=primary") \
                .classes("text-gray-600") as modules:
            t_sys = ui.tab("System Control", icon="monitor")
            t_samples = ui.tab("Sample Manager", icon="science")
            t_method = ui.tab("Method Editor", icon="edit_note")
            t_admin = ui.tab("Administration", icon="admin_panel_settings")

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------
    def menu(title: str, items: List[Tuple[str, Optional[Callable]]]):
        with ui.button(title).props("flat dense no-caps"):
            with ui.menu().props("square"):
                for text, fn in items:
                    if text == "-":
                        ui.separator()
                    else:
                        ui.menu_item(text, on_click=fn)

    with ui.row().classes("u-menubar w-full items-center px-1 gap-0"):
        menu("File", [("Sample Manager…", lambda: modules.set_value(t_samples)),
                      ("Open method…", lambda: modules.set_value(t_method)),
                      ("Save method", lambda: save_current())])
        menu("View", [("Clear bottle selection", lambda: select_well(None, None)),
                      ("Clear Run Log", lambda: log_view.clear())])
        menu("Manual", [("Calibrate (encoder offset)…", calibrate_all_ui),
                        ("Find limits (torque)…", find_limits_ui),
                        ("Home all axes" if not g.manual_homing else "Set home here (all)…", home_ui),
                        ("Home X" if not g.manual_homing else "Set home here: X…", lambda: home_axis_ui("x")),
                        ("Home Y" if not g.manual_homing else "Set home here: Y…", lambda: home_axis_ui("y")),
                        ("Home Z" if not g.manual_homing else "Set home here: Z…", lambda: home_axis_ui("z")),
                        ("-", None),
                        ("Raise needle to safe height", lambda: act(g.raise_z, fail="Raise failed")),
                        ("Go to sample position", lambda: go_slot("sample", "A1", "top")),
                        ("Park", lambda: go_named("park"))])
        menu("System", [("Connect", lambda: act(g.connect, fail="Connect failed")),
                        ("Disconnect", lambda: act(g.disconnect)),
                        ("Reset faults", lambda: act(g.reset_all)),
                        ("-", None),
                        ("Calibration…", lambda: modules.set_value(t_admin)),
                        ("-", None),
                        ("Motors off…", motors_off)])
        menu("ÄKTA", [("Connect link", lambda: run.io_bound(akta.connect) if akta else None),
                      ("Reset outputs to idle", lambda: run.io_bound(akta.reset_outputs) if akta else None),
                      ("Link status…", lambda: modules.set_value(t_admin))])
        menu("Help", [("About", lambda: ui.notify("ÄKTA Autosampler - XYZ gantry, 25 × 20 in deck"))])

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------
    def tool(icon: str, tip: str, on_click, color: str = "grey-9"):
        return ui.button(icon=icon, on_click=on_click) \
            .props(f'flat dense color={color} aria-label="{attr(tip)}"').tooltip(tip)

    with ui.row().classes("u-toolbar w-full items-center px-2 gap-1 flex-nowrap"):
        tb_run = tool("play_arrow", "Run method (asks first)", lambda: start_run(), "green-8")
        tb_pause = tool("pause", "Pause after the current step", runner.pause)
        tb_cont = tool("play_circle", "Continue", runner.resume, "green-8")
        tb_end = tool("stop", "End run: finish the current bottle, raise, park (asks first)", confirm_end, "red-8")
        ui.separator().props("vertical").classes("mx-2")
        lock(tool("home", "Home all axes" if not g.manual_homing else "Set home here", home_ui))
        lock(tool("vertical_align_top", "Raise needle to safe height",
                  lambda: act(g.raise_z, fail="Raise failed")), needs_homed=True)
        lock(tool("local_parking", "Park", lambda: go_named("park")), needs_homed=True)
        lock(tool("link", "Connect", lambda: act(g.connect, fail="Connect failed")))
        lock(tool("restart_alt", "Reset faults", lambda: act(g.reset_all)))
        ui.separator().props("vertical").classes("mx-2")
        tb_method = ui.label("No method").classes("text-gray-600 truncate min-w-0")
        ui.space()
        ui.button("Motors off", icon="power_settings_new", on_click=motors_off) \
            .props('flat dense no-caps color=grey-8 aria-label="Motors off"')
        ui.button("STOP", icon="pan_tool", on_click=stop) \
            .props('unelevated dense no-caps color=negative aria-label="STOP"').classes("font-bold px-4") \
            .tooltip("Halt all motion immediately and stop the run. Nothing moves until you choose.")

    # ------------------------------------------------------------------
    # Systems strip
    # ------------------------------------------------------------------
    with ui.row().classes("u-systems w-full items-end px-2 gap-1"):
        with ui.row().classes("u-systab items-center gap-2"):
            sys_dot = ui.html('<span class="u-dot" style="background:#9e9e9e"></span>', sanitize=False)
            ui.label("Autosampler" + (" (simulation)" if g.simulated else ""))
        akta_dot = akta_tab_lbl = None
        if akta is not None:
            with ui.row().classes("u-systab items-center gap-2 cursor-pointer") \
                    .on("click", lambda: modules.set_value(t_admin)):
                akta_dot = ui.html('<span class="u-dot" style="background:#9e9e9e"></span>', sanitize=False)
                akta_tab_lbl = ui.label("ÄKTA").classes("max-w-[34rem] truncate")

    # ------------------------------------------------------------------
    # Banners
    # ------------------------------------------------------------------
    with ui.element("div").classes("u-banner u-banner-info") as home_banner:
        ui.icon("info", size="20px")
        home_lbl = ui.label().classes("grow")
        home_connect_btn = set_button("Connect", lambda: act(g.connect, fail="Connect failed"), icon="link")
        home_reset_btn = plain_button("Reset faults", lambda: act(g.reset_all), icon="restart_alt")
        home_cal_btn = set_button("Calibrate…", calibrate_all_ui, icon="rotate_right")
        home_home_btn = set_button("Home all" if not g.manual_homing else "Set home here…", home_ui, icon="home")
        home_limits_btn = plain_button("Find limits…", find_limits_ui, icon="straighten")
    home_banner.set_visibility(False)

    with ui.element("div").classes("u-banner u-banner-pause") as pause_banner:
        ui.icon("front_hand", size="22px")
        pause_lbl = ui.label().classes("grow")
        set_button("Continue", runner.resume, icon="play_arrow")
        plain_button("End run…", confirm_end)
    pause_banner.set_visibility(False)

    with ui.element("div").classes("u-banner u-banner-stop") as stop_banner:
        ui.icon("report", size="22px")
        stop_lbl = ui.label().classes("grow")
        stop_raise = plain_button("Raise needle", lambda: act(g.raise_z, fail="Raise failed"), icon="north")
        stop_park = plain_button("Park", lambda: go_named("park"), icon="local_parking")
        stop_rerun = set_button("Re-run from here…", lambda: rerun_from_stop(), icon="replay")
        plain_button("Dismiss", lambda: dismiss_stop())
    stop_banner.set_visibility(False)

    with ui.tab_panels(modules, value=t_sys, animated=False).classes("u-modules w-full bg-transparent"):

        # ==============================================================
        # SYSTEM CONTROL
        # ==============================================================
        with ui.tab_panel(t_sys).classes("p-2 u-fill"):
            with ui.row().classes("u-sys-row w-full h-full gap-2 flex-nowrap items-stretch"):

                # ---- process picture (deck)
                with ui.element("div").classes("u-pane u-sys-deck h-full"):
                    with ui.element("div").classes("u-pane-title"):
                        ui.icon("account_tree", size="16px").classes("text-gray-600")
                        ui.label("Process Picture")
                        ui.space()
                        for color, border, text in (("#d9d9d9", "#888", "Bottle"), ("#fde9c4", "#888", "Sample"),
                                                    ("#cfe6f7", "#2a7ab8", "Wash"), ("#fff3e0", "#f57c00", "Selected"),
                                                    ("#6cc24a", "#2e7d32", "Needle in")):
                            ui.html(f'<span class="u-dot" style="background:{color};border:2px solid {border}"></span>',
                                    sanitize=False)
                            ui.label(text).classes("text-[11px] text-gray-600 mr-2 font-normal")
                    with ui.element("div").classes("u-pane-body flex flex-col gap-2").style("overflow:hidden"):
                        with ui.row().classes("gap-2 flex-wrap"):
                            v_x, u_x = value_box("X position")
                            v_y, u_y = value_box("Y position")
                            v_z, u_z = value_box("Z (down)")
                            v_state, u_state = value_box("Gantry")
                        with ui.element("div").classes("w-full grow min-h-0"):
                            deck_view = DeckView(g, deck, on_select=lambda s, w: open_bottle(s, w))

                # ---- run order (live) + run log
                with ui.column().classes("u-sys-runcol grow h-full gap-2 min-w-0 flex-nowrap"):
                    p_ro, b_ro = pane("Run order", "w-full", icon="format_list_numbered")
                    p_ro.style("height: 48%")
                    with b_ro:
                        with ui.row().classes("items-center w-full gap-2 -mt-1 flex-nowrap"):
                            ro_title = ui.label().classes("font-bold grow truncate")
                            ro_state = ui.badge("-").props("rounded")
                        ro_status = ui.label().classes("u-run-status w-full mt-1")
                        ro_status.set_visibility(False)
                        ro_bar = ui.linear_progress(value=0, show_value=False, size="5px").props("color=primary") \
                            .classes("mt-1")
                        ro_list = ui.column().classes("gap-0 w-full text-[12.5px] mt-1")
                        ro_rows: List[ui.label] = []
                        ro_index: List[int] = []  # step index of each run-order row
                    p_log, b_log = pane("Run Log", "w-full grow", icon="list_alt")
                    with b_log:
                        log_view = ui.log(max_lines=500).classes("u-log w-full h-full")

                # ---- manual instructions
                p_man, b_man = pane("Manual instructions", "h-full shrink-0", icon="tune")
                p_man.style("width: 330px")
                with b_man:
                    with ui.column().classes("w-full gap-2"):
                        with group("Selected bottle"):
                            sel_lbl = ui.label("None - click a bottle").classes("font-bold")
                            sel_xyz = ui.label().classes("text-[11px] text-gray-600 -mt-1")
                            with ui.row().classes("gap-1"):
                                lock(set_button("Go", lambda: go_selected("sample"), icon="near_me"), needs_homed=True)                                     .tooltip("Move to the bottle and lower the needle to sampling depth")
                                lock(plain_button("Top", lambda: go_selected("top"), icon="vertical_align_top"),
                                     needs_homed=True).tooltip("Move to the bottle, needle at the bottle top")
                                lock(plain_button("Raise", lambda: act(g.raise_z, fail="Raise failed"), icon="north"),
                                     needs_homed=True)
                            with ui.row().classes("gap-1"):
                                plain_button("Sample position", lambda: select_well("sample", "A1"), icon="science")
                                plain_button("Add to run order", lambda: add_to_samples(), icon="playlist_add")

                        with group("Speed"):
                            def speed_text(pct: float) -> str:
                                xy = min(g.config.axes["x"].speed_mm_s, g.config.axes["y"].speed_mm_s) * pct / 100
                                return f"{pct:.0f} % - XY up to {xy:.0f} mm/s (applies to jog, Go and runs; next move)"

                            def on_speed(e):
                                g.set_speed_percent(e.value)
                                speed_lbl.set_text(speed_text(g.speed_percent))
                            speed_slider = ui.slider(min=5, max=100, step=5, value=g.speed_percent, on_change=on_speed) \
                                .props("label label-always dense").classes("w-full px-2")
                            speed_lbl = ui.label(speed_text(g.speed_percent)).classes("text-[11px] text-gray-600 -mt-1")

                        with group("Move to position (safe: Z up, XY, Z down)"):
                            with ui.row().classes("gap-1 flex-nowrap items-center"):
                                gx = ui.number("X mm", value=0, format="%.1f").props("dense outlined").classes("w-[78px]")
                                gy = ui.number("Y mm", value=0, format="%.1f").props("dense outlined").classes("w-[78px]")
                                gz = ui.number("Z mm", value=0, format="%.1f").props("dense outlined").classes("w-[78px]")
                                ui.button(icon="my_location", on_click=lambda: fill_move_fields()) \
                                    .props('flat round dense aria-label="Use current position"') \
                                    .tooltip("Fill in the current position")
                            lock(set_button("Move", lambda: act(g.safe_move_to, gx.value, gy.value, gz.value,
                                                                fail="Move failed"), icon="open_with"),
                                 needs_homed=True)

                        with group("Jog"):
                            step = ui.toggle({0.1: "0.1", 1.0: "1", 10.0: "10", 50.0: "50", 100.0: "100"},
                                             value=10.0).props("dense unelevated no-caps toggle-color=primary")

                            def jog_btn(label, axis, sign):
                                b = plain_button(label, lambda: act(g.jog, axis, sign * step.value, fail="Jog failed"))
                                b.classes("w-14 h-9")
                                return lock(b)

                            with ui.row().classes("items-center gap-4"):
                                with ui.grid(columns=3).classes("gap-1"):
                                    ui.element("div")
                                    jog_btn("Y−", "y", -1)
                                    ui.element("div")
                                    jog_btn("X−", "x", -1)
                                    ui.icon("open_with", size="22px").classes("self-center justify-self-center text-gray-500")
                                    jog_btn("X+", "x", 1)
                                    ui.element("div")
                                    jog_btn("Y+", "y", 1)
                                    ui.element("div")
                                with ui.column().classes("gap-1"):
                                    jog_btn("Z ↑", "z", -1)
                                    jog_btn("Z ↓", "z", 1)
                            ui.label("mm per click. Y− moves up the screen, toward row A / home. "
                                     "Jog also works before homing.").classes("text-[11px] text-gray-500")

                        with group("Homing" if not g.manual_homing else "Homing (manual)"):
                            if g.manual_homing:
                                ui.label(f"Calibrate, then jog (or push) to the home corner - {HOME_CORNER} - "
                                         "and set home there.").classes("text-[11px] text-gray-500")
                            with ui.row().classes("gap-1 items-center"):
                                lock(plain_button("Calibrate…", calibrate_all_ui, icon="rotate_right"))
                                lock(plain_button("Find limits…", find_limits_ui, icon="straighten"))
                                lock(set_button("Home all" if not g.manual_homing else "Set home here…", home_ui))
                                for name in ("x", "y", "z"):
                                    lock(plain_button(name.upper(), lambda n=name: home_axis_ui(n)))

        # ==============================================================
        # SAMPLE MANAGER
        # ==============================================================
        with ui.tab_panel(t_samples).classes("p-2 u-fill"):
            with ui.row().classes("u-sm-row w-full h-full gap-2 flex-nowrap items-stretch"):

                # ---- positions: name / role / notes per bottle
                p, b = pane("Positions", "u-sm-positions h-full shrink-0", icon="inventory_2")
                with b:
                    with ui.column().classes("w-full h-full gap-2 flex-nowrap"):
                        ui.label("Name each bottle and set its role. Click a cell to edit; changes save "
                                 "automatically.").classes("text-[11px] text-gray-600")
                        pos_grid = ui.aggrid({
                            "columnDefs": [
                                {"headerName": "Position", "field": "pos", "width": 92},
                                {"headerName": "Name", "field": "name", "editable": True, "flex": 1},
                                {"headerName": "Role", "field": "role", "editable": True, "width": 92,
                                 "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": list(ROLES)},
                                 "singleClickEdit": True},
                                {"headerName": "Notes", "field": "notes", "editable": True, "flex": 1},
                            ],
                            "rowData": [],
                            "singleClickEdit": True,
                            "defaultColDef": {"sortable": False, "resizable": True},
                            "rowClassRules": {"u-row-wash": "data.role === 'wash'",
                                              "u-row-blank": "data.role === 'blank' || data.role === 'empty'"},
                            "stopEditingWhenCellsLoseFocus": True,
                        }, theme="balham").classes("w-full grow")
                        pos_grid.on("cellValueChanged", lambda e: on_position_edit(e.args.get("data", {})))

                # ---- run order
                p, b = pane("Run order", "flex-1 h-full min-w-0", icon="format_list_numbered")
                with b:
                    with ui.column().classes("w-full h-full gap-2 flex-nowrap"):
                        with ui.row().classes("items-center w-full gap-2 flex-nowrap"):
                            q_name = ui.input("Sample list name", value=queue.name).props("dense outlined") \
                                .classes("grow")
                            q_name.on("blur", lambda: rename_queue())

                            def list_files() -> List[str]:
                                return sorted(f.name for f in SAMPLE_LIST_DIR.glob("*.json"))

                            q_file = ui.select(list_files(), label="Saved lists").props("dense outlined").classes("w-56")
                            plain_button("Open", lambda: open_list(q_file.value), icon="folder_open")
                            plain_button("Save", lambda: save_list(), icon="save")
                        with ui.row().classes("items-center w-full gap-2"):
                            set_button("Quick build…", lambda: quick_build_ui(), icon="auto_awesome")
                            plain_button("Add all samples", lambda: add_all_samples(), icon="playlist_add")
                            wash_names = [b_.key for b_ in reg.with_role("wash")]
                            q_wash = ui.select(wash_names, value=wash_names[0] if wash_names else None,
                                               label="Wash bottle").props("dense outlined").classes("w-36")
                            q_wash_dwell = ui.number("Wash s", value=10, min=0).props("dense outlined").classes("w-20")
                            plain_button("Insert washes between", lambda: insert_washes(), icon="water_drop")
                            plain_button("Remove washes", lambda: remove_washes())
                            plain_button("Remove selected", lambda: remove_selected(), icon="delete")
                            plain_button("Clear…", lambda: clear_queue())
                            q_undo = plain_button("Undo", lambda: undo_queue(), icon="undo")
                        ui.label("Drag rows by the ⠿ handle to reorder · click Type / Dwell / Depth / ÄKTA to edit · "
                                 "click a row to select it · click a bottle on the right to add it") \
                            .classes("text-[11px] text-gray-600")
                        q_grid = ui.aggrid({
                            "columnDefs": [
                                {"headerName": "#", "field": "n", "width": 70, "rowDrag": True},
                                {"headerName": "Type", "field": "kind", "editable": True, "width": 90,
                                 "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": list(KINDS)}},
                                {"headerName": "Position", "field": "pos", "width": 120},
                                {"headerName": "Name", "field": "name", "flex": 1},
                                {"headerName": "Dwell s", "field": "dwell_s", "editable": True, "width": 85,
                                 "cellDataType": "number"},
                                {"headerName": "Depth", "field": "depth", "editable": True, "width": 85,
                                 "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["sample", "top"]}},
                                {"headerName": "ÄKTA", "field": "handshake", "editable": bool(handshake), "width": 75,
                                 "cellDataType": "boolean"},
                            ],
                            "rowData": [],
                            "singleClickEdit": True,
                            "rowDragManaged": True,
                            "animateRows": True,
                            "rowSelection": {"mode": "multiRow", "checkboxes": True, "headerCheckbox": True,
                                             "enableClickSelection": True},
                            "defaultColDef": {"sortable": False, "resizable": True},
                            "rowClassRules": {"u-row-wash": "data.kind === 'wash'", "u-row-blank": "data.kind === 'blank'"},
                            "stopEditingWhenCellsLoseFocus": True,
                        }, theme="balham").classes("w-full grow")
                        q_grid.on("rowDragEnd", lambda: reorder_from_grid())
                        q_grid.on("cellValueChanged", lambda e: on_queue_edit(e.args.get("data", {})))
                        with ui.row().classes("items-center w-full gap-3"):
                            q_home = ui.checkbox("Home first", value=True).props("dense")
                            q_pause = ui.checkbox("Pause in each sample for the operator").props("dense")
                            q_end = ui.select(["(none)"] + list(deck.positions),
                                              value="park" if "park" in deck.positions else "(none)",
                                              label="End at").props("dense outlined").classes("w-28")
                            ui.space()
                            plain_button("Send to Method Editor", lambda: queue_to_method(), icon="send")
                            set_button("Create method & Run…", lambda: queue_run(), icon="play_arrow")

                # ---- quick add: click bottles in deck order (A1 top-left)
                p, b = pane("Add to run order", "h-full shrink-0", icon="touch_app")
                p.style("width: 300px")
                with b:
                    with ui.column().classes("w-full gap-2"):
                        with ui.row().classes("items-center gap-2"):
                            q_dwell = ui.number("Sample dwell s", value=0 if handshake else 30, min=0) \
                                .props("dense outlined").classes("w-32")
                            q_hs = ui.checkbox("ÄKTA handshake", value=bool(handshake)).props("dense")
                            q_hs.set_enabled(bool(handshake))
                        q_autowash = ui.checkbox("Auto wash between samples (wash bottle + Wash s from the run "
                                                 "order)", value=bool(reg.with_role("wash"))).props("dense")
                        quick_box = ui.column().classes("w-full gap-2")
                        q_last = ui.label().classes("text-[12px] font-semibold text-[#1d6b45]")
                        ui.label("Blue = wash bottle · grey dashed = blank/empty. Wash bottles are added as "
                                 "washes. With the ÄKTA handshake the ÄKTA sets the pace; dwell is extra time "
                                 "after it reports done.").classes("text-[11px] text-gray-600")

        # ==============================================================
        # METHOD EDITOR (viewer of the generated / opened method)
        # ==============================================================
        with ui.tab_panel(t_method).classes("p-2 u-fill"):
            with ui.row().classes("w-full h-full gap-2 flex-nowrap items-start"):
                with ui.column().classes("w-[360px] shrink-0 gap-2"):
                    p, b = pane("Open method", "w-full", icon="folder_open")
                    with b:
                        def seq_files() -> List[str]:
                            return sorted(p.name for p in SEQUENCE_DIR.glob("*.json"))

                        with ui.row().classes("items-center w-full flex-nowrap gap-1"):
                            file_sel = ui.select(seq_files(), label="Method file").props("dense outlined").classes("grow")
                            ui.button(icon="refresh", on_click=lambda: file_sel.set_options(seq_files())) \
                                .props('flat round dense aria-label="Refresh"')
                            set_button("Open", lambda: load_file(file_sel.value))
                        plain_button("Save method", lambda: save_current(), icon="save").classes("mt-2")
                    p, b = pane("Build a method", "w-full", icon="science")
                    with b:
                        ui.label("Sample lists (which bottles, in what order, with washes) are built in the "
                                 "Sample Manager. 'Send to Method Editor' or Play turns the list into a method.") \
                            .classes("text-[12px] text-gray-700")
                        plain_button("Open Sample Manager", lambda: modules.set_value(t_samples), icon="science") \
                            .classes("mt-2")

                p, b = pane("Method outline", "grow min-w-0 max-h-full", icon="format_list_numbered")
                b.classes("flex flex-col flex-nowrap")
                with b:
                    with ui.row().classes("items-center w-full"):
                        seq_title = ui.label("No method open").classes("text-base font-bold grow")
                        run_state = ui.badge("-").props("rounded")
                    with ui.row().classes("gap-1 mt-1"):
                        run_btn = set_button("Run…", lambda: start_run(), icon="play_arrow")
                        pause_btn = plain_button("Pause", runner.pause, icon="pause")
                        resume_btn = plain_button("Continue", runner.resume, icon="play_circle")
                        abort_btn = plain_button("End…", confirm_end, icon="stop")
                    run_bar = ui.linear_progress(value=0, show_value=False, size="6px").props("color=primary") \
                        .classes("mt-2")
                    run_msg = ui.label().classes("text-sm text-gray-600")
                    steps_box = ui.column().classes("gap-0 w-full min-h-0 flex-nowrap overflow-auto text-[12.5px] mt-1")
                    step_rows: List[ui.label] = []

        # ==============================================================
        # ADMINISTRATION (ÄKTA link, axes, teach)
        # ==============================================================
        with ui.tab_panel(t_admin).classes("p-2"):
            with ui.row().classes("u-admin-row w-full gap-2 items-start"):
                with ui.column().classes("u-admin-main gap-2 min-w-0"):
                    p, b = pane("ÄKTA link", "w-full", icon="settings_input_component")
                    with b:
                        akta_backends = ui.row().classes("gap-2 items-center")
                        akta_table = ui.table(
                            columns=[{"name": c, "label": c.capitalize(), "field": c, "align": "left"}
                                     for c in ("signal", "source", "value", "age", "io", "description")],
                            rows=[], row_key="signal").classes("w-full").props("dense flat bordered")
                        if akta is None:
                            ui.label("ÄKTA link is off. Set \"mode\" in config/akta.json to \"sim\" or \"hardware\", "
                                     "or start with --akta sim|hardware.").classes("text-gray-600")
                        else:
                            outputs = [n for n, s in akta.signals.items() if s.get("output")]

                            async def set_output(name, value):
                                try:
                                    await run.io_bound(akta.write, name, value)
                                except Exception as e:
                                    ui.notify(f"{name}: {e}", type="negative")

                            with ui.row().classes("gap-2 items-center mt-2"):
                                set_button("Connect", lambda: run.io_bound(akta.connect), icon="link")
                                plain_button("Reset outputs to idle", lambda: run.io_bound(akta.reset_outputs))
                                for name in outputs:
                                    ui.label(name).classes("font-mono ml-4")
                                    plain_button("0", lambda n=name: set_output(n, 0))
                                    plain_button("1", lambda n=name: set_output(n, 1))
                            if handshake:
                                hs = handshake
                                ui.markdown(
                                    f"**Handshake per bottle:** move over the bottle and wait for "
                                    f"`{hs['request']['signal']}` = {hs['request']['equals']} → lower, set "
                                    f"`{hs['ready']['signal']}` = {hs['ready']['active']} → wait for "
                                    f"`{hs['done']['signal']}` = {hs['done']['equals']} → set "
                                    f"`{hs['ready']['signal']}` = {hs['ready']['idle']}, raise. "
                                    "Outputs return to idle on STOP / End / failure. "
                                    "(UNICORN logic: 1 = open, 0 = closed.)"
                                ).classes("text-sm text-gray-700")

                    p, b = pane("Axes", "w-full", icon="straighten")
                    with b:
                        axis_table = ui.table(
                            columns=[{"name": c, "label": c.capitalize(), "field": c, "align": "left"}
                                     for c in ("axis", "state", "position", "velocity", "torque", "homed", "errors")],
                            rows=[], row_key="axis").classes("w-full").props("dense flat bordered")
                        cfg = g.config
                        ui.label(
                            f"Workspace {cfg.axes['x'].max_mm / MM_PER_IN:.0f} × {cfg.axes['y'].max_mm / MM_PER_IN:.0f} in "
                            f"({cfg.axes['x'].max_mm:.1f} × {cfg.axes['y'].max_mm:.1f} mm) · Z travel "
                            f"{cfg.axes['z'].max_mm:.0f} mm · safe Z {cfg.z_safe_mm} mm · "
                            + " · ".join(f"{s.name}: {s.labware.name}" for s in deck.slots.values())
                        ).classes("text-xs text-gray-600 mt-1")

                    p, b = pane("Calibration (ODrive)", "w-full", icon="tune")
                    with b:
                        ui.label("Motor calibration measures the motor (beeps, does not turn) - needed once, then "
                                 "Save to board. Encoder offset calibration is needed after every power-up "
                                 "(Home all also runs it automatically); the motor turns slightly.") \
                            .classes("text-[12px] text-gray-700")
                        cal_rows: Dict[str, Dict[str, Any]] = {}
                        for name in ("x", "y", "z"):
                            ax_cfg = g.config.axes[name]
                            with ui.column().classes("w-full gap-1 py-1 border-b border-gray-200"):
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.label(name.upper()).classes("text-lg font-bold w-5")
                                    ui.label(f"Board {ax_cfg.serial}").classes("text-[12px] font-mono")
                                    src = ui.label().classes("text-[11px] text-gray-500")
                                    ui.space()
                                    motor_b = ui.badge("motor ?").props("rounded")
                                    enc_b = ui.badge("encoder ?").props("rounded")
                                    ready_b = ui.badge("ready ?").props("rounded")
                                with ui.row().classes("w-full items-center gap-2 pl-7"):
                                    btns = [
                                        plain_button("Motor…", lambda n=name: calibrate_ui(n, "motor"),
                                                     icon="graphic_eq"),
                                        plain_button("Encoder offset…", lambda n=name: calibrate_ui(n, "encoder_offset"),
                                                     icon="rotate_right"),
                                        plain_button("Full…", lambda n=name: calibrate_ui(n, "full"), icon="build"),
                                        set_button("Save to board…", lambda n=name: save_board_ui(n), icon="save"),
                                        plain_button("Scale…", lambda n=name: scale_ui(n), icon="straighten"),
                                    ]
                                    for btn in btns:
                                        btn.props("dense no-wrap")
                                with ui.row().classes("w-full items-center gap-x-4 gap-y-0 pl-7"):
                                    scale_l = ui.label().classes("text-[11px] text-gray-600 font-mono")
                                    rl = ui.label().classes("text-[11px] text-gray-600")
                            cal_rows[name] = {"src": src, "motor": motor_b, "rl": rl, "enc": enc_b,
                                              "ready": ready_b, "btns": btns,
                                              "scale": scale_l}

                    p, b = pane("Tuning (control loop)", "w-full", icon="speed")
                    with b:
                        ui.label("Set the position / velocity loop gains, try them with a small step test (out and "
                                 "back), and save the ones that work: the app writes them to the board on every "
                                 "connect. Y runs smoothly - use it as the reference.") \
                            .classes("text-[12px] text-gray-700")
                        with ui.row().classes("items-center gap-3"):
                            tune_axis = ui.toggle({"x": "X", "y": "Y", "z": "Z"}, value="x",
                                                  on_change=lambda _: tune_read()) \
                                .props("dense unelevated no-caps toggle-color=primary")
                            tune_src = ui.label().classes("text-[11px] text-gray-500")
                        with ui.row().classes("gap-1 items-center flex-wrap"):
                            preset_sel = ui.select(list(load_presets()), label="Gain preset") \
                                .props("dense outlined").classes("w-64")
                            plain_button("Load", lambda: preset_load(), icon="upload")
                            preset_name = ui.input("Save as preset").props("dense outlined").classes("w-48")
                            plain_button("Save preset", lambda: preset_save(), icon="bookmark_add")
                            plain_button("Delete…", lambda: preset_delete(), icon="delete")
                        tune_inputs: Dict[str, Any] = {}
                        with ui.row().classes("gap-2 items-end flex-wrap"):
                            for key, (label, lo, hi) in GAIN_LIMITS.items():
                                if key == "enable_gain_scheduling":
                                    tune_inputs[key] = ui.checkbox(label).props("dense")
                                else:
                                    tune_inputs[key] = ui.number(label, min=lo, max=hi, format="%.5g") \
                                        .props("dense outlined").classes("w-56")
                        tune_ref = ui.label().classes("text-[11px] text-gray-600 font-mono")
                        with ui.row().classes("gap-1 items-center flex-wrap"):
                            plain_button("Read from board", lambda: tune_read(), icon="download")
                            plain_button("Copy from Y", lambda: tune_copy_y(), icon="content_copy")
                            lock(set_button("Apply", lambda: tune_apply(), icon="check"))
                            tune_size = ui.number("Test move (mm)", value=2.0, min=0.5, max=100, step=0.5,
                                                  format="%.1f").props("dense outlined").classes("w-32")
                            lock(set_button("Step test…", lambda: tune_step(), icon="show_chart"))
                            lock(plain_button("Save to gantry.json…", lambda: tune_save(), icon="save"))
                        tune_result = ui.label().classes("text-[12px] font-mono whitespace-pre-wrap")
                        tune_chart = ui.echart({
                            "animation": False,
                            "grid": {"left": 50, "right": 16, "top": 30, "bottom": 30},
                            "legend": {"top": 0},
                            "tooltip": {"trigger": "axis"},
                            "xAxis": {"type": "value", "name": "s", "nameLocation": "end"},
                            "yAxis": [{"type": "value", "name": "mm", "scale": True},
                                      {"type": "value", "name": "mm/s", "scale": True, "splitLine": {"show": False}}],
                            "series": [
                                {"name": "position", "type": "line", "showSymbol": False, "data": []},
                                {"name": "target", "type": "line", "showSymbol": False, "step": "end", "data": [],
                                 "lineStyle": {"type": "dashed"}},
                                {"name": "speed", "type": "line", "showSymbol": False, "yAxisIndex": 1, "data": [],
                                 "lineStyle": {"width": 1}},
                            ],
                        }).classes("w-full h-64")
                        ui.timer(0.5, lambda: tune_read(), once=True)

                    p, b = pane("Motion (speed and ramps)", "w-full", icon="moving")
                    with b:
                        ui.label("Top speed and ramps for every move (jog, Go, Move, runs). X / Y ramp up and down "
                                 "(acceleration / deceleration). Z runs at constant speed with a separate up and down "
                                 "speed. The Speed slider in System Control scales all of these.") \
                            .classes("text-[12px] text-gray-700")
                        motion_inputs: Dict[str, Dict[str, Any]] = {}
                        motion_limits: Dict[str, Dict[str, Any]] = {}
                        for name in ("x", "y", "z"):
                            with ui.row().classes("items-end gap-2 w-full flex-wrap"):
                                ui.label(name.upper()).classes("text-lg font-bold w-6")
                                row: Dict[str, Any] = {}
                                if name == "z":
                                    row["speed_mm_s"] = ui.number("Down speed mm/s", min=1, max=500, format="%.0f")
                                    row["speed_to_home_mm_s"] = ui.number("Up speed mm/s", min=1, max=500, format="%.0f")
                                    ui.label("constant speed").classes("text-[11px] text-gray-500 pb-2")
                                else:
                                    row["speed_mm_s"] = ui.number("Speed mm/s", min=1, max=1000, format="%.0f")
                                    row["accel_mm_s2"] = ui.number("Ramp up mm/s²", min=1, max=5000, format="%.0f")
                                    row["decel_mm_s2"] = ui.number("Ramp down mm/s²", min=1, max=5000, format="%.0f")
                                for w in row.values():
                                    w.props("dense outlined").classes("w-40")
                                motion_inputs[name] = row
                            with ui.row().classes("items-end gap-2 w-full flex-wrap pl-8"):
                                ui.label("Limits").classes("text-[11px] text-gray-500 w-12 pb-2")
                                lim: Dict[str, Any] = {"max_speed_mm_s": ui.number("Max speed mm/s", min=1, max=1000,
                                                                                     format="%.0f")}
                                if name != "z":
                                    lim["max_accel_mm_s2"] = ui.number("Max ramp up mm/s²", min=1, max=5000,
                                                                       format="%.0f")
                                    lim["max_decel_mm_s2"] = ui.number("Max ramp down mm/s²", min=1, max=5000,
                                                                       format="%.0f")
                                for w in lim.values():
                                    w.props("dense outlined").classes("w-40")
                                motion_limits[name] = lim
                        with ui.row().classes("gap-1 items-center flex-wrap"):
                            plain_button("Reset fields", lambda: motion_read(), icon="refresh")
                            lock(set_button("Apply", lambda: motion_apply(False), icon="check"))
                            lock(plain_button("Save to gantry.json…", lambda: motion_save(), icon="save"))
                            plain_button("Read from board", lambda: motion_board(), icon="download")
                            lock(plain_button("Reload settings from gantry.json", lambda: reload_settings_ui(),
                                              icon="sync"))
                        motion_board_lbl = ui.label().classes("text-[11px] font-mono text-gray-700 whitespace-pre")
                        ui.timer(0.6, lambda: motion_read(), once=True)

                p, b = pane("Teach (from the current needle position)", "u-admin-side w-[380px] shrink-0",
                            icon="my_location")
                with b:
                    with ui.column().classes("gap-2 w-full"):
                        ui.label("1. Select a bottle in the Process Picture.  2. Jog the needle onto it.  "
                                 "3. Save the value below.").classes("text-xs text-gray-600")
                        teach_lbl = ui.label("No bottle selected").classes("font-semibold")
                        with ui.row().classes("items-center justify-between w-full"):
                            teach_xy_lbl = ui.label("Bottle position (XY)")
                            set_button("Save…", lambda: teach_a1())
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label("Bottle top (Z)")
                            set_button("Save…", lambda: teach_z("top"))
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label("Sampling depth (Z)")
                            set_button("Save…", lambda: teach_z("sample"))
                        ui.label("Saving overwrites the calibration in config/ (you will be asked first).") \
                            .classes("text-[11px] text-gray-500")

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------
    with ui.row().classes("u-statusbar w-full items-center px-3 gap-3 flex-nowrap fixed bottom-0 left-0 z-10"):
        sb_run = ui.label()
        ui.element("div").classes("sep")
        sb_step = ui.label().classes("truncate max-w-[34rem]")
        ui.element("div").classes("sep")
        sb_gantry = ui.label()
        ui.space()
        sb_akta = ui.label().classes("truncate max-w-[28rem]")
        ui.element("div").classes("sep")
        ui.label("Simulation" if g.simulated else "Hardware: ODrive (USB)")

    # ------------------------------------------------------------------
    # Bottle popup (UNICORN-style component dialog). Placed beside the clicked
    # bottle, kept inside the Process Picture, closed by X / Esc / outside click.
    # ------------------------------------------------------------------
    with ui.element("div").classes("u-dialog as-popup w-[290px]") \
            .style("position: fixed; display: none; z-index: 3000"):
        with ui.element("div").classes("u-dialog-title"):
            dlg_title = ui.label("Bottle")
            ui.space()
            ui.button(icon="close", on_click=lambda: ui.run_javascript("asDeck.hidePopup()")) \
                .props('flat round dense color=white size=sm aria-label="Close"')
        with ui.column().classes("p-3 gap-2 w-full"):
            dlg_info = ui.label().classes("text-xs font-semibold")
            dlg_xyz = ui.label().classes("text-xs text-gray-600 -mt-1")
            with ui.row().classes("w-full justify-between"):
                with ui.column().classes("items-center gap-1"):
                    ui.label("Into sample").classes("text-[11px] text-gray-600")
                    lock(set_button("Go", lambda: go_selected("sample")), needs_homed=True)
                with ui.column().classes("items-center gap-1"):
                    ui.label("Bottle top").classes("text-[11px] text-gray-600")
                    lock(plain_button("Top", lambda: go_selected("top")), needs_homed=True)
                with ui.column().classes("items-center gap-1"):
                    ui.label("Safe Z").classes("text-[11px] text-gray-600")
                    lock(plain_button("Raise", lambda: act(g.raise_z, fail="Raise failed")), needs_homed=True)
            plain_button("Add to run order", lambda: add_to_samples(), icon="playlist_add").classes("w-full")
            dlg_hint = ui.label("Home the gantry to enable moves.").classes("text-[11px] text-red-700")

    # ------------------------------------------------------------------
    # Selection / deck helpers
    # ------------------------------------------------------------------
    def select_well(slot: Optional[str], well: Optional[str]):
        m.selected = (slot, well) if slot else None
        show_selection()

    def open_bottle(slot: str, well: str):
        """Bottle clicked in the process picture: the browser already placed the popup beside it."""
        select_well(slot, well)

    def show_selection():
        if not m.selected:
            sel_lbl.set_text("None - click a bottle")
            sel_xyz.set_text("")
            teach_lbl.set_text("No bottle selected")
            teach_xy_lbl.set_text("Bottle position (XY)")
            return
        slot, well = m.selected
        tx, ty, tz = deck.well_xyz(slot, well, "top")
        sz = deck.well_xyz(slot, well, "sample")[2]
        name = well_label(slot, well)
        info = reg.get(slot, well)
        sel_lbl.set_text(name + (f" · {info.name}" if info.name else ""))
        teach_lbl.set_text(f"{name}  ({slot}:{well})")
        teach_xy_lbl.set_text("Sample position XY" if slot == "sample" else f"{slot} XY (via {well})")
        xyz = f"X {tx:.1f}  Y {ty:.1f} mm · Z top {tz:.1f} · sample {sz:.1f}"
        sel_xyz.set_text(xyz)
        dlg_title.set_text(name)
        dlg_info.set_text(f"{info.role.capitalize()}" + (f" · {info.name}" if info.name else "")
                          + (f" · {info.notes}" if info.notes else ""))
        dlg_xyz.set_text(xyz)

    async def go_selected(depth: str):
        if not m.selected:
            ui.notify("Select a bottle first (click one in the Process Picture)", type="warning")
            return
        await go_slot(*m.selected, depth)

    async def go_slot(slot: str, well: str, depth: str):
        if not g.is_homed:
            ui.notify("Home the gantry first", type="warning")
            return
        x, y, z = deck.well_xyz(slot, well, depth)
        await act(g.safe_move_to, x, y, z, fail="Move failed")

    async def go_named(name: str):
        if not g.is_homed:
            ui.notify("Home the gantry first", type="warning")
            return
        if name in deck.positions:
            await act(g.safe_move_to, *deck.positions[name], fail="Move failed")

    def fill_move_fields():
        pos = g.get_position()
        gx.value, gy.value, gz.value = round(pos.x, 1), round(pos.y, 1), round(pos.z, 1)

    def save_labware_z(lw):
        path = CONFIG_DIR / "labware" / f"{lw.id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["z_top_mm"], data["z_sample_mm"] = lw.z_top_mm, lw.z_sample_mm
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def need_sel() -> Optional[Tuple[str, str]]:
        if not m.selected:
            ui.notify("Select a bottle first (System Control → Process Picture)", type="warning")
        return m.selected

    def teach_a1():
        sel = need_sel()
        if not sel:
            return
        if not g.is_homed:
            ui.notify("Home the gantry first - taught positions need a homed gantry", type="warning")
            return
        slot, well = sel
        pos = g.get_position()
        s = deck.slot(slot)
        ax, ay = s.labware.a1_offset_mm
        dx, dy = s.labware.well_offset(well)

        def do():
            # current XY is this well: shift the slot so the well lands here
            s.origin_mm = (round(pos.x - dx, 3), round(pos.y - dy, 3))
            deck.save()
            show_selection()
            ui.notify(f"{slot} moved: origin now {s.origin_mm} (saved). Reload the page to redraw the deck.")
        confirm("Save bottle position", [f"Set {well_label(slot, well)} to the current XY "
                                         f"(X {pos.x:.1f}, Y {pos.y:.1f})?",
                                         f"This moves every position in slot '{slot}' and overwrites deck.json."],
                "Save", do)

    def teach_z(which: str):
        sel = need_sel()
        if not sel:
            return
        if not g.is_homed:
            ui.notify("Home the gantry first - taught heights need a homed gantry", type="warning")
            return
        slot = deck.slot(sel[0])
        z = round(g.get_position().z - slot.z_offset_mm, 3)
        what = "bottle top" if which == "top" else "sampling depth"

        def do():
            setattr(slot.labware, f"z_{which}_mm", z)
            save_labware_z(slot.labware)
            show_selection()
            ui.notify(f"{slot.labware.name}: {what} = {z} mm (saved)")
        confirm("Save height", [f"Set the {what} for '{slot.labware.name}' to Z {z} mm?",
                                "This applies to every bottle of this type and overwrites its labware file."],
                "Save", do)

    # ------------------------------------------------------------------
    # Calibration (ODrive)
    # ------------------------------------------------------------------
    CAL_TEXT = {
        "motor": ("Motor calibration", ["Measures the motor resistance and inductance.",
                                        "The motor BEEPS for a few seconds; it does not turn.",
                                        "Afterwards use 'Save to board' so it is kept after power-off."]),
        "encoder_offset": ("Encoder offset calibration", [
            "Finds the encoder offset (needed after every power-up).",
            "The motor TURNS slightly in both directions - make sure the axis has room to move."]),
        "full": ("Full calibration", ["Motor calibration (beeps) followed by encoder offset calibration.",
                                      "The motor TURNS slightly - make sure the axis has room to move."]),
    }

    def calibrate_ui(name: str, kind: str):
        if not g.is_connected:
            ui.notify("Connect the gantry first (toolbar → Connect)", type="warning")
            return
        title, lines = CAL_TEXT[kind]
        confirm(f"{title} - {name.upper()}", lines + ["", "STOP aborts it.", "Run it now?"], "Run",
                lambda: act(g.calibrate, name, kind, fail=f"{title} on {name.upper()} failed"))

    def save_board_ui(name: str):
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        confirm(f"Save to board - {name.upper()}",
                ["Writes this board's configuration (including a new motor calibration) to its flash.",
                 "The board reboots: the encoder offset and homing must be redone afterwards.", "", "Save now?"],
                "Save", lambda: act(g.save_to_board, name, fail=f"Saving {name.upper()} failed"))

    def scale_ui(name: str):
        """Scale (turns_per_mm) calibration: test move, measure it with a ruler, apply + save."""
        ax_cfg = g.config.axes[name]
        if not g.is_homed:
            ui.notify("Home the gantry first - the test move goes away from the home end", type="warning")
            return
        k = ax_cfg.turns_per_mm
        st: Dict[str, Any] = {"commanded": None, "new": None}
        dialog_host.clear()
        with dialog_host, ui.dialog().props("persistent") as dlg, ui.element("div").classes("u-dialog w-[500px]"):
            with ui.element("div").classes("u-dialog-title"):
                ui.label(f"Scale calibration - {name.upper()}")
            with ui.column().classes("p-4 gap-2 w-full"):
                ui.label(f"Current scale: {k:.6g} turns/mm = {1 / k:.4g} mm per motor turn.").classes("text-[13px]")
                ui.label("1. Mark where the carriage is now.  2. Make the test move.  3. Measure how far it "
                         "really went and enter it.  4. Apply + save.").classes("text-[13px]")
                ui.label("Until the scale is right, the real move can be shorter or longer than asked - start "
                         "small. It moves slowly, away from home. STOP works as usual.")                     .classes("text-[12px] text-gray-600")
                dist = ui.number("Test move (mm at the current scale)", value=50, min=5, max=200, step=5,
                                 format="%.0f").classes("w-64")
                moved_l = ui.label().classes("text-[13px]")
                measured = ui.number("Measured distance (mm)", min=0.1, step=0.5, format="%.1f").classes("w-64")
                result_l = ui.label().classes("text-[13px] font-medium")

                def recompute():
                    st["new"] = None
                    apply_b.set_enabled(False)
                    if not st["commanded"] or not measured.value:
                        result_l.set_text("")
                        return
                    try:
                        new = Gantry.rescaled_turns_per_mm(k, st["commanded"], float(measured.value))
                    except ValueError as e:
                        result_l.set_text(str(e))
                        return
                    st["new"] = new
                    result_l.set_text(f"New scale: {new:.6g} turns/mm = {1 / new:.4g} mm/turn "
                                      f"(the real move was {100 * float(measured.value) / st['commanded']:.0f}% of the asked).")
                    apply_b.set_enabled(True)
                measured.on_value_change(lambda _: recompute())

                async def test_move():
                    d = float(dist.value or 0)
                    if not 5 <= d <= 200:
                        ui.notify("Test move must be 5-200 mm", type="warning")
                        return
                    pos = getattr(g.get_position(), name)
                    if pos + d > ax_cfg.max_mm:
                        ui.notify(f"Not enough room: {name.upper()} is at {pos:.0f} of {ax_cfg.max_mm:.0f} mm - "
                                  f"move it back towards home first", type="warning")
                        return
                    ok = await act(g.jog, name, d, speed=ax_cfg.homing_speed_mm_s, fail="Test move failed")
                    if ok:
                        st["commanded"] = (st["commanded"] or 0) + d
                        moved_l.set_text(f"Moved {st['commanded']:g} mm (current scale) from the mark - "
                                         f"now measure it.")
                        back_b.set_enabled(True)
                        recompute()

                async def move_back():
                    if st["commanded"]:
                        ok = await act(g.jog, name, -st["commanded"], speed=ax_cfg.homing_speed_mm_s,
                                       fail="Move back failed")
                        if ok:
                            st["commanded"] = None
                            moved_l.set_text("Back at the mark.")
                            back_b.set_enabled(False)
                            recompute()

                def apply():
                    new = st["new"]
                    try:
                        g.set_turns_per_mm(name, new)
                        save_turns_per_mm(name, new, CONFIG_DIR / "gantry.json")
                    except Exception as e:
                        ui.notify(f"Could not apply the scale: {e}", type="negative")
                        return
                    dlg.close()
                    ui.notify(f"{name.upper()} scale set to {new:.6g} turns/mm and saved to gantry.json. "
                              f"Positions now read in real mm.", type="positive")

                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    plain_button("Close", dlg.close)
                    back_b = plain_button("Move back", move_back, icon="undo")
                    back_b.set_enabled(False)
                    plain_button("Make test move", test_move, icon="open_in_full")
                    apply_b = set_button("Apply + save", apply, icon="save")
                    apply_b.set_enabled(False)
        dlg.open()

    def tune_values() -> Dict[str, float]:
        """Fields -> board units (the quiet-hold band is shown in mm, the board wants turns)."""
        k = g.config.axes[tune_axis.value].turns_per_mm
        out: Dict[str, float] = {}
        for key, w in tune_inputs.items():
            if key == "enable_gain_scheduling":
                out[key] = 1.0 if w.value else 0.0
            elif w.value is not None:
                out[key] = float(w.value) * (k if key == "gain_scheduling_width" else 1.0)
        return out

    def tune_show(vals: Dict[str, Any], mm_band: bool = False):
        """Board-unit values (or a preset, whose band is already in mm) -> fields."""
        k = g.config.axes[tune_axis.value].turns_per_mm
        for key, w in tune_inputs.items():
            v = vals.get(key)
            if key == "gain_scheduling_width":
                v = vals.get("gain_scheduling_width_mm") if mm_band else (None if v is None else v / k)
            if v is None:
                continue
            w.set_value(bool(v) if key == "enable_gain_scheduling" else round(float(v), 6))

    def tune_read():
        n = tune_axis.value
        if not g.is_connected:
            tune_src.set_text("not connected - showing gantry.json (empty = board default)")
            tune_show({k: getattr(g.config.axes[n], k) for k in tune_inputs})
            return
        try:
            vals = g.read_tuning(n)
        except Exception as e:
            ui.notify(f"Could not read {n.upper()}: {e}", type="negative")
            return
        tune_show(vals)
        stand_in = n in g.simulated_axes and not g.simulated
        tune_src.set_text("simulated stand-in" if stand_in else "simulation" if g.simulated else "read from the board")
        if n != "y":
            try:
                ref = g.read_tuning("y")
                tune_ref.set_text("Y (reference): " + "  ".join(f"{k} {v:.5g}" for k, v in ref.items()))
            except Exception:
                tune_ref.set_text("")
        else:
            tune_ref.set_text("Y is the reference axis")

    def tune_copy_y():
        try:
            ref = g.read_tuning("y") if g.is_connected else {k: getattr(g.config.axes["y"], k) for k in tune_inputs}
        except Exception as e:
            ui.notify(f"Could not read Y: {e}", type="negative")
            return
        tune_show(ref)
        ui.notify("Copied Y's gains into the fields - Apply or Step test to try them")

    def tune_apply(save: bool = False) -> bool:
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return False
        n = tune_axis.value
        try:
            g.apply_tuning(n, tune_values(), CONFIG_DIR / "gantry.json" if save else None)
        except Exception as e:
            ui.notify(f"Not applied: {e}", type="negative")
            return False
        ui.notify(f"{n.upper()} gains {'saved to gantry.json and ' if save else ''}applied"
                  + ("" if save else " (not saved yet)"), type="positive")
        return True

    def preset_load():
        name = preset_sel.value
        if not name:
            ui.notify("Pick a preset", type="info")
            return
        tune_show(load_presets().get(name, {}), mm_band=True)
        ui.notify(f"Loaded '{name}' into the fields - Apply or Step test to try it on {tune_axis.value.upper()}")

    def preset_save():
        k = g.config.axes[tune_axis.value].turns_per_mm
        vals = {key: v for key, v in tune_values().items() if key not in ("gain_scheduling_width", "encoder_bandwidth")}
        width = tune_values().get("gain_scheduling_width")
        if width is not None:
            vals["gain_scheduling_width_mm"] = round(width / k, 4)
        try:
            save_preset(preset_name.value or "", vals)
        except ValueError as e:
            ui.notify(str(e), type="warning")
            return
        preset_sel.set_options(list(load_presets()), value=preset_name.value.strip())
        ui.notify(f"Preset '{preset_name.value.strip()}' saved")

    def preset_delete():
        name = preset_sel.value
        if not name:
            return

        def do():
            delete_preset(name)
            preset_sel.set_options(list(load_presets()), value=None)
        confirm("Delete preset", [f"Delete the gain preset '{name}'?"], "Delete", do, danger=True)

    def tune_save():
        n = tune_axis.value
        confirm(f"Save {n.upper()} tuning", [
            "Writes these gains to gantry.json and applies them now.",
            "The app sets them on the board every time it connects.",
            "  ".join(f"{k} {v:g}" for k, v in tune_values().items()), "", "Save?"],
            "Save", lambda: tune_apply(save=True))

    def tune_step():
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        n = tune_axis.value
        size = float(tune_size.value or 2.0)
        homed = g.axes[n].status.is_homed
        lines = [f"{n.upper()} moves {size:g} mm and back at up to "
                 f"{min(g.config.axes[n].speed_mm_s, 2.0 / g.config.axes[n].turns_per_mm):.0f} mm/s, "
                 "with the gains in the fields (applied first if they differ from the board).",
                 "It stops by itself on overspeed or if the drive disarms; STOP works as usual."]
        if not homed:
            lines.append(f"Not homed: make sure {n.upper()} has at least {size:g} mm of room in its + direction "
                         "(away from home) - it is not checked.")
        if n == "z":
            lines.append("Z: make sure the needle has room above/below.")

        async def run_it():
            try:
                on_board = g.read_tuning(n)
            except Exception:
                on_board = {}
            changed = any(abs(v - on_board.get(k, float("nan"))) > 1e-6 * max(1.0, abs(v)) or k not in on_board
                          for k, v in tune_values().items())
            if changed and not tune_apply():  # unchanged fields: test exactly what is on the board
                return
            r = await act(g.step_test, n, size, fail="Step test failed")
            if not r:
                return
            show_step(n, r)
        confirm(f"Step test - {n.upper()}", lines + ["", "Run it?"], "Run", run_it)

    def show_step(n: str, r: dict):
        gains = "  ".join(f"{k} {v:.4g}" for k, v in r.get("gains", {}).items())
        st_ = r.get("settings")
        if st_:
            gains += (f"\nmove: {st_['speed_out']:.0f} mm/s out / {st_['speed_back']:.0f} back, "
                      f"ramp up {st_['accel']:.0f} / down {st_['decel']:.0f} mm/s2")
        if r.get("aborted"):
            tune_result.set_text(f"{n.upper()} ABORTED: {r['aborted']}\n{gains}")
            tune_result.classes(replace="text-[12px] font-mono whitespace-pre-wrap text-red-700")
        elif "metrics" in r:
            m = r["metrics"]
            good = (m["oscillations"] <= 2 and m["overshoot_pct"] <= 10 and m["settle_after_move_s"] <= 0.3
                    and m["error_mm"] <= 0.1)
            settle = ("never" if m["settle_s"] == float("inf")
                      else f"{m['settle_after_move_s'] * 1000:.0f} ms after the {m['move_s']:.2f} s move")
            tune_result.set_text(
                f"{n.upper()} {'GOOD' if good else 'needs work'}: overshoot {m['overshoot_pct']:.1f} %, settle {settle}, "
                f"oscillations {m['oscillations']}, error {m['error_mm']:.3f} mm\n{m['out']}\n{m['back']}\n{gains}")
            tune_result.classes(replace="text-[12px] font-mono whitespace-pre-wrap "
                                + ("text-green-800" if good else "text-amber-800"))
        t, pos, tgt = r.get("t", []), r.get("pos_mm", []), r.get("target_mm", [])
        every = max(1, len(t) // 800)
        tune_chart.options["series"][0]["data"] = [[round(a, 4), round(b, 4)] for a, b in zip(t[::every], pos[::every])]
        tune_chart.options["series"][1]["data"] = [[round(a, 4), b] for a, b in zip(t[::every], tgt[::every])]
        vel = r.get("vel_mm_s", [])
        tune_chart.options["series"][2]["data"] = [[round(a, 4), round(b, 2)] for a, b in zip(t[::every], vel[::every])]
        tune_chart.update()

    Z_RAMP_MM_S2 = 300.0  # Z: "constant speed" = a near-instant ramp (0.1 s to 30 mm/s)

    def motion_read():
        for name, lim in motion_limits.items():
            ax = g.config.axes[name]
            for key, w in lim.items():
                w.set_value(getattr(ax, key))
        for name, row in motion_inputs.items():
            ax = g.config.axes[name]
            row["speed_mm_s"].set_value(ax.speed_mm_s)
            if name == "z":
                row["speed_to_home_mm_s"].set_value(ax.speed_to_home_mm_s or ax.speed_mm_s)
            else:
                row["accel_mm_s2"].set_value(ax.accel_mm_s2)
                row["decel_mm_s2"].set_value(ax.decel_mm_s2 or ax.accel_mm_s2)

    def motion_apply(save: bool) -> bool:
        try:
            for name, lim in motion_limits.items():  # limits first, then the values within them
                v = {k: (float(w.value) if w.value is not None else None) for k, w in lim.items()}
                g.set_limits(name, v.get("max_speed_mm_s"), v.get("max_accel_mm_s2"), v.get("max_decel_mm_s2"),
                             CONFIG_DIR / "gantry.json" if save else None)
            for name, row in motion_inputs.items():
                v = {k: float(w.value) for k, w in row.items() if w.value is not None}
                if "speed_mm_s" not in v:
                    raise ValueError(f"{name.upper()}: speed is empty")
                if name == "z":
                    g.set_motion("z", v["speed_mm_s"], Z_RAMP_MM_S2, Z_RAMP_MM_S2,
                                 v.get("speed_to_home_mm_s"), CONFIG_DIR / "gantry.json" if save else None)
                else:
                    g.set_motion(name, v["speed_mm_s"], v.get("accel_mm_s2", g.config.axes[name].accel_mm_s2),
                                 v.get("decel_mm_s2"), None, CONFIG_DIR / "gantry.json" if save else None)
        except Exception as e:
            ui.notify(f"Not applied: {e}", type="negative")
            return False
        speed_lbl.set_text(speed_text(g.speed_percent))
        ui.notify("Motion settings " + ("saved to gantry.json and applied" if save else "applied (not saved yet)"),
                  type="positive")
        return True

    def motion_board():
        if not g.is_connected:
            ui.notify("Connect the gantry first", type="warning")
            return
        lines = [f"Speed slider {g.speed_percent:.0f} %. On the drives now (limits of the last move):"]
        for name in ("x", "y", "z"):
            try:
                b_ = g.read_motion(name)
            except Exception as e:
                lines.append(f"{name.upper()}: could not read ({e})")
                continue
            lines.append(f"{name.upper()}: {b_['vel_limit']:6.1f} mm/s   ramp up {b_['accel_limit']:6.0f}   "
                         f"ramp down {b_['decel_limit']:6.0f} mm/s2"
                         + (f"   (overspeed trip {b_['controller_vel_limit'] * 1.2:.0f} mm/s)"
                            if "controller_vel_limit" in b_ else "   (simulation)"))
        motion_board_lbl.set_text("\n".join(lines))

    async def reload_settings_ui():
        try:
            changed = await act(g.reload_settings, CONFIG_DIR / "gantry.json", fail="Reload failed")
        except Exception as e:
            ui.notify(f"Reload failed: {e}", type="negative")
            return
        if changed is None:
            return
        motion_read()
        tune_read()
        speed_slider.set_value(g.speed_percent)
        speed_lbl.set_text(speed_text(g.speed_percent))
        ui.notify("Reloaded from gantry.json: " + ("; ".join(changed) if changed else "nothing changed"),
                  type="positive", multi_line=True, timeout=10000)

    def motion_save():
        confirm("Save motion settings", ["Writes the speeds and ramps of X, Y and Z to gantry.json and applies them now.",
                                         "", "Save?"], "Save", lambda: motion_apply(True))

    def refresh_calibration():
        try:
            status = g.calibration_status()
        except Exception as e:  # USB hiccup while reading - try again next tick
            logger.debug(f"calibration status: {e}")
            return
        for name, st in status.items():
            row = cal_rows[name]
            if st["stand_in"]:
                src_txt, ok_color = "stand-in (not wired)", "grey-6"
            elif st["simulated"]:
                src_txt, ok_color = "simulation", "grey-6"
            else:
                src_txt, ok_color = ("ODrive connected" if st["connected"] else "ODrive not connected"), "green-7"
            row["src"].set_text(src_txt)
            for key, badge, label in (("motor", row["motor"], "motor"), ("encoder_offset", row["enc"], "encoder offset"),
                                      ("prepared", row["ready"], "ready")):
                ok = bool(st.get(key)) and st["connected"]
                badge.set_text(f"{label} {'✓' if ok else '✗'}")
                badge.props(f"color={ok_color if ok else ('red-7' if st['connected'] else 'grey-5')}")
            r, l_ = st.get("resistance"), st.get("inductance")
            row["rl"].set_text(f"R {r:.3f} Ω · L {l_ * 1e3:.2f} mH" if isinstance(r, float) and isinstance(l_, float)
                               else "")
            k = g.config.axes[name].turns_per_mm
            row["scale"].set_text(f"scale {k:.6g} turns/mm = {1 / k:.4g} mm/turn")
            for btn in row["btns"]:
                btn.set_enabled(st["connected"] and not st["stand_in"] and not runner.is_active and not m.busy)

    # ------------------------------------------------------------------
    # Sample manager
    # ------------------------------------------------------------------
    def bump():
        """Registry or queue changed: every open page refreshes its grids, run order and deck marks."""
        m.samples_version += 1

    def snapshot():
        m.queue_undo.append((queue.name, copy.deepcopy(queue.entries)))
        del m.queue_undo[:-20]

    def undo_queue():
        if not m.queue_undo:
            ui.notify("Nothing to undo", type="info")
            return
        queue.name, queue.entries = m.queue_undo.pop()
        bump()
        ui.notify("Undone")

    def add_entry(slot: str, well: str):
        role = reg.get(slot, well).role
        if role == "empty":
            ui.notify(f"{well_label(slot, well)} is marked empty - change its role in Positions to use it",
                      type="warning")
            return
        kind = {"wash": "wash", "blank": "blank"}.get(role, "sample")
        dwell = q_wash_dwell.value if kind == "wash" else q_dwell.value
        if kind == "sample":
            wash = split_key(q_wash.value) if q_autowash.value and q_wash.value else None
            if q_autowash.value and not q_wash.value:
                ui.notify("Auto wash is on but no bottle is marked 'wash' in Positions", type="warning")
            queue.add_sample(slot, well, wash=wash, wash_dwell_s=float(q_wash_dwell.value or 0),
                             dwell_s=float(dwell or 0), handshake=bool(q_hs.value))
        else:
            queue.add(kind, slot, well, dwell_s=float(dwell or 0), handshake=False)
        bump()
        q_last.set_text(f"Added #{len(queue.entries)}: {where(slot, well)} ({kind})")

    def add_to_samples():
        if not m.selected:
            ui.notify("Select a bottle first", type="warning")
            return
        add_entry(*m.selected)
        ui.notify(f"Added {well_label(*m.selected)} as #{len(queue.entries)} in the run order")

    def add_all_samples():
        snapshot()
        n = 0
        for b_ in reg.bottles.values():
            if b_.role == "sample":
                add_entry(b_.slot, b_.well)
                n += 1
        ui.notify(f"Added {n} sample positions in deck order")

    def quick_build_ui():
        """Build a whole run order at once: N samples in row/column order with washes / blanks between."""
        slot = "bottles"
        lw = deck.slot(slot).labware
        available = len(reg.sample_wells(slot))
        if not available:
            ui.notify("No bottles are marked 'sample' in Positions", type="warning")
            return

        def bottle_options(role: str) -> Dict[str, str]:
            return {b_.key: well_label(b_.slot, b_.well) + (f" '{b_.name}'" if b_.name else "")
                    for b_ in reg.with_role(role)}
        washes, blanks = bottle_options("wash"), bottle_options("blank")
        dialog_host.clear()
        with dialog_host, ui.dialog().props("persistent") as dlg, ui.element("div").classes("u-dialog w-[560px]"):
            with ui.element("div").classes("u-dialog-title"):
                ui.label("Quick build - run order")
            with ui.column().classes("p-4 gap-3 w-full"):
                with ui.row().classes("items-end gap-3 w-full"):
                    n_in = ui.number("Number of samples", value=min(10, available), min=1, max=available,
                                     format="%.0f").props("dense outlined").classes("w-40")
                    first_in = ui.select(list(lw.wells()), value="A1", label="First bottle") \
                        .props("dense outlined").classes("w-28")
                    order_in = ui.toggle({"row": "Row by row", "column": "Column by column"}, value="row") \
                        .props("dense unelevated no-caps toggle-color=primary")
                with ui.row().classes("items-center gap-3 w-full"):
                    dwell_in = ui.number("Sample dwell s", value=float(q_dwell.value or 0), min=0) \
                        .props("dense outlined").classes("w-36")
                    hs_in = ui.checkbox("ÄKTA handshake per sample", value=bool(q_hs.value)).props("dense")
                    hs_in.set_enabled(bool(handshake))
                with ui.row().classes("items-center gap-3 w-full"):
                    wash_on = ui.checkbox("Wash between samples", value=bool(washes)).props("dense")
                    wash_in = ui.select(washes, value=next(iter(washes), None), label="Wash bottle") \
                        .props("dense outlined").classes("w-44")
                    wash_s = ui.number("Wash s", value=float(q_wash_dwell.value or 10), min=0) \
                        .props("dense outlined").classes("w-24")
                    wash_last = ui.checkbox("also after the last", value=False).props("dense")
                with ui.row().classes("items-center gap-3 w-full"):
                    blank_on = ui.checkbox("Blank / buffer between samples", value=False).props("dense")
                    blank_in = ui.select(blanks, value=next(iter(blanks), None), label="Blank bottle") \
                        .props("dense outlined").classes("w-44")
                    blank_s = ui.number("Blank s", value=10, min=0).props("dense outlined").classes("w-24")
                if not washes or not blanks:
                    ui.label("Wash / blank bottles are the ones marked wash / blank in Positions"
                             + ("" if washes else " (none marked wash yet)")
                             + ("" if blanks else " (none marked blank yet)") + ".") \
                        .classes("text-[11px] text-gray-600")
                ui.label(f"Sample bottles are taken from the first bottle on, skipping wash / blank / empty bottles "
                         f"({available} marked sample).").classes("text-[11px] text-gray-600")
                preview = ui.label().classes("text-[12px] font-mono whitespace-pre-wrap")
                mode_in = ui.radio({"replace": "Replace the run order", "append": "Add to the end"},
                                   value="replace").props("inline dense")

                def entries():
                    return SampleQueue.build(
                        reg, int(n_in.value or 0), slot, first_in.value or "A1", order_in.value,
                        sample_dwell_s=float(dwell_in.value or 0), handshake=bool(hs_in.value),
                        wash=split_key(wash_in.value) if wash_on.value and wash_in.value else None,
                        wash_dwell_s=float(wash_s.value or 0), wash_after_last=bool(wash_last.value),
                        blank=split_key(blank_in.value) if blank_on.value and blank_in.value else None,
                        blank_dwell_s=float(blank_s.value or 0))

                def show_preview(*_):
                    try:
                        es = entries()
                    except ValueError as e:
                        preview.set_text(str(e))
                        preview.classes(replace="text-[12px] font-mono whitespace-pre-wrap text-red-700")
                        build_b.set_enabled(False)
                        return
                    names = [e.well if e.kind == "sample" else e.kind for e in es]
                    shown = " → ".join(names[:16]) + (" → …" if len(names) > 16 else "")
                    n_s = sum(e.kind == "sample" for e in es)
                    preview.set_text(f"{shown}\n{len(es)} entries: {n_s} samples"
                                     + "".join(f", {sum(e.kind == k for e in es)} {k}es" if k == "wash"
                                               else f", {sum(e.kind == k for e in es)} {k}s"
                                               for k in ("wash", "blank") if any(e.kind == k for e in es)))
                    preview.classes(replace="text-[12px] font-mono whitespace-pre-wrap text-gray-800")
                    build_b.set_enabled(True)

                def build():
                    try:
                        es = entries()
                    except ValueError as e:
                        ui.notify(str(e), type="warning")
                        return
                    snapshot()
                    if mode_in.value == "replace":
                        queue.entries = es
                    else:
                        queue.entries.extend(es)
                    bump()
                    dlg.close()
                    ui.notify(f"Run order: {len(queue.entries)} entries - name it and Save to keep it "
                              "(Undo is available)", type="positive")

                with ui.row().classes("w-full justify-end gap-2"):
                    plain_button("Cancel", dlg.close)
                    build_b = set_button("Build", build, icon="auto_awesome")
                for w in (n_in, first_in, order_in, dwell_in, hs_in, wash_on, wash_in, wash_s, wash_last,
                          blank_on, blank_in, blank_s):
                    w.on_value_change(show_preview)
                show_preview()
        dlg.open()

    def insert_washes():
        if not q_wash.value:
            ui.notify("Mark a bottle as 'wash' in Positions first", type="warning")
            return
        snapshot()
        queue.insert_washes(*split_key(q_wash.value), dwell_s=float(q_wash_dwell.value or 0))
        bump()

    def remove_washes():
        washes = [e for e in queue.entries if e.kind == "wash"]
        if not washes:
            ui.notify("There are no washes in the list", type="info")
            return
        snapshot()
        q_wash_dwell.value = washes[0].dwell_s  # remembered for the next "Insert washes between"
        queue.remove_washes()
        bump()

    async def remove_selected():
        rows = await q_grid.get_selected_rows()
        if not rows:
            ui.notify("Select rows first (click a row or tick its box)", type="info")
            return
        snapshot()
        for idx in sorted((r["idx"] for r in rows), reverse=True):
            queue.remove(idx)
        bump()
        ui.notify(f"Removed {len(rows)} entr{'y' if len(rows) == 1 else 'ies'} - Undo is available")

    def clear_queue():
        if not queue.entries:
            return

        def do():
            snapshot()
            queue.entries.clear()
            bump()
            ui.notify("Run order cleared - Undo is available")
        confirm("Clear run order", [f"Remove all {len(queue.entries)} entries from '{queue.name}'?",
                                    "You can Undo this."], "Clear", do, danger=True)

    async def reorder_from_grid():
        rows = await q_grid.get_client_data(method="filtered_sorted")
        order = [r["idx"] for r in rows]
        if sorted(order) == list(range(len(queue.entries))) and order != list(range(len(queue.entries))):
            snapshot()
            queue.entries = [queue.entries[i] for i in order]
            bump()

    def on_queue_edit(data: dict):
        idx = data.get("idx")
        if idx is None or not 0 <= idx < len(queue.entries):
            return
        e = queue.entries[idx]
        if data.get("kind") in KINDS:
            e.kind = data["kind"]
        try:
            e.dwell_s = max(0.0, float(data.get("dwell_s") or 0))
            if e.kind == "wash":
                q_wash_dwell.value = e.dwell_s  # keep the wash default in step with edits
        except (TypeError, ValueError):
            pass
        if data.get("depth") in ("sample", "top"):
            e.depth = data["depth"]
        e.handshake = bool(data.get("handshake")) and handshake is not None
        bump()

    def on_position_edit(data: dict):
        if "key" not in data:
            return
        slot, well = split_key(data["key"])
        role = data.get("role") if data.get("role") in ROLES else "sample"
        reg.update(slot, well, name=str(data.get("name") or ""), role=role, notes=str(data.get("notes") or ""))
        reg.save()
        bump()

    def rename_queue():
        if (q_name.value or "") != queue.name:
            queue.name = q_name.value or "Sample list"
            bump()

    def save_list():
        rename_queue()
        fname = "".join(c if c.isalnum() or c in "-_" else "_" for c in queue.name).strip("_") + ".json"
        queue.save(SAMPLE_LIST_DIR / fname)
        q_file.set_options(list_files(), value=fname)
        ui.notify(f"Saved sample_lists/{fname}")

    def open_list(name: Optional[str]):
        if not name:
            ui.notify("Choose a saved list first", type="warning")
            return

        def do():
            snapshot()
            loaded = SampleQueue.load(SAMPLE_LIST_DIR / name)
            queue.name, queue.entries = loaded.name, loaded.entries
            bump()
            ui.notify(f"Opened {name}")
        if queue.entries:
            confirm("Open sample list", [f"Replace the current list ({len(queue.entries)} entries) with '{name}'?",
                                         "You can Undo this."], "Open", do)
        else:
            do()

    def queue_method() -> Optional[Sequence]:
        try:
            rename_queue()
            return queue.to_sequence(deck, handshake, reg, home_first=q_home.value,
                                     end_position=None if q_end.value == "(none)" else q_end.value,
                                     pause_in_sample=q_pause.value)
        except Exception as e:
            ui.notify(f"Cannot create the method: {e}", type="negative")
            return None

    def queue_to_method():
        if seq := queue_method():
            set_method(seq, from_queue=True)
            modules.set_value(t_method)
            ui.notify("Method created - press Play (▶) to run it", type="positive")

    def queue_run():
        if seq := queue_method():
            set_method(seq, from_queue=True)
            start_run(seq)

    def refresh_samples_ui():
        """Re-render everything that depends on the bottle registry or the draft run order."""
        pos_grid.options["rowData"] = [
            {"key": b_.key, "pos": "Sample" if b_.slot == "sample" else b_.well,
             "name": b_.name, "role": b_.role, "notes": b_.notes}
            for b_ in reg.bottles.values()]
        pos_grid.update()
        q_grid.options["rowData"] = [
            {"idx": i, "n": i + 1, "kind": e.kind, "key": e.key, "pos": where(e.slot, e.well),
             "name": reg.get(e.slot, e.well).name, "dwell_s": e.dwell_s, "depth": e.depth,
             "handshake": e.handshake}
            for i, e in enumerate(queue.entries)]
        q_grid.update()
        if q_name.value != queue.name:
            q_name.value = queue.name
        washes = [b_.key for b_ in reg.with_role("wash")]
        q_wash.set_options(washes, value=q_wash.value if q_wash.value in washes else (washes[0] if washes else None))
        q_undo.set_enabled(bool(m.queue_undo))
        roles_sig = tuple((b_.key, b_.role, b_.name) for b_ in reg.bottles.values())
        if roles_sig != last.get("roles"):  # rebuild only when roles change, so fast clicks aren't lost
            last["roles"] = roles_sig
            rebuild_quick_add()

    def rebuild_quick_add():
        quick_box.clear()
        with quick_box:
            bottles_slot = deck.slots.get("bottles")
            if bottles_slot:
                lw = bottles_slot.labware
                with ui.grid(columns=lw.cols).classes("gap-1 justify-center w-full"):
                    for r_ in range(lw.rows):  # row A on top, A1 top-left
                        for c_ in range(lw.cols):
                            w = lw.well_name(r_, c_)
                            info = reg.get("bottles", w)
                            color = {"wash": "light-blue-3", "blank": "grey-2", "empty": "white"}.get(info.role, "grey-4")
                            ui.button(w, on_click=lambda w=w: add_entry("bottles", w)) \
                                .props(f'unelevated dense no-caps color={color} text-color=black '
                                       f'aria-label="Add {w}"') \
                                .classes("u-qbtn justify-self-center").tooltip(info.display)
            if "sample" in deck.slots:
                plain_button("Sample position", lambda: add_entry("sample", "A1"), icon="science").classes("w-full")
            set_button("+ Wash", lambda: add_entry(*split_key(q_wash.value)) if q_wash.value
                       else ui.notify("No wash bottle defined", type="warning"), icon="water_drop").classes("w-full")

    # ------------------------------------------------------------------
    # Method helpers
    # ------------------------------------------------------------------
    def method_is_stale() -> bool:
        """The loaded method was built from the sample list, which has changed since."""
        return current["from_queue"] is not None and current["from_queue"] != m.samples_version

    def shown_method() -> Optional[Sequence]:
        if runner.is_active:
            return runner.sequence
        seq = current["seq"]
        return None if seq is None or method_is_stale() else seq

    def set_method(seq: Optional[Sequence], from_queue: bool = False):
        current["seq"] = seq
        current["from_queue"] = m.samples_version if (seq is not None and from_queue) else None
        steps_box.clear()
        step_rows.clear()
        seq_title.set_text(seq.name if seq else "No method open")
        tb_method.set_text(f"Method: {seq.name}" if seq else "No method")
        last["index"] = "reset"
        last["ro_key"] = None
        if not seq:
            return
        with steps_box:
            for i, st in enumerate(seq.steps):
                step_rows.append(ui.label(f"{i + 1:>3}  {st.describe()}").classes("px-2 py-[3px] w-full"))

    def load_file(name: Optional[str]):
        if not name:
            ui.notify("Choose a method file first", type="warning")
            return
        try:
            set_method(Sequence.load(SEQUENCE_DIR / name, deck, handshake))
            ui.notify(f"Opened {name}")
        except Exception as e:
            ui.notify(f"Could not open {name}: {e}", type="negative")

    def save_current():
        seq = current["seq"]
        if not seq:
            ui.notify("No method to save", type="warning")
            return
        if not seq.items:
            ui.notify("This method was built from a sample list - save the list in the Sample Manager",
                      type="info")
            return
        fname = "".join(c if c.isalnum() or c in "-_" else "_" for c in seq.name).strip("_") + ".json"
        seq.save(SEQUENCE_DIR / fname)
        file_sel.set_options(seq_files(), value=fname)
        ui.notify(f"Saved sequences/{fname}")

    def method_summary(seq: Sequence) -> List[str]:
        moves = [st for st in seq.steps if st.type == "move_to_well"]
        kinds = {"Sample": 0, "Wash": 0, "Blank": 0}
        for mv in moves:
            for k in kinds:
                if getattr(mv, "label", "").startswith(k):
                    kinds[k] += 1
        counted = sum(kinds.values())
        parts = [f"{n} {k.lower()}{'s' if n != 1 else ''}" for k, n in kinds.items() if n]
        if len(moves) - counted:
            parts.append(f"{len(moves) - counted} other")
        lines = [f"Method: {seq.name}",
                 f"{len(moves)} bottle visit{'s' if len(moves) != 1 else ''}" + (f" ({', '.join(parts)})" if parts else "")]
        lines.append("Homes the gantry first (about 20 s)" if seq.steps and seq.steps[0].type == "home"
                     else "Starts from the current position (no homing)")
        uses_akta = any(st.type in ("wait_for_akta", "signal_akta") for st in seq.steps)
        lines.append("ÄKTA handshake ON - each sample waits for the ÄKTA's request" if uses_akta
                     else "ÄKTA handshake off - timing by dwell only")
        if seq.steps and isinstance(seq.steps[-1], MoveToPositionStep):
            lines.append(f"Ends at: {seq.steps[-1].name}")
        return lines

    def start_run(seq: Optional[Sequence] = None):
        rebuilt = False
        if seq is None:
            seq = current["seq"]
            if (seq is None or method_is_stale()) and queue.entries:
                seq = queue_method()  # Play with a (changed) sample list: build the method from it
                if seq is None:
                    return
                set_method(seq, from_queue=True)
                rebuilt = True
        if not seq:
            ui.notify("Nothing to run - build a sample list in the Sample Manager first", type="warning")
            return
        if runner.is_active or m.busy:
            ui.notify("The gantry is busy", type="warning")
            return
        if not g.is_connected:
            ui.notify("Connect the gantry first (toolbar → Connect)", type="warning")
            return
        if not g.is_homed and seq.steps and seq.steps[0].type != "home":
            ui.notify("Home the gantry first, or tick 'Home first' in the Sample Manager", type="warning")
            return
        uses_akta = any(st.type in ("wait_for_akta", "signal_akta") for st in seq.steps)
        if uses_akta and (akta is None or not akta.connected):
            ui.notify("This method talks to the ÄKTA but the ÄKTA link is not connected "
                      "(ÄKTA menu → Connect link, or turn the handshake off)", type="negative")
            return
        lines = method_summary(seq)
        if rebuilt:
            lines.insert(1, "(built from the current sample list)")
        modules.set_value(t_sys)
        confirm("Start run", lines + ["", "Start now?"], "Start", lambda: runner.start(seq))

    def rerun_from_stop():
        seq = runner.sequence
        if seq is None or runner.stopped_index is None:
            return
        resumed = resume_from(seq, runner.stopped_index)
        first = resumed.steps[0]
        where_txt = first.describe().replace("Move to ", "") if first.type == "move_to_well" else "the start"
        if not g.is_homed:
            ui.notify("Home the gantry first (motors were turned off or homing was lost)", type="warning")
            return
        set_method(resumed)
        confirm("Re-run", [f"Re-run '{seq.name}' starting again at {where_txt}?",
                           f"{len(resumed.steps)} steps."] + method_summary(resumed)[1:],
                "Start", lambda: runner.start(resumed))

    def dismiss_stop():
        last["dismissed"] = (id(runner.sequence), runner.stopped_index)
        stop_banner.set_visibility(False)

    # ------------------------------------------------------------------
    # Run order pane (System Control)
    # ------------------------------------------------------------------
    def show_run_order():
        """Run order pane + deck badges: the running/loaded method, or the draft sample list."""
        seq = shown_method()
        ro_list.clear()
        ro_rows.clear()
        ro_index.clear()
        last["ro"] = None
        orders: Dict[str, List[str]] = {}
        with ro_list:
            if seq:
                ro_title.set_text(("Running: " if runner.is_active else "Method: ") + seq.name)
                n = 0
                for i, st in enumerate(seq.steps):
                    if st.type == "move_to_well":
                        n += 1
                        ro_index.append(i)
                        orders.setdefault(pos_key(st.slot, st.well), []).append(str(n))
                        text = f"{n:>2}.  {where(st.slot, st.well)}" + (f"  -  {st.label}" if st.label else "")
                        ro_rows.append(ui.label(text).classes("px-2 py-[2px] w-full"))
            elif queue.entries:
                ro_title.set_text(f"Draft list: {queue.name}  (Play creates the method)")
                for i, e in enumerate(queue.entries):
                    orders.setdefault(e.key, []).append(str(i + 1))
                    nm = reg.get(e.slot, e.well).name
                    ro_rows.append(ui.label(f"{i + 1:>2}.  {where(e.slot, e.well)}  -  {e.kind}"
                                            + (f" '{nm}'" if nm else ""))
                                   .classes("px-2 py-[2px] w-full text-gray-600"))
            else:
                ro_title.set_text("No run order - build one in the Sample Manager")
        marks = {}
        for b_ in reg.bottles.values():
            order = orders.get(b_.key, [])
            title = b_.display + (f" - {b_.notes}" if b_.notes else "") \
                + (f" - run order {', '.join(order)}" if order else "")
            badge = ",".join(order[:3]) + ("…" if len(order) > 3 else "")
            marks[bottle_key(b_.slot, b_.well)] = {"role": b_.role, "order": badge, "title": title}
        deck_view.set_marks(marks)

    def run_status_text() -> str:
        n = len(runner.steps)
        if not runner.is_active or not (0 <= runner.index < n):
            return ""
        st = runner.steps[runner.index]
        bottle = ""
        if runner.current_well:
            info = reg.get(*runner.current_well)
            bottle = where(*runner.current_well) + (f" ({info.name})" if info.name else "")
        if runner.state == RunnerState.PAUSED:
            doing = f"PAUSED - {runner.message}"
        elif st.type == "dwell" and runner.step_started:
            left = max(0.0, st.seconds - (time.time() - runner.step_started))
            doing = f"{st.describe()} - {left:.0f} s left"
        else:
            doing = runner.message or st.describe()
        prefix = ""
        if runner.end_pending:
            prefix = "Ending after this bottle · " if runner.needle_down else "Ending · "
        elif runner.pause_pending:
            prefix = "Pausing after this step · "
        return prefix + (f"{bottle} · " if bottle else "") + f"step {runner.index + 1}/{n} · {doing}"

    # ------------------------------------------------------------------
    # Periodic refresh: process picture at 20 Hz, the rest at 5 Hz
    # ------------------------------------------------------------------
    def fast_tick():
        pos = g.get_position()
        homed = g.is_homed
        target = None
        if g.is_moving() and homed:
            tx, ty = g.x_axis.status.target_position, g.y_axis.status.target_position
            if tx is not None or ty is not None:
                kx, ky = g.config.axes["x"].turns_per_mm, g.config.axes["y"].turns_per_mm
                target = (tx / kx if tx is not None else pos.x, ty / ky if ty is not None else pos.y)
        deck_view.update(pos.x, pos.y, pos.z, target, m.selected, homed=homed)
        for box, unit, value in ((v_x, u_x, pos.x), (v_y, u_y, pos.y), (v_z, u_z, pos.z)):
            if homed:
                box.set_text(f"{round(value, 1) + 0.0:.1f}")
                unit.set_text(f"mm · {value / MM_PER_IN:.2f} in")
            else:
                box.set_text("—")
                unit.set_text("not homed")
        last["n"] += 1
        if last["n"] % 4 == 0:
            slow_tick()
        if last["n"] % 20 == 0:  # 1 Hz: calibration state read from the boards
            refresh_calibration()

    def slow_tick():
        if last["ver"] != m.samples_version:
            last["ver"] = m.samples_version
            refresh_samples_ui()
        shown = shown_method()
        ro_key = (id(shown), runner.is_active, m.samples_version if shown is None else 0)
        if ro_key != last["ro_key"]:
            last["ro_key"] = ro_key
            show_run_order()

        active = runner.is_active
        homed = g.is_homed
        st = g.get_status()

        # current-bottle highlight in the run order
        n_ro = len(ro_index)
        mine = shown is not None and runner.sequence is shown
        cur = -1
        if mine:
            cur = n_ro if runner.state == RunnerState.COMPLETED else \
                max((k for k, si in enumerate(ro_index) if si <= runner.index), default=-1)
        if cur != last["ro"]:
            last["ro"] = cur
            for k, row in enumerate(ro_rows):
                row.classes(remove="u-step-current u-step-done")
                if k == cur and active:
                    row.classes(add="u-step-current")
                elif k < cur:
                    row.classes(add="u-step-done")
        state_txt = "pausing…" if runner.pause_pending else "ending…" if runner.end_pending else runner.state.value
        ro_state.set_text(state_txt if mine else ("ready" if shown else "-"))
        ro_state.props(f"color={_STATE_COLORS[runner.state] if mine else 'grey-6'}")
        ro_bar.set_value(min(1.0, cur / n_ro) if n_ro and cur > 0 else 0)
        status = run_status_text()
        ro_status.set_text(status)
        ro_status.set_visibility(bool(status))

        # gantry state box
        state = st["state"].replace("_", " ")
        if active:
            v_state.set_text("RUNNING" if runner.state == RunnerState.RUNNING else "PAUSED")
            u_state.set_text("method in progress")
        else:
            v_state.set_text("HOMED" if homed else ("NOT HOMED" if g.is_connected else "OFFLINE"))
            u_state.set_text(state + (" · busy" if m.busy else ""))
        color = "#2e7d32" if g.is_connected and homed else ("#ef8f00" if g.is_connected else "#9e9e9e")
        if st["state"] in ("error", "emergency_stop"):
            color = "#c62828"
        sys_dot.set_content(f'<span class="u-dot" style="background:{color}"></span>')

        # prefill the Move fields after each completed move / homing
        moving = g.is_moving() or m.busy
        if homed and ((last["moving"] and not moving) or last["homed"] is False):
            fill_move_fields()
        last["moving"], last["homed"] = moving, homed

        # enable / disable
        for el, needs_homed in lockable:
            el.set_enabled(not active and not m.busy and (homed or not needs_homed))
        dlg_hint.set_visibility(not homed)
        for btn, enabled in ((run_btn, not active), (tb_run, not active),
                             (pause_btn, runner.state == RunnerState.RUNNING and not runner.pause_pending),
                             (tb_pause, runner.state == RunnerState.RUNNING and not runner.pause_pending),
                             (resume_btn, runner.state == RunnerState.PAUSED),
                             (tb_cont, runner.state == RunnerState.PAUSED),
                             (abort_btn, active and not runner.end_pending),
                             (tb_end, active and not runner.end_pending)):
            btn.set_enabled(enabled)

        # banners
        homing = st["state"] == "homing"
        need_home_banner = not active and (not g.is_connected or not homed)
        home_banner.set_visibility(need_home_banner)
        if need_home_banner:
            if not g.is_connected:
                home_lbl.set_text("Gantry not connected - connect it to start.")
            elif homing or m.busy:
                home_lbl.set_text("Working (calibration / homing)… positions are shown when it finishes.")
            else:
                unprepared = needs_calibration()
                faulted = [n.upper() for n, a in g.axes.items() if a.is_faulted]
                if faulted:
                    text = f"{', '.join(faulted)} faulted - click Reset faults."
                elif unprepared:
                    text = (f"Step 1: calibrate {', '.join(unprepared)} (encoder offset, needed after every "
                            f"power-up; motors turn slightly). Jog is blocked until then.")
                elif g.manual_homing:
                    text = (f"Step 2: jog (or push) the gantry to the home corner - {HOME_CORNER} - "
                            "then click Set home here.")
                else:
                    text = "Gantry not homed - moves are disabled until it is homed. Jog still works."
                home_lbl.set_text(text)
            unprepared = needs_calibration()
            faulted_any = any(a.is_faulted for a in g.axes.values())
            home_connect_btn.set_visibility(not g.is_connected)
            home_reset_btn.set_visibility(g.is_connected and faulted_any)
            home_cal_btn.set_visibility(g.is_connected and bool(unprepared) and not faulted_any and not homing)
            home_home_btn.set_visibility(g.is_connected and not homing and not unprepared and not faulted_any)
            home_limits_btn.set_visibility(home_home_btn.visible)
            for b in (home_reset_btn, home_cal_btn, home_home_btn, home_limits_btn):
                b.set_enabled(not m.busy)

        pause_banner.set_visibility(runner.state == RunnerState.PAUSED)
        pause_lbl.set_text(runner.message + "   (manual moves are disabled while paused - End the run to move manually)")

        stopped = runner.state in (RunnerState.ABORTED, RunnerState.FAILED) and runner.stopped_index is not None
        key = (id(runner.sequence), runner.stopped_index)
        show_stop = stopped and last["dismissed"] != key and not active
        stop_banner.set_visibility(show_stop)
        if show_stop:
            bottle = where(*runner.current_well) if runner.current_well else "no bottle"
            head = "STOPPED" if runner.state == RunnerState.ABORTED else f"RUN FAILED: {runner.message}"
            needle = "needle is DOWN in the bottle" if runner.needle_down else "needle is up"
            tail = "" if homed else " Motors were turned off or homing was lost - home before moving."
            stop_lbl.set_text(f"{head} at step {runner.stopped_index + 1} ({bottle}), {needle}. "
                              f"Nothing will move until you choose.{tail}")
            stop_raise.set_enabled(homed and not m.busy)
            stop_park.set_enabled(homed and not m.busy)
            stop_rerun.set_enabled(not m.busy)

        # method editor outline
        mine_editor = current["seq"] is not None and runner.sequence is current["seq"]
        run_state.set_text(state_txt if mine_editor else ("ready" if current["seq"] else "-"))
        run_state.props(f"color={_STATE_COLORS[runner.state] if mine_editor else 'grey-6'}")
        n_steps = len(runner.steps)
        progress = (max(runner.index, 0) / n_steps) if n_steps and mine_editor else 0
        if mine_editor and runner.state == RunnerState.COMPLETED:
            progress = 1.0
        run_bar.set_value(progress)
        run_msg.set_text(status if mine_editor else ("Method changed since it was built - Play rebuilds it "
                                                    "from the sample list" if method_is_stale() else ""))
        idx = runner.index if mine_editor else None
        if idx != last["index"]:
            for i, row in enumerate(step_rows):
                row.classes(remove="u-step-current u-step-done")
                if idx is not None and i == idx and active:
                    row.classes(add="u-step-current")
                elif idx is not None and i < idx:
                    row.classes(add="u-step-done")
            last["index"] = idx

        # status bar
        if active and runner.sequence:
            sb_run.set_text(f"● {runner.sequence.name}: {state_txt}")
        elif runner.state in (RunnerState.ABORTED, RunnerState.FAILED, RunnerState.ENDED, RunnerState.COMPLETED) \
                and runner.sequence:
            sb_run.set_text(f"● Last run {runner.state.value}")
        else:
            sb_run.set_text("● Manual")
        sb_step.set_text(status or "Block: -")
        sb_gantry.set_text(f"Gantry: {state}{' · homed' if homed else ' · not homed'}")

        axis_table.rows = [
            {"axis": n.upper(), "state": a["state"], "position": f"{a['position']:.2f}",
             "velocity": f"{a['velocity']:.1f}", "torque": f"{a['torque']:.2f}",
             "homed": "yes" if a["is_homed"] else "no", "errors": "; ".join(a["errors"][-2:])}
            for n, a in st["axes"].items()
        ]

        for seq_no, line in m.log.since(last["log"]):
            log_view.push(line)
            last["log"] = seq_no

        # ÄKTA
        if akta is not None:
            ast = akta.status()
            values = {s["name"]: s["value"] for s in ast["signals"]}
            if akta.connected:
                parts = [str(values[k]) for k in ("run_state", "phase") if values.get(k) is not None]
                text = "ÄKTA" + (" (simulation)" if ast["simulated"] else "") + (": " + " · ".join(parts) if parts else "")
                dot = "#2e7d32"
            else:
                text, dot = "ÄKTA: offline", "#c62828"
            akta_tab_lbl.set_text(text)
            akta_tab_lbl.props(f'title="{attr(text)}"')
            akta_dot.set_content(f'<span class="u-dot" style="background:{dot}"></span>')
            sb_akta.set_text(f"Connection: {text}")
            sb_akta.props(f'title="{attr(text)}"')
            akta_table.rows = [
                {"signal": s["name"], "source": s["source"],
                 "value": "—" if s["value"] is None else str(s["value"]),
                 "age": "" if s["age_s"] is None else f"{s['age_s']:.1f} s",
                 "io": "out" if s["output"] else "in", "description": s["description"]}
                for s in ast["signals"]
            ]
            key = tuple((n, b["connected"], b["error"]) for n, b in ast["backends"].items())
            if key != last["akta"]:
                last["akta"] = key
                akta_backends.clear()
                with akta_backends:
                    for n, b in ast["backends"].items():
                        ui.badge(f"{n}: {'connected' if b['connected'] else 'offline'}",
                                 color="green-7" if b["connected"] else "red-7").props("rounded")
                        if b["error"] and not b["connected"]:
                            ui.label(b["error"]).classes("text-xs text-red-700")
        else:
            sb_akta.set_text("ÄKTA link off")

    ui.timer(0.05, fast_tick)
    if m.selected:  # restore the selection after a page reload
        ui.timer(0.5, show_selection, once=True)


# ============================================================================
# ENTRY
# ============================================================================

def main(gantry: Gantry, deck: Deck, akta: Optional[AktaLink] = None, *, host: str = "127.0.0.1",
         port: int = 8080, native: bool = False, show: bool = True, auto_connect: bool = False):
    global MACHINE
    log = LogBuffer()
    logging.getLogger().addHandler(log)
    MACHINE = Machine(gantry, deck, SequenceRunner(gantry, deck, akta), log, akta,
                      registry=BottleRegistry(deck), queue=SampleQueue("Sample list"))

    if auto_connect:
        app.on_startup(lambda: run.io_bound(gantry.connect))
    if akta is not None:
        app.on_startup(lambda: run.io_bound(akta.connect))
        app.on_shutdown(akta.disconnect)
    app.on_shutdown(lambda: gantry.disconnect() if gantry.is_connected else None)

    ui.run(host=host, port=port, title="Autosampler - System Control", native=native, show=show,
           reload=False, dark=False, favicon="🧪", show_welcome_message=True)
