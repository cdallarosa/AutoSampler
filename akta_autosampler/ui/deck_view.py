"""
2D top-down "process picture" of the deck and gantry (SVG).

Screen layout: gantry X runs left -> right and gantry Y runs top -> bottom,
so home (0, 0) is the top-left corner and bottle A1 is the top-left bottle.

The SVG is drawn once; a small JS function (``asDeck.update``) moves the
bridge / carriage / target marker and recolours bottles. Short CSS
transitions make the motion glide between updates. Clicking a bottle emits
an ``as_bottle`` event.
"""

import json
from typing import Callable, List, Optional, Tuple

from nicegui import events, ui

from ..deck import Deck
from ..gantry import Gantry

MARGIN = 12.0  # mm of frame drawn around the plate / bottles
RULER = 46.0  # mm of space left of / above the frame for the inch rulers
FONT = "font-family:'Segoe UI', Arial, sans-serif"

# All SVG units are gantry mm. Stroke widths scale with the bottle size through --u (= bottle radius /
# 2.5 in bottle radius, set on the <svg>); font sizes are set per element from the bottle radius.
CSS = """
.as-deck { position: relative; overflow: hidden; isolation: isolate; }
.as-deck svg { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
.as-deck .bottle circle.body { fill: #d9d9d9; stroke: #8c8c8c; stroke-width: calc(1.6 * var(--u, 1));
  transition: fill .15s, stroke .15s; }
.as-deck .bottle.sample circle.body { fill: #fde9c4; stroke: #e0a030; }
.as-deck .bottle text { font-weight: 600; font-family: 'Segoe UI', Arial, sans-serif; fill: #333; pointer-events: none; }
.as-deck .bottle { cursor: pointer; }
.as-deck .bottle:hover circle.body { stroke: #1b7f3b; stroke-width: calc(3.2 * var(--u, 1)); }
.as-deck .bottle.selected circle.body { stroke: #f57c00; stroke-width: calc(6 * var(--u, 1)); fill: #fff3e0; }
.as-deck .bottle.selected.wash circle.body { fill: #cfe6f7; }
.as-deck .bottle.active circle.body { fill: #6cc24a; stroke: #2e7d32; stroke-width: calc(4.5 * var(--u, 1)); }
.as-deck .bottle.active text { fill: #fff; }
.as-deck .bottle.wash circle.body { fill: #cfe6f7; stroke: #2a7ab8; }
.as-deck .bottle.blank circle.body { fill: #f1f1f1; stroke: #9e9e9e; stroke-dasharray: 6 4; }
.as-deck .bottle.empty circle.body { fill: #ffffff; stroke: #c4c4c4; stroke-dasharray: 3 4; }
.as-deck .bottle.empty text { fill: #aaaaaa; }
.as-deck .bottle text.role { font-weight: 700; fill: #2a7ab8; }
.as-deck .qbadge circle { fill: #0b5a8f; stroke: #ffffff; stroke-width: calc(2 * var(--u, 1)); }
.as-deck .qbadge text { font-weight: 700; fill: #ffffff; }
#as-bridge, #as-carriage { transition: transform 110ms linear, opacity .3s; pointer-events: none; }
#as-path, #as-target { pointer-events: none; }
.as-deck.unhomed #as-bridge, .as-deck.unhomed #as-carriage { opacity: .22; }
.as-zgauge.unhomed #as-zneedle, .as-zgauge.unhomed #as-ztip { display: none; }
#as-target { transition: transform 110ms linear; }
.as-zgauge { position: relative; width: 26px; background: linear-gradient(#f4f4f4, #e2e2e2); border: 1px solid #9e9e9e; border-radius: 4px; }
.as-zgauge .mark { position: absolute; left: -4px; right: -4px; height: 0; border-top: 2px dashed #9e9e9e; }
.as-zgauge .mark.sample { border-color: #2e7d32; }
.as-zgauge #as-zneedle { position: absolute; left: 3px; right: 3px; top: 0; height: 0; transition: height 110ms linear; background: #5a5a5a; border-radius: 2px; }
.as-zgauge #as-ztip { position: absolute; left: -5px; right: -5px; height: 8px; margin-top: -4px; border-radius: 4px; background: #d32f2f; transition: top 110ms linear; }
"""

JS = """
<script>
window.asDeck = {
  sel: null, act: null,
  pick(g, args) {
    // Show the bottle popup right beside the clicked bottle, kept inside the Process Picture pane
    const pop = document.querySelector('.as-popup');
    if (pop) {
      this._installClose();
      const r = g.getBoundingClientRect();
      const paneEl = g.closest('.u-pane') || document.body;
      const b = paneEl.getBoundingClientRect();
      pop.style.display = 'block';
      const pw = pop.offsetWidth || 290, ph = pop.offsetHeight || 210;
      let left = r.right + 8;
      if (left + pw > b.right - 6) left = r.left - pw - 8;      // flip to the left of the bottle
      left = Math.max(b.left + 6, Math.min(left, b.right - pw - 6));
      let top = r.top + r.height / 2 - ph / 2;
      top = Math.max(b.top + 30, Math.min(top, b.bottom - ph - 6));
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }
    emitEvent('as_bottle', args);
  },
  _installClose() {
    if (this._closeInstalled) return;
    this._closeInstalled = true;
    document.addEventListener('keydown', e => { if (e.key === 'Escape') asDeck.hidePopup(); });
    document.addEventListener('mousedown', e => {
      const pop = document.querySelector('.as-popup');
      if (!pop || pop.style.display === 'none') return;
      if (pop.contains(e.target) || (e.target.closest && e.target.closest('.as-deck .bottle'))) return;
      asDeck.hidePopup();
    }, true);
  },
  hidePopup() {
    const pop = document.querySelector('.as-popup');
    if (pop) pop.style.display = 'none';
  },
  marks(m) {
    document.querySelectorAll('.as-deck .bottle').forEach(g => {
      const key = g.id.slice(2), info = m[key] || {};
      for (const r of ['wash', 'blank', 'empty']) g.classList.toggle(r, info.role === r);
      const rt = document.getElementById('rt-' + key);
      if (rt) rt.textContent = info.role && info.role !== 'sample' ? info.role.toUpperCase() : '';
      const qb = document.getElementById('qb-' + key), qt = document.getElementById('qt-' + key);
      if (qb) { qb.style.visibility = info.order ? 'visible' : 'hidden'; qt.textContent = info.order || ''; }
      const title = g.querySelector('title');
      if (title) title.textContent = info.title || '';
    });
  },
  update(x, y, zpct, tx, ty, act, sel, homed) {
    const q = id => document.getElementById(id);
    const br = q('as-bridge'), ca = q('as-carriage');
    if (!br || !ca) return;
    const deckEl = br.closest('.as-deck'), gauge = document.querySelector('.as-zgauge');
    if (deckEl) deckEl.classList.toggle('unhomed', !homed);
    if (gauge) gauge.classList.toggle('unhomed', !homed);
    br.style.transform = `translate(0px, ${y}px)`;
    ca.style.transform = `translate(${x}px, ${y}px)`;
    const tg = q('as-target'), ln = q('as-path');
    if (tx === null) { tg.style.visibility = 'hidden'; ln.style.visibility = 'hidden'; }
    else {
      tg.style.visibility = 'visible'; tg.style.transform = `translate(${tx}px, ${ty}px)`;
      ln.style.visibility = 'visible';
      ln.setAttribute('x1', x); ln.setAttribute('y1', y); ln.setAttribute('x2', tx); ln.setAttribute('y2', ty);
    }
    const zn = q('as-zneedle'), zt = q('as-ztip');
    if (zn) { zn.style.height = (zpct * 100) + '%'; zt.style.top = (zpct * 100) + '%'; }
    for (const [key, cls] of [[act, 'act'], [sel, 'sel']]) {
      if (this[cls] !== key) {
        const old = this[cls] && q('b-' + this[cls]);
        if (old) old.classList.remove(cls === 'act' ? 'active' : 'selected');
        const now = key && q('b-' + key);
        if (now) now.classList.add(cls === 'act' ? 'active' : 'selected');
        this[cls] = key;
      }
    }
  }
};
</script>
"""


def bottle_key(slot: str, well: str) -> str:
    return f"{slot}-{well}"


def plate_extent(cfg) -> List[Tuple[float, float]]:
    """
    [(x_lo, x_hi), (y_lo, y_hi)] of the plate in gantry mm: the physical stop-to-stop area
    (simulation.travel_mm, 25 x 20 in), at least the usable travel plus a back-off at each end.
    Coordinate 0 sits one homing back-off inside the home-end stop, so the plate starts at -backoff.
    """
    extent = []
    for n in ("x", "y"):
        ax = cfg.axes[n]
        lo = ax.min_mm - ax.homing_backoff_mm
        span = max(float(cfg.simulation.travel_mm.get(n, 0.0)), ax.max_mm - ax.min_mm + 2 * ax.homing_backoff_mm)
        extent.append((lo, lo + span))
    return extent


class DeckView:

    def __init__(self, gantry: Gantry, deck: Deck, on_select: Optional[Callable[[str, str], None]] = None):
        self.gantry = gantry
        self.deck = deck
        self.on_select = on_select
        cfg = gantry.config
        self.W, self.D = cfg.axes["x"].max_mm, cfg.axes["y"].max_mm
        self.Zmax = cfg.axes["z"].max_mm
        self.plate = plate_extent(cfg)
        self._last = None
        self._sent_key = None

        ui.add_css(CSS)
        ui.add_body_html(JS)
        with ui.row().classes("w-full h-full flex-nowrap gap-3 items-stretch"):
            ui.html(self._svg(), sanitize=False).classes("as-deck grow h-full min-w-0")
            self._z_gauge()
        ui.on("as_bottle", self._clicked)

    # ------------------------------------------------------------------

    def _bottles(self):
        """(slot, well, key, x, y, radius) of every bottle on the deck, in gantry mm."""
        for slot in self.deck.slots.values():
            r = (slot.labware.well_diameter_mm or 50.0) / 2
            for well in slot.labware.wells():
                bx, by = self.deck.well_xy(slot.name, well)
                yield slot, well, bottle_key(slot.name, well), bx, by, r

    def _svg(self) -> str:
        """
        Everything is drawn in gantry mm (1 SVG unit = 1 mm, X right, Y down, home = 0, 0), so bottles,
        rulers and the needle marker share one scale. The frame wraps the plate and every bottle: with
        A1 at home, the A-row / column-1 bottles reach past the home-end edge of the needle travel.
        """
        inch = 25.4
        bottles = list(self._bottles())
        rb = max((b[5] for b in bottles), default=31.75)  # reference bottle radius for marker sizes
        u = rb / (1.25 * inch)  # 1.0 for 2.5 in bottles
        (px0, px1), (py0, py1) = self.plate
        x0 = min([px0] + [b[3] - b[5] for b in bottles]) - MARGIN
        x1 = max([px1] + [b[3] + b[5] for b in bottles]) + MARGIN
        y0 = min([py0] + [b[4] - b[5] for b in bottles]) - MARGIN
        y1 = max([py1] + [b[4] + b[5] for b in bottles]) + MARGIN
        vx, vy = x0 - RULER, y0 - RULER + 10
        vw, vh = x1 - vx + 10, y1 + 34 - vy
        parts = [f'<svg viewBox="{vx:.1f} {vy:.1f} {vw:.1f} {vh:.1f}" style="--u:{u:.3f}" '
                 f'preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">']
        # frame + plate (the physical stop-to-stop area)
        parts.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{x1 - x0:.1f}" height="{y1 - y0:.1f}" rx="10" '
                     f'fill="#f7f7f7" stroke="#b5b5b5" stroke-width="2.5"/>')
        parts.append(f'<rect x="{px0:.1f}" y="{py0:.1f}" width="{px1 - px0:.1f}" height="{py1 - py0:.1f}" '
                     f'fill="#ffffff" stroke="#c9c9c9" stroke-width="1.6"/>')
        # 1 in grid over the plate, heavier every 4 in; lines at whole inches of gantry coordinates
        for i in range(int(px0 // inch) + 1, int(px1 / inch) + 1):
            if px0 < i * inch < px1:
                w = 1.4 if i % 4 == 0 else 0.6
                parts.append(f'<line x1="{i * inch:.1f}" y1="{py0:.1f}" x2="{i * inch:.1f}" y2="{py1:.1f}" '
                             f'stroke="#e6e6e6" stroke-width="{w}"/>')
        for i in range(int(py0 // inch) + 1, int(py1 / inch) + 1):
            if py0 < i * inch < py1:
                w = 1.4 if i % 4 == 0 else 0.6
                parts.append(f'<line x1="{px0:.1f}" y1="{i * inch:.1f}" x2="{px1:.1f}" y2="{i * inch:.1f}" '
                             f'stroke="#e6e6e6" stroke-width="{w}"/>')
        # inch rulers (gantry coordinates, 0 = home), with ticks on the frame edge
        ruler = f'style="font-size:16px;{FONT};fill:#8a8a8a"'
        for i in range(0, int((px1 + 3) / inch) + 1, 2):
            x = i * inch
            parts.append(f'<line x1="{x:.1f}" y1="{y0:.1f}" x2="{x:.1f}" y2="{y0 - (8 if i % 4 == 0 else 4):.1f}" '
                         f'stroke="#9e9e9e" stroke-width="1.4"/>')
            if i % 4 == 0:
                parts.append(f'<text x="{x:.1f}" y="{y0 - 12:.1f}" text-anchor="middle" {ruler}>{i}"</text>')
        for i in range(0, int((py1 + 3) / inch) + 1, 2):
            y = i * inch
            parts.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x0 - (8 if i % 4 == 0 else 4):.1f}" y2="{y:.1f}" '
                         f'stroke="#9e9e9e" stroke-width="1.4"/>')
            if i % 4 == 0:
                parts.append(f'<text x="{x0 - 11:.1f}" y="{y + 5.5:.1f}" text-anchor="end" {ruler}>{i}"</text>')
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{y1 + 24:.1f}" text-anchor="middle" '
                     f'style="font-size:16px;font-weight:600;{FONT};fill:#8a8a8a">'
                     f'X →     ·     Y ↓     ·     home = top-left</text>')

        # bottles: true diameter at their needle-centre coordinates; labels sized from the radius
        for slot, well, key, bx, by, r in bottles:
            label = "SAMPLE" if slot.name == "sample" else well
            fs = r * (0.6 if len(label) <= 3 else 0.36)
            cls = "bottle sample" if slot.name == "sample" else "bottle"
            args = json.dumps({"slot": slot.name, "well": well}).replace('"', "&quot;")
            br = r * 0.4  # run-order badge, top-right of the bottle
            bdx, bdy = bx + r * 0.72, by - r * 0.72
            parts.append(
                f'<g id="b-{key}" class="{cls}" onclick="asDeck.pick(this, {args})"><title></title>'
                f'<circle class="body" cx="{bx:.2f}" cy="{by:.2f}" r="{r:.2f}"/>'
                f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="{r * 0.62:.2f}" fill="none" stroke="#b0b0b0" '
                f'stroke-width="{r * 0.03:.2f}"/>'
                f'<text x="{bx:.2f}" y="{by + fs * 0.36:.2f}" text-anchor="middle" font-size="{fs:.1f}">{label}</text>'
                f'<text id="rt-{key}" class="role" x="{bx:.2f}" y="{by + r * 0.66:.2f}" text-anchor="middle" '
                f'font-size="{r * 0.34:.1f}"></text>'
                f'<g id="qb-{key}" class="qbadge" style="visibility:hidden">'
                f'<circle cx="{bdx:.2f}" cy="{bdy:.2f}" r="{br:.2f}"/>'
                f'<text id="qt-{key}" x="{bdx:.2f}" y="{bdy + br * 0.38:.2f}" text-anchor="middle" '
                f'font-size="{br * 1.05:.1f}"></text></g></g>')

        # home marker
        h = rb * 0.45
        parts.append(f'<path d="M {-h:.1f} 0 L {h:.1f} 0 M 0 {-h:.1f} L 0 {h:.1f}" stroke="#9e9e9e" '
                     f'stroke-width="{2 * u:.1f}"/>')

        # target + path
        parts.append(f'<line id="as-path" x1="0" y1="0" x2="0" y2="0" stroke="#2e7d32" stroke-width="{2.5 * u:.1f}" '
                     f'stroke-dasharray="{9 * u:.0f} {6 * u:.0f}" style="visibility:hidden"/>')
        parts.append(f'<g id="as-target" style="visibility:hidden">'
                     f'<circle r="{rb * 0.66:.1f}" fill="none" stroke="#2e7d32" stroke-width="{2.5 * u:.1f}" '
                     f'stroke-dasharray="{5 * u:.0f} {4 * u:.0f}"/>'
                     f'<circle r="{2.5 * u:.1f}" fill="#2e7d32"/></g>')

        # gantry: bridge spans X and moves in Y; carriage moves in X and Y. Both are smaller than a
        # bottle so the bottle ring (selected / needle-in colour) stays visible around the needle.
        bh = rb * 0.7
        parts.append(f'<g id="as-bridge"><rect x="{x0 + 3:.1f}" y="{-bh / 2:.1f}" width="{x1 - x0 - 6:.1f}" '
                     f'height="{bh:.1f}" rx="{4 * u:.1f}" fill="#9a9a9a" fill-opacity="0.3" stroke="#6e6e6e" '
                     f'stroke-width="{1.4 * u:.1f}"/></g>')
        cw, ch = rb * 1.25, rb * 0.95
        parts.append(f'<g id="as-carriage">'
                     f'<rect x="{-cw / 2:.1f}" y="{-ch / 2:.1f}" width="{cw:.1f}" height="{ch:.1f}" rx="{5 * u:.1f}" '
                     f'fill="#505050" fill-opacity="0.85" stroke="#2b2b2b" stroke-width="{1.4 * u:.1f}"/>'
                     f'<circle r="{rb * 0.3:.1f}" fill="#ffffff" stroke="#d32f2f" stroke-width="{2.5 * u:.1f}"/>'
                     f'<circle r="{2.5 * u:.1f}" fill="#d32f2f"/></g>')
        parts.append("</svg>")
        return "".join(parts)

    def _z_gauge(self):
        lw = next(iter(self.deck.slots.values())).labware
        top_pct = 100 * lw.z_top_mm / self.Zmax
        smp_pct = 100 * lw.z_sample_mm / self.Zmax
        with ui.column().classes("items-center gap-1 h-full py-2"):
            ui.label("Z").classes("text-xs font-bold text-gray-600")
            ui.html(f'<div class="as-zgauge" style="height:100%">'
                    f'<div id="as-zneedle"></div><div id="as-ztip"></div>'
                    f'<div class="mark" style="top:{top_pct:.1f}%" title="bottle top"></div>'
                    f'<div class="mark sample" style="top:{smp_pct:.1f}%" title="sampling depth"></div></div>',
                    sanitize=False).classes("grow")
            ui.label("down").classes("text-[10px] text-gray-500")

    # ------------------------------------------------------------------

    def update(self, x: float, y: float, z: float, target: Optional[Tuple[float, float]],
               selected: Optional[Tuple[str, str]], homed: bool = True):
        active = self._active(x, y, z)
        sel = bottle_key(*selected) if selected else None
        key = (round(x, 1), round(y, 1), round(z, 1), target and (round(target[0]), round(target[1])), active, sel,
               homed)
        if key == self._sent_key:
            return
        self._sent_key = key
        zpct = max(0.0, min(1.0, z / self.Zmax)) if self.Zmax else 0.0
        tx, ty = (f"{target[0]:.2f}", f"{target[1]:.2f}") if target else ("null", "null")
        ui.run_javascript(
            f"window.asDeck && asDeck.update({x:.2f}, {y:.2f}, {zpct:.4f}, {tx}, {ty}, "
            f"{json.dumps(active)}, {json.dumps(sel)}, {'true' if homed else 'false'})")

    def set_marks(self, marks: dict):
        """marks: {bottle_key: {"role", "order": "1,3", "title"}} - role colours and run-order badges."""
        self._marks = marks
        ui.run_javascript(f"window.asDeck && asDeck.marks({json.dumps(marks)})")

    def reapply(self):
        """Re-send marks, position and selection - the SVG is re-rendered when its tab is shown again."""
        self._sent_key = None
        if getattr(self, "_marks", None) is not None:
            ui.run_javascript(f"window.asDeck && (asDeck.sel = asDeck.act = null, asDeck.marks({json.dumps(self._marks)}))")

    def _active(self, x: float, y: float, z: float) -> Optional[str]:
        """The bottle the needle is in: over it and below its top (only once homed)."""
        if not self.gantry.is_homed:
            return None
        for slot in self.deck.slots.values():
            lw = slot.labware
            r = (lw.well_diameter_mm or 50.0) / 2
            if z < lw.z_top_mm + slot.z_offset_mm - 1:
                continue
            for well in lw.wells():
                bx, by = self.deck.well_xy(slot.name, well)
                if (x - bx) ** 2 + (y - by) ** 2 <= r * r:
                    return bottle_key(slot.name, well)
        return None

    def _clicked(self, e: events.GenericEventArguments):
        args = e.args[0] if isinstance(e.args, list) and e.args else e.args
        if self.on_select and isinstance(args, dict):
            self.on_select(args["slot"], args["well"])
