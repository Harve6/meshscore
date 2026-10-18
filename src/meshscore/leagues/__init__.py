"""League plugins.

A *league plugin* is a class that the hub drives: it gets a ``(hub, name)`` constructor, polls its data source in
``run()``, posts alerts with ``hub.say_chan`` and answers ``!commands``. See docs/writing-a-plugin.md and
examples/plugins/ for a complete minimal one.

Built-in types are registered here. Third-party plugins are found through

* Python entry points in the group ``meshscore.leagues`` (``pip install meshscore-yourleague``), and
* ``[plugins] dirs = ["./my_plugins"]`` in meshscore.toml: every ``*.py`` there that defines
  ``LEAGUES = {"type-name": ClassName}`` is loaded.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from importlib import metadata

log = logging.getLogger("meshscore")

_extra: dict = {}
_loaded = False


def builtin():
    from .hockeytech import HockeyTechModule
    from .mlb import MlbModule
    from .mlb_postseason import PostseasonModule
    from .nfl import NflModule
    from .nhl import NhlModule
    types = {
        "nhl": NhlModule,
        "mlb": MlbModule,
        "nfl": NflModule,
        "hockeytech": HockeyTechModule,
        "ohl": HockeyTechModule,                 # the OHL is the default HockeyTech league
        "mlb-postseason": PostseasonModule,
        "postseason": PostseasonModule,
    }
    try:                                         # not sports (indices, stocks, crypto, headlines); never takes the sports bots down
        from .market import MarketModule
        types["market"] = MarketModule
    except Exception:
        log.exception("The market bot type could not be loaded; sports bots are unaffected")
    try:                                         # crowd-vs-bot chess; needs `pip install chess` (the [chess] extra)
        from .chessbot import ChessModule
        types["chess"] = ChessModule
    except ImportError:
        log.debug("The chess bot type needs the python-chess package (pip install chess)")
    except Exception:
        log.exception("The chess bot type could not be loaded; the other bots are unaffected")
    try:                                         # crowd-plays Pokemon Red; needs `pip install pyboy pillow` + your own ROM
        from .pokemon import PokemonModule
        types["pokemon"] = PokemonModule
    except Exception:
        log.exception("The pokemon bot type could not be loaded; the other bots are unaffected")
    for typ, mod, cls in (("word", "wordbot", "WordModule"), ("trivia", "triviabot", "TriviaModule"),
                          ("pickem", "pickembot", "PickemModule"), ("coverage", "coveragebot", "CoverageModule"),
                          ("weather", "weatherbot", "WeatherModule")):
        try:                                     # the daily bots (docs/daily-games.md); never take the others down
            types[typ] = getattr(importlib.import_module(f"{__name__}.{mod}"), cls)
        except ImportError:
            log.debug("The %s bot type is not available", typ)
        except Exception:
            log.exception("The %s bot type could not be loaded; the other bots are unaffected", typ)
    return types


def register(type_name, cls):
    """Add (or replace) a league type at runtime."""
    _extra[str(type_name).lower()] = cls


def load_plugin_dir(path):
    """Import every *.py in `path`; each may define LEAGUES = {type: class}. Returns the number of types added."""
    added = 0
    for fn in sorted(os.listdir(path)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        full = os.path.join(path, fn)
        try:
            spec = importlib.util.spec_from_file_location(f"meshscore_plugin_{fn[:-3]}", full)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            for t, cls in (getattr(mod, "LEAGUES", None) or {}).items():
                register(t, cls)
                added += 1
        except Exception:
            log.exception("Could not load plugin %s", full)
    return added


def load_plugins(dirs=()):
    """Discover entry-point plugins and plugin directories (idempotent for entry points)."""
    global _loaded
    if not _loaded:
        _loaded = True
        try:
            for ep in metadata.entry_points(group="meshscore.leagues"):
                try:
                    register(ep.name, ep.load())
                except Exception:
                    log.exception("Could not load plugin entry point %s", ep.name)
        except Exception:                                       # pragma: no cover - very old importlib.metadata
            pass
    for d in dirs or ():
        if os.path.isdir(d):
            load_plugin_dir(d)
        else:
            log.warning("[plugins] directory not found: %s", d)


def registry():
    """{type name: class} - built-ins plus whatever plugins registered."""
    return {**builtin(), **_extra}
