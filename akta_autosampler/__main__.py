"""
Run the autosampler UI.

    python -m akta_autosampler                   # the real gantry (ODrives over USB)
    python -m akta_autosampler --akta hardware   # ÄKTA link mode (sim | hardware | off), default from config/akta.json

The gantry simulator is only for the automated tests and UI development (hidden --sim flag).
"""

import argparse
import logging
from pathlib import Path

from .akta import AktaLink, load_akta_config
from .config import CONFIG_DIR, load_gantry_config
from .deck import Deck
from .gantry import Gantry


def main():
    parser = argparse.ArgumentParser(prog="akta_autosampler", description="ÄKTA autosampler gantry UI")
    # Developer only: simulated axes for UI work without hardware. --hardware is the default (kept for old run configs).
    parser.add_argument("--sim", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hardware", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--akta", choices=["sim", "hardware", "off"],
                        help="ÄKTA link mode (default: 'mode' in config/akta.json)")
    parser.add_argument("--config", type=Path, default=CONFIG_DIR, help="config directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--native", action="store_true", help="open in a desktop window (needs pywebview)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--debug", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    config = load_gantry_config(args.config / "gantry.json")
    config.simulate = args.sim  # always the real gantry unless the hidden developer flag is given
    deck = Deck.load(args.config)
    gantry = Gantry(config)

    akta = None
    akta_path = args.config / "akta.json"
    if akta_path.exists():
        akta_config = load_akta_config(akta_path)
        mode = args.akta or akta_config.get("mode", "off")
        if mode != "off":
            akta = AktaLink(akta_config, simulate=(mode == "sim"))

    from .ui.app import main as run_ui
    run_ui(gantry, deck, akta, host=args.host, port=args.port, native=args.native,
           show=not args.no_browser, auto_connect=config.simulate)


if __name__ in {"__main__", "__mp_main__"}:
    main()
