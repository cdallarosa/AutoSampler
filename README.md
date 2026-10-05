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
   - **Autosampler**: the app, driving the real ODrives over USB. Close the ODrive GUI first: only one program can hold the USB link.
   - **Tests**: pytest. The tests use a built-in gantry simulator, so they need no hardware.

The UI opens at http://127.0.0.1:8080.

From a terminal:

```bash
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m akta_autosampler
.venv\Scripts\python -m pytest
```

Other flags: `--akta sim|hardware|off` (the ÄKTA link, default from `config/akta.json`), `--port 8081`, `--no-browser`, `--debug`, and `--native`, which opens a desktop window and needs `pip install pywebview`.

## Coordinates

- Units are mm. Coordinates are **corner-origin**: each axis is 0 at its homed end and runs to `max_mm`.
- **Z = 0 is the top.** +Z moves the needle down.
- Homing is sensorless. Each axis drives slowly toward its `homing_direction` hard stop until it stalls or the torque rises, then backs off `homing_backoff_mm`. That point becomes 0.
- `safe_move_to` always raises Z to `z_safe_mm`, moves XY, then lowers. Sequences and the UI only travel this way.

## Using the UI

The UI is laid out like UNICORN 7 System Control: a title bar with module tabs, a menu bar (File / View / Manual / System / ÄKTA / Help), a toolbar (Run, Pause, Continue, End, Home, Raise Z, Park, Connect, Reset, **STOP**), system tabs showing the gantry and ÄKTA status, and a status bar.

**Deck:** 24 × 32 in workspace with 30 bottles, 4 in in diameter, in a 5 × 6 grid on a 4.5 in pitch (A1–F5), plus one **sample position** at front-centre. Edit `config/deck.json` and `config/labware/*.json` to change it.

**System Control**
- **Process Picture:** a 2D top-down view of the deck.
  - Home is top-left, A1 is the top-left bottle, X runs right and Y runs down.
  - The gantry bridge and carriage move live, a dashed line and ring show where the gantry is going, and a Z gauge shows needle depth.
  - The bottle the needle is in turns green.
  - Wash bottles are blue and labelled WASH. A blue badge on each bottle shows its place(s) in the run order.
  - Click a bottle to select it. Its dialog opens right beside it: **Go** (move there and lower the needle to sampling depth), **Top** (needle at the bottle top), **Raise**, **Add to run order**.
  - Black value boxes show X, Y, Z and the gantry state.
- **Run order:** the bottles of the current method (or of the sample list, before it becomes a method). The current one is highlighted while running.
- **Run Log:** app messages.
- **Manual instructions:**
  - Selected bottle actions.
  - Safe move to X, Y, Z (**Set**).
  - Jog. This works before homing, with no limit checks.
  - Homing.

**Moves:** travel between bottles is a coordinated **vector move**. Each axis gets its speed and acceleration scaled to its share of the distance, so X and Y move together the whole way and the needle follows a straight diagonal line. Z always lifts to the safe height first and lowers last.

**Starting and stopping**
- **Play** (▶) runs the open method. If only a sample list exists, or it changed since the method was built, Play builds the method from it first. A **Start run** dialog summarises the run before anything moves: bottle visits, whether it homes first, whether the ÄKTA handshake is on, and the end position.
- **Pause** pauses after the current step. The badge shows "pausing…" until then.
- **End** (■) is graceful and asks first. The needle finishes the bottle it is in, raises, and the gantry parks (state *ended*).
- **STOP** halts all motion immediately and **nothing moves afterwards on its own**. A red recovery banner says where it stopped and whether the needle is still down, and offers **Raise needle**, **Park**, **Re-run from here…** (restarts at the stopped bottle) and **Dismiss**.
- **Motors off** asks first, then de-energises the motors. Re-home afterwards.
- Until the gantry is homed, a banner offers **Home all**. The position readouts show "—", and Go / Top / Raise / Move / Park are disabled. Jog still works.

**Sample Manager**: decide what is in each bottle and the order they are visited.
- **Positions:** name, role (sample / wash / blank / empty) and notes for every bottle. Saved to `config/samples.json`. F5 starts as the wash bottle.
- **Run order:** an ordered list such as *A1 → wash → A2 → wash → A3*.
  - Click bottles on the right to append them. Wash-role bottles are added as washes.
  - **+ Wash** appends a wash, **Insert washes between** puts one between every pair of samples, and **Remove washes** takes them out.
  - Drag rows to reorder. Double-click Type, Dwell, Depth or ÄKTA to edit.
  - Lists are saved to `config/sample_lists/`.
- **Send to Method Editor** turns the list into a method. **Create method & Run…** does that and asks to start it.
- **Clear…** asks first. Clear, Remove selected, Remove washes, Insert washes, reordering and Open can all be undone with **Undo**.
- **Pause in each sample for the operator** adds a manual pause with the needle in each sample (manual ÄKTA sync).
- With the ÄKTA handshake, the ÄKTA sets the pace: lower → signal ready → wait for done → (optional dwell) → raise.

**Method Editor**
- Shows the method outline (every step, highlighted while running). You can open or save method files here (`config/sequences/`).
- Sample lists are built only in the Sample Manager; there is no second builder here.

**Administration**
- **ÄKTA link:** signals, manual outputs and the handshake.
- **Axes:** diagnostics.
- **Teach:** set the A1 / position XY, bottle top Z and sampling depth Z from the current needle position.

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

A background thread polls every input signal. **Administration → ÄKTA link** shows them live, with manual 0/1 buttons for the outputs, and the header shows the run state and phase.

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

- **Credentials:** the user name and password come from the environment variables named by `username_env` / `password_env` (`AKTA_OPCUA_USER` / `AKTA_OPCUA_PASSWORD`). Never put them in the file.
- **Security (UNICORN):** the server only offers secured endpoints (Basic256Sha256 / Aes128_Sha256_RsaOaep, Sign or SignAndEncrypt, no anonymous login). Set `opcua.security` to a client `cert`/`key` the server **already trusts**; a new self-signed cert lands in UNICORN's `rejected/` folder until an admin trusts it. The application URI is read from the certificate (UNICORN refuses a mismatch), and the server certificate is fetched automatically.
- **Which server:** the only UNICORN OPC UA server found on the network is `opc.tcp://opcsrv:60434/OPC/HistoricalAccessServer` ("UNICORN OPC SERVER - HDA"). It serves *archived results*, not live run status. Live status for the handshake needs UNICORN's real-time OPC UA server; until that is located, use the LabJack signals.
- **Quick checks:** `--endpoints-only` lists endpoints without credentials; `--from-config` takes the endpoint, certificate and env-var names from `akta.json`. The HDA archive is very large, so keep `--depth` low (3–4) or start from a `--root` node.
- **Any node can be a signal.** For example, map the ÄKTA's `Digital out 1` via OPC UA instead of wiring it.
- **Writes over OPC UA:** if UNICORN marks a node as writable (`RW`), it can be an output (`"output": true`, plus `"type"` such as `"Int32"`). Otherwise, keep outputs on the LabJack.

## ODrive configuration and start-up

The boards are three **ODrive Pro** controllers (hardware v4.4, firmware 0.6.11), one axis each. The board-to-axis mapping is in `gantry.json`.

- **The configuration lives on the boards.** Set it in the ODrive web GUI, then save a copy into this repo:
  ```bash
  .venv\Scripts\python -m akta_autosampler.tools.odrive_backup
  ```
  This writes `config/odrive/<axis>_<serial>.json` with every `*.config.*` value. `--restore` writes a saved file back to the board and saves it.
- **Checking the config:** `gantry.json`'s `motor` and `board` sections mirror the boards. `tools.odrive_setup` (a dry run by default) shows any differences; `--apply` writes them.
- **Start-up procedure:** the encoders are incremental with no index pulse, so the encoder offset is lost at every power-up. **Home all** (or Home X/Y/Z) runs this per axis before homing:
  1. If the motor isn't calibrated (R/L invalid), it runs `FULL_CALIBRATION_SEQUENCE`. Otherwise, if the encoder offset isn't valid, it runs `ENCODER_OFFSET_CALIBRATION`. **The motor moves slightly**, about `calib_scan_distance`.
  2. Any ODrive procedure error, a timeout or STOP aborts it, and that axis is not homed.
  3. Then it homes against the hard stop, as before.
- **Calibrating from the app:** go to *Administration → Calibration (ODrive)*, or *Manual → Calibration…*. Each axis shows its board, the calibration flags (motor, encoder offset, ready) and the measured R/L. Each button asks for confirmation first, and STOP aborts it.
  - **Motor…** measures R/L. The motor beeps but doesn't turn. Needed once per motor, then use *Save to board*.
  - **Encoder offset…** is needed after every power-up. The motor turns slightly.
  - **Full…** runs both.
  - **Scale…** measures `turns_per_mm` (needs homing first). Mark the carriage, make a slow test move (50 mm at the current scale), and measure how far it really went. Apply + save then writes the corrected value into `gantry.json`. Homing stays valid, and positions read in real mm from then on.
  - **Save to board…** writes the board's config to flash. The board reboots, so redo the encoder offset and homing afterwards. Run `tools.odrive_backup` again to refresh the copy in the repo.
- **Tuning (Administration → Tuning):** pick the axis and read its gains from the board (pos_gain, vel_gain, vel_integrator_gain, encoder_bandwidth). Edit them, then **Apply** (live, not saved) or **Step test…**: the axis moves a few mm and back, and the plot shows position against target with overshoot, settle time, oscillations and remaining error. **Copy from Y** loads the reference axis's gains. **Save to gantry.json…** keeps them; the app writes them to the board on every connect, so the ODrive GUI isn't needed. When homed, the step test goes the way there's room. When not homed, check by eye that there's room in the + direction. It stops itself on overspeed or if the drive disarms.
- **An axis that isn't wired yet:** set `"enabled": false` on it in `gantry.json` (Z is set this way for now). In `--hardware` mode that axis becomes a simulated stand-in, so the app works with only X and Y connected. The Calibration pane marks it *stand-in (not wired)*. When Z is wired, set it back to `true` and check its serial.

## First power-on with hardware

The gantry talks to the ODrives over **USB**, using the `odrive` Python package (0.6.x) and its bundled USB library. Everything in `config/gantry.json` is a placeholder until you check it:

1. **Set up and calibrate each ODrive in the ODrive web GUI or `odrivetool` first:** motor type, pole pairs, encoder, current limits and DC bus. The app keeps that stored setup (`motor.write_motor_config: false`). It only applies what it needs to run: position mode, trajectory limits, a torque clamp and the watchdog. Set `write_motor_config: true` only if you want `gantry.json`'s motor and encoder values written on every connect.
2. **Check the USB connection (read-only, nothing moves):** close the web GUI and `odrivetool`, since only one program can hold the USB link, then run:
   ```bash
   .venv\Scripts\python -m akta_autosampler.tools.odrive_check --any
   ```
   It prints the board's serial, hardware and firmware versions, bus voltage and axis errors. Put the serials into `gantry.json`, then run it without `--any` to confirm every configured board is found.
3. **Supported boards:** this driver targets ODrive Pro and S1 boards (firmware 0.6.x API). A legacy ODrive v3.6 board running 0.5.x firmware has a different API, needs the `odrive==0.5.x` package, and needs driver changes. On Windows, v3.6 boards also need the WinUSB driver installed with Zadig.
4. **Serials and axis numbers:** set `serial` and `axis_number` for each axis.
5. **`turns_per_mm`:** set this from your belt or leadscrew pitch. The default of 0.1 is a guess.
6. **Limits:** start with low `torque_soft_limit` and slow `homing_speed_mm_s`. Keep a hand on a hardware e-stop. The software STOP is not a safety device.
7. **Homing direction:** home one axis at a time (Manual → Home X) and confirm that `homing_direction` drives toward the intended end. Z must home **up**.
8. **Teach the deck:**
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
