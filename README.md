# ÄKTA Autosampler

A gantry-only XYZ autosampler for an ÄKTA. There are no pumps here: the ÄKTA handles the fluidics, and the gantry puts the sample needle into the right vial at the right time.

The motion code is ported from ProteinMakerV5 (`system_configuration/odrive/`). Each axis is driven by an ODrive servo controller. The operator UI is built with [NiceGUI](https://nicegui.io).

```
akta_autosampler/
  hardware/axis.py         motion state machine, sensorless homing, limits (hardware-independent)
  hardware/odrive_axis.py  ODrive driver (odrive 0.6.x, imported lazily)
  hardware/sim_axis.py     simulated axis with hard stops
  gantry.py                XYZ in mm: move_to, safe_move_to, jog, stop, home_all
  deck.py                  labware grids and deck slots; well name -> XYZ
  sequence.py              sequence steps and the threaded runner (pause/resume/abort)
  akta/link.py             ÄKTA link: named signals, polling, wait/write, handshake
  akta/backends.py         UNICORN OPC UA client and LabJack (I/O-box E9) backends
  akta/sim.py              simulated ÄKTA method for testing handshakes
  tools/opcua_browse.py    lists UNICORN OPC UA node ids
  ui/app.py                NiceGUI operator UI
config/
  gantry.json              axes, serials, turns_per_mm, limits, speeds, motor params
  akta.json                ÄKTA link: mode, OPC UA endpoint, LabJack, signals, handshake
  deck.json                slots (origin + labware) and named positions (park, wash)
  labware/*.json           rack/plate definitions
  sequences/*.json         saved sequences
```

## Running in PyCharm

1. **File → Open** this folder.
2. PyCharm should pick up the `.venv` interpreter (Python 3.12). If it doesn't, go to **Settings → Project → Python Interpreter → Add → Existing → `.venv\Scripts\python.exe`**.
3. Choose a run configuration from the toolbar. They are shared via `.run/`:
   - **Autosampler (sim)**: simulated axes, so no hardware is needed.
   - **Autosampler (hardware)**: real ODrives.
   - **Tests**: pytest against the simulator.

The UI opens at http://127.0.0.1:8080.

From a terminal:

```bash
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m akta_autosampler --sim
.venv\Scripts\python -m pytest
```

Other flags: `--hardware`, `--port 8081`, `--no-browser`, `--debug`, and `--native`, which opens a desktop window and needs `pip install pywebview`.

## Coordinates

- Units are mm. Coordinates are **corner-origin**: each axis is 0 at its homed end and runs to `max_mm`.
- **Z = 0 is the top.** +Z moves the needle down.
- Homing is sensorless. Each axis drives slowly toward its `homing_direction` hard stop until it stalls or the torque rises, then backs off `homing_backoff_mm`. That point becomes 0.
- `safe_move_to` always raises Z to `z_safe_mm`, moves XY, then lowers. Sequences and the UI only travel this way.

## Using the UI

**Header**
- Live XYZ readout and homed status.
- **STOP** halts all axes with the motors still holding, and aborts the sequence.
- **Motors off** de-energises the motors. Re-home afterwards.

**Control tab**
- Connect.
- Home all, with Z homed first.
- Jog. This works before homing, with no limit checks.
- Go to XYZ.
- Named positions.
- Reset faults.

**Deck tab**
- Click a well to select it.
- **Go (top)**, **Lower to sample** and **Raise** move the needle.
- **Teach** calibrates from the current needle position:
  - *A1 is here* sets the slot origin in `deck.json`.
  - *Z top here* and *Z sample here* set the heights in the labware file.

**Sequence tab**
- Load a sequence file, or build a sample list: slot, wells such as `A1-A6, B1`, dwell, wash and end position.
- Run, Pause (takes effect after the current step), Resume and Abort. On abort the needle lifts to safe Z.

## Sequences

```json
{"name": "My run", "steps": [
  {"type": "home"},
  {"type": "samples", "slot": "rack1", "wells": "A1-A6", "depth": "sample",
   "dwell_s": 30, "pause_in_sample": true,
   "wash": {"position": "wash", "dwell_s": 5}, "end_position": "park"}
]}
```

Step types:

| Step | Fields |
| --- | --- |
| `home` | none |
| `move_to_well` | `slot`, `well` |
| `lower` | `depth`: `"sample"`, `"top"`, or mm below the top |
| `raise` | none |
| `dwell` | `seconds` |
| `pause` | `message` |
| `move_to_position` | `name` |
| `wait_for_akta` | `signal` plus one of `equals`, `not_equals`, `in`, `contains`; optional `timeout_s`, `message` |
| `signal_akta` | `signal`, `value`; optional `pulse_s` |
| `samples` | macro: expands to move, (wait for ÄKTA), lower, dwell, (signal ÄKTA), raise, (wash) for each well |

**ÄKTA sync:** use `akta_handshake: true` (see [ÄKTA integration](#äkta-integration)). For manual sync, use `pause_in_sample: true`: the runner waits with the needle in the vial until the operator clicks Resume.

## ÄKTA integration

The ÄKTA link is set up in `config/akta.json`, where `"mode"` is `sim`, `hardware` or `off`. Override it with `--akta sim|hardware|off`.

The link exposes **named signals**. Each signal is read from, or written to, one of two sources:

- **`opcua`**: the UNICORN OPC UA server, which needs the separate licence. Good for status: run state, phase and block, monitor values, and the ÄKTA's own digital I/O values.
- **`labjack`**: a LabJack T4/T7 (via LJM) or U3 wired to the **I/O-box E9**. This is a hardwired handshake and does not depend on OPC UA.

A background thread polls every input signal. The **ÄKTA** tab shows them live, with manual 0/1 buttons for the outputs, and the header shows the run state and phase.

Values use **UNICORN logic** for the I/O-box: `1` = open circuit and `0` = closed circuit to signal ground.

### Handshake (per vial)

| Step | Autosampler | ÄKTA method |
| --- | --- | --- |
| 1 | Moves over the vial and waits for `sample_request = 0` | `Digital out 1 = 0` ("ready for sample"), then hold or watch |
| 2 | Lowers the needle and sets `needle_ready = 0` | Watch `Digital in 1 = 0`, then continue with sample application |
| 3 | Waits for `sample_request = 1` | `Digital out 1 = 1` when sample application ends |
| 4 | Sets `needle_ready = 1`, raises, washes, moves to the next vial | Wash / elution / re-equilibration, then the next sample |

If a sequence is aborted or fails, every output returns to its `idle` value, so the ÄKTA never sees "needle ready" while the needle is out of the vial.

### Wiring: I/O-box E9 to LabJack T4

The E9 D-sub has digital in 1–4 on pins 1–4, **signal ground on pin 5**, and digital out 1–4 on pins 6–9 (ÄKTA pure manual §3.5.3).

| I/O-box pin | ÄKTA signal | LabJack T4 | Signal in akta.json |
| --- | --- | --- | --- |
| 6 | Digital out 1 | FIO4 (input, internal pull-up) | `sample_request` |
| 1 | Digital in 1 | FIO5 (open-drain output) | `needle_ready` |
| 5 | Signal ground | GND | none |

- The ÄKTA outputs are contacts closed to ground, so the LabJack pull-up reads open as 1 and closed as 0.
- To drive the ÄKTA inputs, the LabJack pulls the line low for 0 and releases it for 1 (`"drive": "open_drain"`). This avoids relying on the 3.3 V LabJack meeting the ÄKTA's 3.5 V logic-high level.
- On the T4, use FIO4–FIO7 or EIO lines. FIO0–3 are analog by default.
- Install the **LabJack LJM driver** from labjack.com before using `labjack-ljm`. For a U3, set `"device": "U3"` and `pip install LabJackPython`.

### Finding UNICORN OPC UA node ids

The node ids in `akta.json` are placeholders. List the real ones from the UNICORN instrument server:

```bash
.venv\Scripts\python -m akta_autosampler.tools.opcua_browse opc.tcp://UNICORN-PC:4840 --find "state|phase|block|digital" --out opcua_nodes.txt
```

Each line shows the path, node id, data type, current value and access (`R`/`RW`). Copy the node ids into `signals`.

- **Credentials:** if the server needs them, set `username` and put the password in the environment variable named by `password_env`. Never put it in the file.
- **Signed/encrypted endpoints:** set `security_string`.
- **Any node can be a signal.** For example, map the ÄKTA's `Digital out 1` via OPC UA instead of wiring it.
- **Writes over OPC UA:** if UNICORN marks a node as writable (`RW`), it can be an output (`"output": true`, plus `"type"` such as `"Int32"`). Otherwise, keep outputs on the LabJack.

## First power-on with hardware

Everything in `config/gantry.json` is a placeholder until you check it:

1. **Driver:** on Windows the ODrive may need the WinUSB driver (install it with Zadig) for `odrive.find_any` to see it. Check the board with `odrivetool` first.
2. **Serials and axis numbers:** set `serial` and `axis_number` per axis. The defaults come from the old ProteinMakerV5 test scripts.
3. **`turns_per_mm`:** set this from your belt/leadscrew pitch. The default of 0.1 is a guess.
4. **Limits:** start with low `current_soft_max` and `torque_soft_limit` and slow `homing_speed_mm_s`. Keep a hand on a hardware e-stop. The software STOP is not a safety device.
5. **Homing direction:** home one axis at a time (Control → Home X) and confirm that `homing_direction` drives toward the intended end. Z must home **up**.
6. **Teach the deck:**
   - Jog to A1 of each rack and click *A1 is here*.
   - Jog Z to just above a vial and click *Z top here*.
   - Jog down to sampling depth and click *Z sample here*.

## Changes from the ProteinMakerV5 gantry code

- Imports are package-relative, so the code works from any working directory.
- Homing finds one hard stop and zeros there. Before, it zeroed at the centre of travel, which contradicted the 0–300 mm limits. It also now honours the configured homing speed.
- Stall detection scales with the homing speed and ignores the acceleration phase. The old fixed 0.0001-turn threshold could false-trigger at 0.01 turn/s.
- Torque-limit and timeout faults are sticky until **Reset faults**. Before, a torque trip was reset to IDLE and reported as success.
- `move_to` validates every axis before moving any of them.
- The watchdog timeout is set explicitly.
- Axes that share a board share one connection.
- `motor_type` defaults to `PMSM_CURRENT_CONTROL`. `MotorType.HIGH_CURRENT` does not exist in odrive 0.6.x.
