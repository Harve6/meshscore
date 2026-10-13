"""meshscore.toml: loading, validation and a starter file.

The file has a few small sections; everything is optional except at least one ``[bots.NAME]``::

    [radio]                      # how the companion radio is connected
    transport = "serial"         # serial (USB) | ble | tcp
    port = "COM3"

    [bot]                        # behaviour (all optional)
    timezone = "America/Toronto"

    [bots.leafs]                 # one table per bot; NAME is also its command prefix: "!leafs score"
    type = "nhl"
    team = "TOR"
    channel = "#leafs"
"""
from __future__ import annotations

import json
import os
import re
import sys

from .core import CFG, DEFAULTS

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore

CONFIG_NAME = "meshscore.toml"

RADIO_KEYS = {"transport", "port", "baud", "host", "tcp_port", "ble_address", "ble_name", "ble_pin"}
BOT_KEYS = {"timezone", "default_mode", "announce_on_start", "dm_default", "respond_to_dms", "log_chat", "admin_names", "admin_keys", "poll_live_seconds",
            "poll_idle_seconds", "busy_live_games", "command_cooldown", "send_gap", "max_msg_bytes", "max_reply_msgs",
            "max_follows", "dm_even_if_broadcast", "instance_port", "log_file", "full_days_back", "full_days_ahead",
            "player_aliases", "phases"}
HOCKEYTECH_KEYS = {"api_key", "client_code", "digest_hour", "early_final_seconds", "summary_fetch"}
NEEDS_TEAM = {"nhl", "mlb", "nfl"}


class ConfigError(Exception):
    """A problem in meshscore.toml, worded for the person editing it."""


def user_config_path():
    base = (os.environ.get("APPDATA") if sys.platform == "win32" else
            os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base or os.path.expanduser("~"), "meshscore", CONFIG_NAME)


def find_config(explicit=None):
    """First existing file of: the explicit path, $MESHSCORE_CONFIG, ./meshscore.toml, the user config directory."""
    cands = [explicit, os.environ.get("MESHSCORE_CONFIG"), os.path.join(os.getcwd(), CONFIG_NAME), user_config_path()]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def parse(text):
    """TOML text -> the flat settings dict the bot uses (CFG layout). Raises ConfigError."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Not valid TOML: {e}") from None
    out = {}
    unknown = [k for k in raw if k not in ("radio", "bot", "hockeytech", "same_air", "airtime", "bots", "plugins")]
    if unknown:
        raise ConfigError(f"Unknown section(s): {', '.join(unknown)}. Known: radio, bot, hockeytech, same_air, airtime, bots, plugins")
    for section, keys in (("radio", RADIO_KEYS), ("bot", BOT_KEYS), ("hockeytech", HOCKEYTECH_KEYS)):
        for k, v in (raw.get(section) or {}).items():
            if k not in keys:
                raise ConfigError(f"[{section}] has no setting called {k!r}. Known: {', '.join(sorted(keys))}")
            out[k] = v
    for section in ("same_air", "airtime"):
        sect = raw.get(section) or {}
        bad = [k for k in sect if k not in DEFAULTS[section]]
        if bad:
            raise ConfigError(f"[{section}] has no setting called {bad[0]!r}. Known: {', '.join(DEFAULTS[section])}")
        out[section] = {**DEFAULTS[section], **sect}
    bots = raw.get("bots") or {}
    if not isinstance(bots, dict):
        raise ConfigError("[bots] must contain one table per bot, e.g. [bots.leafs]")
    out["modules"] = {}
    for name, b in bots.items():
        if not isinstance(b, dict):
            raise ConfigError(f"[bots.{name}] must be a table")
        out["modules"][str(name).lower()] = {"enabled": True, **b}
    out["plugin_dirs"] = list((raw.get("plugins") or {}).get("dirs") or [])
    return out


def problems(cfg=None):
    """Human-readable list of things wrong with a settings dict (empty = fine)."""
    from .leagues import registry
    cfg = cfg if cfg is not None else CFG
    errs = []
    t = cfg.get("transport")
    if t not in ("serial", "ble", "tcp"):
        errs.append(f'[radio] transport must be "serial", "ble" or "tcp" (got {t!r})')
    elif t == "serial" and not cfg.get("port"):
        errs.append('[radio] transport = "serial" needs port = "COM3" (or /dev/ttyUSB0). Run: meshscore ports')
    elif t == "tcp" and not cfg.get("host"):
        errs.append('[radio] transport = "tcp" needs host = "192.168.x.x"')
    elif t == "ble" and not cfg.get("ble_address"):
        errs.append('[radio] transport = "ble" needs ble_address. Run: meshscore scan-ble')
    mods = cfg.get("modules") or {}
    if not any(m.get("enabled", True) for m in mods.values()):
        errs.append("no bots configured - add a [bots.NAME] table (run: meshscore init)")
    types = registry()
    for name, m in mods.items():
        if not m.get("enabled", True):
            continue
        typ = str(m.get("type", name)).lower()
        if typ not in types:
            errs.append(f"[bots.{name}] unknown type {typ!r}. Known: {', '.join(sorted(types))}")
            continue
        if not str(m.get("channel", "")).strip().lstrip("#"):
            errs.append(f'[bots.{name}] needs channel = "#something" (a channel on your radio)')
        if typ in NEEDS_TEAM and not m.get("team"):
            errs.append(f'[bots.{name}] type {typ!r} needs team = "..." (e.g. "TOR")')
    if int(cfg.get("max_msg_bytes", 130)) > 160:
        errs.append("max_msg_bytes above 160 will not fit a MeshCore channel message")
    return errs


def reset():
    """Back to defaults (before loading a file, and for the tests)."""
    CFG.clear()
    CFG.update({k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()})
    CFG["modules"] = {}
    CFG["plugin_dirs"] = []


def load_config(path=None, strict=False):
    """Read `path` (or the first config found) into core.CFG. Returns the path used, or None when no file exists.
    Raises ConfigError for an unreadable / invalid file; with strict=True also when problems() finds any."""
    path = find_config(path)
    reset()
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        user = parse(f.read())
    for k, v in user.items():
        CFG[k] = {**CFG[k], **v} if k in ("same_air", "airtime") else v
    if strict:
        bad = problems()
        if bad:
            raise ConfigError("\n".join("  - " + b for b in bad))
    return path


# ---------------------------------------------------------------- writing a starter file
def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    # a TOML basic string uses the same escapes as JSON; DEL is the one extra control character TOML forbids
    return json.dumps(str(v), ensure_ascii=False).replace(chr(127), chr(92) + "u007f")


def render(radio: dict, bots: dict, timezone: str = "") -> str:
    """A commented meshscore.toml for the given radio settings and {name: {type, team, channel, ...}} bots."""
    lines = ["# MeshScore configuration - every setting is documented in docs/configuration.md", "", "[radio]"]
    for k in ("transport", "port", "host", "tcp_port", "ble_address", "ble_name", "ble_pin"):
        if radio.get(k) not in (None, ""):
            lines.append(f"{k} = {_toml_value(radio[k])}")
    lines += ["", "[bot]"]
    lines.append(f"timezone = {_toml_value(timezone)}" if timezone else
                 '# timezone = "America/Toronto"   # times shown in this zone (default: this computer\'s zone)')
    lines += ['# default_mode = "max"            # max = every goal/run/score | min = period results + final | off',
              "# announce_on_start = false       # post an 'active' message when the bot starts",
              "# admin_names = []                # node names allowed to use !mode / !phase (empty = anyone; names can be spoofed)",
              "# admin_keys = []                 # public-key prefixes allowed to do so by DM (authenticated)",
              "", "[same_air]", "# several MeshScore bots on one mesh: skip a post another bot already made",
              "# enabled = true", "# rank = -1        # place in the line-up of bots: 0 posts first, 1 waits a slot... (-1 = by node name)",
              "# peers = []        # node names of your other bots, if the line-up should be active before they are heard", "",
              "[airtime]", "# max_per_hour = 0   # cap on alert messages per hour (0 = no cap); finals always go out"]
    for name, b in bots.items():
        lines += ["", f"[bots.{name}]"]
        for k, v in b.items():
            lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- editing an existing file (keeps your comments)
_BOT_HEADER = re.compile(r"^\s*\[bots\.([A-Za-z0-9_\-]+)\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")


def bot_blocks(text):
    """{name: (first_line, last_line_exclusive)} of every [bots.NAME] table in the text (commented-out ones don't count)."""
    lines = text.split("\n")
    heads = [(i, m.group(1).lower()) for i, ln in enumerate(lines) if (m := _BOT_HEADER.match(ln))]
    out = {}
    for i, name in heads:
        j = next((k for k in range(i + 1, len(lines)) if _ANY_HEADER.match(lines[k])), len(lines))
        while j - 1 > i and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#")):
            j -= 1                    # comments / blank lines after the last setting belong to what follows, not to this bot
        out[name] = (i, j)
    return out


def add_bot_text(text, name, fields):
    """The file text with a new [bots.NAME] table appended. Raises ConfigError if the name is already used."""
    if name.lower() in bot_blocks(text):
        raise ConfigError(f"There is already a bot called {name!r}.")
    block = [f"[bots.{name}]"] + [f"{k} = {_toml_value(v)}" for k, v in fields.items()]
    return text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"


def remove_bot_text(text, name):
    """The text without the [bots.NAME] table (its comments inside the table go with it)."""
    blocks = bot_blocks(text)
    if name.lower() not in blocks:
        raise ConfigError(f"No bot called {name!r}. Bots: {', '.join(blocks) or '(none)'}")
    a, b = blocks[name.lower()]
    lines = text.split("\n")
    kept = lines[:a] + lines[b:]
    out = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", out).rstrip("\n") + "\n"


def set_enabled_text(text, name, enabled):
    """Turn a bot on/off by adding or updating `enabled = ...` inside its table."""
    blocks = bot_blocks(text)
    if name.lower() not in blocks:
        raise ConfigError(f"No bot called {name!r}. Bots: {', '.join(blocks) or '(none)'}")
    a, b = blocks[name.lower()]
    lines = text.split("\n")
    val = "true" if enabled else "false"
    for k in range(a + 1, b):
        if re.match(r"^\s*enabled\s*=", lines[k]):
            lines[k] = f"enabled = {val}"
            break
    else:
        lines.insert(a + 1, f"enabled = {val}")
    return "\n".join(lines)


def set_field_text(text, name, key, value):
    """Set `key = value` inside an existing [bots.NAME] table (replacing the line if it is there, adding it if not)."""
    blocks = bot_blocks(text)
    if name.lower() not in blocks:
        raise ConfigError(f"No bot called {name!r}. Bots: {', '.join(blocks) or '(none)'}")
    a, b = blocks[name.lower()]
    lines = text.split("\n")
    new = f"{key} = {_toml_value(value)}"
    for k in range(a + 1, b):
        if re.match(rf"^\s*{re.escape(key)}\s*=", lines[k]):
            lines[k] = new
            break
    else:
        lines.insert(b, new)
    return "\n".join(lines)


def save_edited(path, new_text):
    """Validate `new_text`, keep the previous file as <path>.bak, then write it. Raises ConfigError (file untouched)
    if the result would not load or has problems."""
    user = parse(new_text)                                   # raises ConfigError for broken TOML / unknown settings
    merged = {**DEFAULTS, **{k: v for k, v in user.items()}}
    bad = problems(merged)
    if bad:
        raise ConfigError("\n".join("  - " + b for b in bad))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f, open(path + ".bak", "w", encoding="utf-8", newline="\n") as g:
            g.write(f.read())
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_text)
