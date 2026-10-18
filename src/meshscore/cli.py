"""The `meshscore` command: run, init (setup wizard), check, ports, scan-ble, teams, dump, version."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
import time

from . import __version__, config
from .config import ConfigError
from .core import CFG, init_tz, log, now, setup_logging

_instance_lock = None


# ---------------------------------------------------------------- one copy per computer
def acquire_single_instance():
    """Claim a local port for the life of the process; False if another copy of MeshScore already holds it.
    The OS releases it when the process ends or crashes, so it can never be left stale."""
    global _instance_lock
    port = int(CFG["instance_port"])
    if not port:
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        return False
    _instance_lock = sock
    return True


# ---------------------------------------------------------------- radio discovery
def scan_ble_devices(timeout=8):
    """[(name, address, rssi)] for everything Bluetooth can see."""
    from bleak import BleakScanner
    found = asyncio.run(BleakScanner.discover(timeout=timeout, return_adv=True))
    return [((dev.name or adv.local_name or "").strip(), addr, adv.rssi) for addr, (dev, adv) in found.items()]


def is_mesh_name(name):
    n = name.lower()
    return any(w in n for w in ("mesh", "heltec", "t096", "rak", "t-echo", "techo", "t1000", "wio", "seeed", "xiao", "lilygo", "station"))


def list_ports():
    from serial.tools import list_ports as lp
    return sorted(lp.comports(), key=lambda p: (len(p.device), p.device))


def describe_radio(d):
    t = d.get("transport")
    if t == "serial":
        return f"USB {d.get('port')}"
    if t == "tcp":
        return f"WiFi {d.get('host')}:{d.get('tcp_port', 5000)}"
    if t == "ble":
        return f"Bluetooth {d.get('ble_name') or d.get('ble_address')}"
    return "?"


def pick_serial(ask=input, say=print):
    """-> radio dict, None (back) or False (quit)."""
    while True:
        ports = list_ports()
        say(f"\nUSB / serial ports ({len(ports)} found):")
        for i, pt in enumerate(ports, 1):
            say(f"  {i:>2}) {pt.device:<10} {pt.description}")
        if not ports:
            say("  (none - is the radio plugged in and running the Companion USB firmware?)")
        ans = ask("\nNumber (or a port name like COM5 or /dev/ttyUSB0), [r]escan, [b]ack, [q]uit: ").strip()
        low = ans.lower()
        if low in ("q", "quit"):
            return False
        if low in ("b", "back"):
            return None
        if low in ("", "r", "rescan"):
            continue
        if low.isdigit() and 1 <= int(low) <= len(ports):
            return {"transport": "serial", "port": ports[int(low) - 1].device}
        if low.startswith("com") or ans.startswith("/dev/") or ans.startswith("tty"):
            return {"transport": "serial", "port": ans if ans.startswith("/") else ans.upper() if low.startswith("com") else ans}


def pick_wifi(ask=input, say=print):
    while True:
        ip = ask("\nRadio IP address ([b]ack, [q]uit): ").strip()
        if ip.lower() in ("q", "quit"):
            return False
        if ip.lower() in ("b", "back"):
            return None
        if not ip:
            continue
        port = ask("Port [5000]: ").strip()
        port = int(port) if port.isdigit() else 5000
        try:
            socket.create_connection((ip, port), timeout=4).close()
            say("Radio reachable.")
        except OSError as e:
            say(f"Can't reach {ip}:{port} ({e}).")
            if ask("Use it anyway? [y/N] ").strip().lower() != "y":
                continue
        return {"transport": "tcp", "host": ip, "tcp_port": port}


def pick_ble(ask=input, say=print):
    say("\nMake sure the phone app is disconnected from the radio (one Bluetooth client at a time).")
    while True:
        ans = ask("Press Enter to scan for 8 seconds, [b]ack, [q]uit: ").strip().lower()
        if ans in ("b", "back"):
            return None
        if ans in ("q", "quit"):
            return False
        say("Scanning...")
        try:
            rows = scan_ble_devices()
        except Exception as e:
            say(f"Scan failed: {e}\n(Is Bluetooth turned on for this computer?)")
            continue
        show_all = False
        while True:
            shown = sorted((r for r in rows if r[0] and (show_all or is_mesh_name(r[0]))), key=lambda r: -(r[2] or -999))
            say("\n" + ("All Bluetooth devices" if show_all else "MeshCore radios") + f" ({len(shown)} found):")
            for i, (name, addr, rssi) in enumerate(shown, 1):
                say(f"  {i:>2}) {name:<28} {addr}   signal {rssi} dBm")
            if not shown:
                say("  (none - radio on? phone app closed? try [a]ll)")
            ans = ask("\nNumber, [r]escan, [a]ll devices, [m]esh only, [b]ack, [q]uit: ").strip().lower()
            if ans in ("q", "quit"):
                return False
            if ans in ("b", "back"):
                return None
            if ans in ("r", "rescan"):
                break
            if ans in ("a", "all"):
                show_all = True
            elif ans in ("m", "mesh"):
                show_all = False
            elif ans.isdigit() and 1 <= int(ans) <= len(shown):
                name, addr, _ = shown[int(ans) - 1]
                pin = ask("Pairing PIN [123456]: ").strip() or "123456"
                return {"transport": "ble", "ble_address": addr, "ble_name": name, "ble_pin": pin}


def pick_radio(ask=input, say=print):
    """Ask how the radio is connected. Returns a radio settings dict, or None if the user quits."""
    while True:
        say("\nHow is the radio connected?\n  1) USB cable\n  2) Bluetooth\n  3) WiFi (IP address)\n  q) Quit")
        ans = ask("> ").strip().lower()
        if ans in ("q", "quit"):
            return None
        picker = {"1": pick_serial, "usb": pick_serial, "2": pick_ble, "bluetooth": pick_ble, "ble": pick_ble,
                  "3": pick_wifi, "wifi": pick_wifi}.get(ans)
        if not picker:
            continue
        res = picker(ask, say)
        if res is False:
            return None
        if res:
            return res


# ---------------------------------------------------------------- setup wizard
NO_TEAM = ("mlb-postseason", "market", "chess", "pokemon", "word", "trivia", "pickem", "coverage", "weather")      # bot types that are not tied to one team
LEAGUE_CHOICES = [
    ("nhl", "NHL hockey - one team"),
    ("mlb", "MLB baseball - one team"),
    ("nfl", "NFL football - one team"),
    ("ohl", "OHL / CHL hockey (HockeyTech) - one team or the whole league"),
    ("mlb-postseason", "MLB postseason - every team, every round"),
    ("market", "Markets - indices, stocks, commodities, crypto, news (no team)"),
    ("chess", "Chess - the whole channel plays one game against a bot that gets stronger (needs: pip install chess)"),
    ("pokemon", "Pokemon Red - the whole channel plays one game (needs: pip install pyboy pillow, and your own ROM)"),
    ("word", "Mesh Word - a daily 5-letter word game, answered by DM"),
    ("trivia", "Mesh Trivia - one question a day, answered by DM"),
    ("pickem", "Pick'em - DM your picks for the day's games, a leaderboard keeps score"),
    ("coverage", "Coverage - which nodes reach the bot, how far (no channel posts)"),
    ("weather", "Weather - !weather and Environment Canada warnings for a place"),
]


def _pick_team(kind, ask, say):
    say("  Loading teams ...")
    teams = []
    try:
        from .teams import team_list
        teams = team_list(kind)
    except Exception as e:
        say(f"  (could not load the team list: {e})")
    codes = {c.upper(): n for c, n in teams}
    if teams:
        say("  " + "  ".join(f"{c}" for c, _ in teams))
    while True:
        t = ask("  Team code" + (" (blank = whole league)" if kind in ("ohl", "hockeytech") else "") + ": ").strip().upper()
        if not t and kind in ("ohl", "hockeytech"):
            return "", ""
        if t and (not codes or t in codes):
            return t, codes.get(t, "")
        say(f"  {t!r} isn't one of the codes above.")


def postseason_companion(team_bot, taken):
    """(name, fields) for the MLB postseason bot that rides along on an MLB team bot's channel: every other playoff
    game goes there too, and the team's own games are skipped (the team bot already posts those)."""
    name = "mlb"
    while name in taken:
        name += "2"
    return name, {"type": "mlb-postseason", "channel": team_bot["channel"], "skip_teams": [str(team_bot["team"]).upper()]}


def _pick_channel(channels, ask, say, default):
    if channels:
        say("  Channels on your radio: " + "  ".join("#" + c.lstrip("#") for c in channels))
    while True:
        c = ask(f"  Channel for this bot [#{default}]: ").strip() or f"#{default}"
        c = "#" + c.lstrip("#").lower()
        if not channels or c.lstrip("#") in {x.lstrip("#").lower() for x in channels}:
            return c
        if ask(f"  {c} isn't on the radio yet - add it in the MeshCore app. Use it anyway? [y/N] ").strip().lower() == "y":
            return c


def run_init(path="meshscore.toml", ask=input, say=print, radio=None, channels=None):
    """Interactive setup. Returns the path written, or None if cancelled. `radio` / `channels` can be passed in
    (tests, or scripted setups) to skip the radio questions."""
    say("\n" + "=" * 60 + "\n   MeshScore setup\n" + "=" * 60)
    if os.path.exists(path) and ask(f"\n{path} already exists. Overwrite it? [y/N] ").strip().lower() != "y":
        return None
    radio = radio or pick_radio(ask, say)
    if not radio:
        return None
    if channels is None:
        channels = []
        if ask("\nConnect now to read the radio's channels? (close the MeshCore app first) [Y/n] ").strip().lower() != "n":
            try:
                channels = list(asyncio.run(_read_channels(radio)).values())
                say("  Found: " + (", ".join("#" + c for c in channels) or "(no channels)"))
            except Exception as e:
                say(f"  Could not read channels ({e}). You can still continue.")
    bots = {}
    while True:
        say("\nWhich league?")
        for i, (_, label) in enumerate(LEAGUE_CHOICES, 1):
            say(f"  {i}) {label}")
        ans = ask("> ").strip()
        if not (ans.isdigit() and 1 <= int(ans) <= len(LEAGUE_CHOICES)):
            continue
        kind = LEAGUE_CHOICES[int(ans) - 1][0]
        b = {"type": kind}
        name = kind.replace("-", "")
        if kind not in NO_TEAM:
            team, long_name = _pick_team(kind, ask, say)
            if team:
                b["team"] = team
                name = team.lower()
                if long_name:
                    b["label"] = long_name
        b["channel"] = _pick_channel(channels, ask, say, name)
        if kind == "market":
            b["tag"] = "MKT"
            wl = ask("  Stocks to watch (alerts on big moves; e.g. AAPL NVDA SHOP.TO) [none]: ").strip().upper()
            if wl:
                b["watch"] = re.split(r"[\s,]+", wl)
        if kind == "ohl" and not b.get("team"):
            pri = ask("  Priority teams (get every alert even on busy nights, e.g. KIT GUE) [none]: ").strip().upper()
            if pri:
                b["priority_teams"] = re.split(r"[\s,]+", pri)
        while name in bots:
            name += "2"
        bots[name] = b
        if kind == "mlb" and b.get("team") and ask(
                f"\nAlso post every other MLB postseason game on {b['channel']}, even when {b['team']} isn't in it? [y/N] "
        ).strip().lower() == "y":
            ps_name, ps_fields = postseason_companion(b, bots)
            bots[ps_name] = ps_fields
        if ask("\nAdd another bot? [y/N] ").strip().lower() != "y":
            break
    tz = ask("\nTime zone for game times, e.g. America/Toronto [this computer's]: ").strip()
    text = config.render(radio, bots, tz)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    say(f"\nWrote {path}. Next:\n  meshscore check    # test the radio, channels and data sources\n  meshscore run      # go live")
    return path


async def _read_channels(radio):
    from .radio import LibTransport
    CFG.update(radio)
    t = LibTransport()
    try:
        return await t.open()
    finally:
        await t.stop()


# ---------------------------------------------------------------- managing bots in an existing config
def _need_config(args):
    path = config.find_config(getattr(args, "config", None))
    if not path:
        print("No meshscore.toml found. Run `meshscore init` first (or pass --config FILE).")
    return path


def bots_table(text):
    """Printable rows [(name, type, team, channel, state)] from the config text."""
    mods = config.parse(text)["modules"]
    return [(n, str(b.get("type", n)), str(b.get("team", "") or "-"), str(b.get("channel", "")),
             "on" if b.get("enabled", True) else "OFF") for n, b in mods.items()]


def add_bot_interactive(text, args, ask=input, say=print):
    """Build the fields for a new bot from flags, asking for whatever is missing. Returns (name, fields)."""
    mods = config.parse(text)["modules"]
    kind = (args.type or "").lower()
    if not kind:
        say("\nWhich league?")
        for i, (_, label) in enumerate(LEAGUE_CHOICES, 1):
            say(f"  {i}) {label}")
        while True:
            ans = ask("> ").strip()
            if ans.isdigit() and 1 <= int(ans) <= len(LEAGUE_CHOICES):
                kind = LEAGUE_CHOICES[int(ans) - 1][0]
                break
    fields = {"type": kind}
    name = args.name or ""
    team = (args.team or "").upper()
    if kind not in NO_TEAM and not team:
        team, long_name = _pick_team(kind, ask, say)
        if long_name and not args.label:
            fields["label"] = long_name
    if team:
        fields["team"] = team
    if args.label:
        fields["label"] = args.label
    if args.tag:
        fields["tag"] = args.tag
    channel = args.channel or ask("  Channel for this bot (it must exist on the radio), e.g. #jays: ").strip()
    fields["channel"] = "#" + channel.lstrip("#").lower()
    if not name:
        name = (team or kind.replace("-", "")).lower()
        while name in mods:
            name += "2"
    # the postseason bot on a channel that already has a team bot: don't repeat that team's games
    if kind == "mlb-postseason":
        same = [b for b in mods.values() if str(b.get("type")) == "mlb" and b.get("team")
                and str(b.get("channel", "")).lstrip("#").lower() == fields["channel"].lstrip("#")]
        if same:
            fields["skip_teams"] = [str(b["team"]).upper() for b in same]
            say(f"  (this channel already has the {', '.join(fields['skip_teams'])} bot, so its games are skipped here: skip_teams)")
    return name, fields


def cmd_bots(args):
    path = _need_config(args)
    if not path:
        return 2
    with open(path, encoding="utf-8") as f:
        text = f.read()
    action = args.action or "list"
    try:
        if action == "list":
            rows = bots_table(text)
            if not rows:
                print("No bots configured. Add one with: meshscore bots add")
            for n, ty, tm, ch, st in rows:
                print(f"  {n:<14} {ty:<15} {tm:<6} {ch:<12} {st}")
            print(f"\n({path})")
            return 0
        if action == "add":
            name, fields = add_bot_interactive(text, args)
            new = config.add_bot_text(text, name, fields)
            done = f"Added bot {name!r}: {fields}"
            if fields.get("type") == "mlb" and fields.get("team"):
                # an MLB postseason bot already on this channel must not repeat this team's games
                for n, b in config.parse(text)["modules"].items():
                    skip = [str(x).upper() for x in (b.get("skip_teams") or [])]
                    if (str(b.get("type")) == "mlb-postseason" and str(b.get("channel", "")).lstrip("#").lower()
                            == fields["channel"].lstrip("#") and fields["team"] not in skip):
                        new = config.set_field_text(new, n, "skip_teams", skip + [fields["team"]])
                        done += f"\nBot {n!r} (postseason) now skips {fields['team']}'s games on {fields['channel']}."
            if getattr(args, "postseason", False):
                if fields.get("type") != "mlb" or not fields.get("team"):
                    print("Not changed: --postseason goes with an MLB team bot (--type mlb --team CODE).")
                    return 2
                have = [n for n, b in config.parse(text)["modules"].items() if str(b.get("type")) == "mlb-postseason"
                        and str(b.get("channel", "")).lstrip("#").lower() == fields["channel"].lstrip("#")]
                if have:
                    done += f"\n{fields['channel']} already has the postseason bot ({have[0]!r}); not adding another."
                else:
                    ps_name, ps_fields = postseason_companion(fields, {*config.bot_blocks(text), name.lower()})
                    new = config.add_bot_text(new, ps_name, ps_fields)
                    done += f"\nAdded bot {ps_name!r}: {ps_fields}"
        else:
            if not args.target:
                print(f"meshscore bots {action} NAME   (names: {', '.join(config.bot_blocks(text)) or 'none'})")
                return 2
            if action == "remove":
                new = config.remove_bot_text(text, args.target)
                done = f"Removed bot {args.target!r}."
            else:
                new = config.set_enabled_text(text, args.target, action == "enable")
                done = f"Bot {args.target!r} {'enabled' if action == 'enable' else 'disabled'}."
        config.save_edited(path, new)
    except ConfigError as e:
        print(f"Not changed: {e}")
        return 2
    print(done)
    print(f"Saved {path} (previous version: {path}.bak). Restart MeshScore to apply; `meshscore check` tests the new setup.")
    return 0


def cmd_edit(args):
    path = _need_config(args)
    if not path:
        return 2
    import subprocess
    if sys.platform == "win32":
        subprocess.Popen(["notepad", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-t", path])
    else:
        subprocess.Popen([os.environ.get("EDITOR", "xdg-open"), path])
    print(f"Opened {path}. Save it, then restart MeshScore (`meshscore check` validates the file).")
    return 0


# ---------------------------------------------------------------- run
def build_hub(transport):
    """Hub with every enabled bot from the settings added. Raises ConfigError for an unknown type."""
    from .hub import Hub
    from .leagues import load_plugins, registry
    load_plugins(CFG.get("plugin_dirs"))
    types = registry()
    hub = Hub(transport)
    for name, c in CFG["modules"].items():
        if not c.get("enabled", True):
            continue
        typ = str(c.get("type", name)).lower()
        if typ not in types:
            raise ConfigError(f"[bots.{name}] unknown type {typ!r}")
        hub.add(types[typ](hub, name))
    if CFG.get("dm_default") and CFG["dm_default"] not in hub.modules:
        log.warning("dm_default %r is not a configured bot", CFG["dm_default"])
    return hub


async def amain(transport):
    hub = build_hub(transport)
    log.info("MeshScore %s starting (%s -> %s)", __version__, transport.name,
             ", ".join(f"{n}:{m.channel}" for n, m in hub.modules.items()))
    try:
        await hub.run()
    finally:
        await transport.stop()


def cmd_run(args):
    path = config.load_config(args.config)
    if not path:
        print("No meshscore.toml found. Run `meshscore init` to create one (or pass --config FILE).")
        return 2
    data_dir = args.data_dir or os.path.dirname(os.path.abspath(path))
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)
    try:
        config.load_config(path, strict=True)
    except ConfigError as e:
        print(f"Problems in {path}:\n{e}")
        return 2
    setup_logging()
    init_tz()
    CFG["same_air_state"] = "meshscore_same_air.json"          # peers heard on the air survive restarts
    if args.quiet:
        CFG["announce_on_start"] = False
    if args.announce:
        CFG["announce_on_start"] = True
    if not acquire_single_instance():
        print("\nAnother copy of MeshScore is already running on this computer (only one program can use the radio).")
        return 1
    from .radio import LibTransport
    delay = 15
    while True:
        started = time.time()
        try:
            asyncio.run(amain(LibTransport()))
            return 0
        except KeyboardInterrupt:
            log.info("Shutting down")
            return 0
        except Exception:
            # without --forever this ends the program (exit code 1); the reason is in meshscore.log either way
            log.exception("MeshScore stopped because of an unexpected error")
            if not getattr(args, "forever", False):
                return 1
        delay = 15 if time.time() - started > 600 else min(delay * 2, 300)   # crashing again quickly: back off
        log.warning("Restarting in %d s (run --forever)", delay)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


def cmd_health(args):
    """meshscore health: is everything fine? Exit code 0 = yes, 1 = something needs a hand."""
    from . import ops
    path = config.load_config(args.config)
    if not path:
        print("No meshscore.toml found.")
        return 2
    data_dir = args.data_dir or os.path.dirname(os.path.abspath(path))
    if args.test_alert:
        errs = ops.notify(CFG["alerts"], "MeshScore test", "Test alert from `meshscore health --test-alert`. "
                          "If you can read this, phone / chat alerts work.")
        configured = CFG["alerts"].get("ntfy_topic") or CFG["alerts"].get("discord_webhook")
        print("No alert target is set: add ntfy_topic or discord_webhook under [alerts]." if not configured
              else ("Test alert sent." if not errs else "Could not send: " + "; ".join(errs)))
        return 0 if configured and not errs else 1
    checks = ops.health(data_dir, CFG)
    sent = ops.health_notify(data_dir, CFG["alerts"], checks) if args.notify else ""
    if args.quiet:
        if sent:
            print(sent)                          # the watchdog shows this as a Windows notification
    elif args.summary:
        print(ops.summary_line(checks))
    else:
        print(ops.report(checks))
        if sent:
            print("Alert sent: " + sent)
    return 1 if any(c.level == "fail" for c in checks) else 0


def cmd_backup(args):
    """meshscore backup: zip the settings + game/bot state into backups/ (keeps the newest 14).
    meshscore backup --restore FILE: put a backup back (stop the bot first)."""
    from . import ops
    path = config.find_config(args.config)
    if not path:
        print("No meshscore.toml found.")
        return 2
    data_dir = args.data_dir or os.path.dirname(os.path.abspath(path))
    if args.restore:
        if ops_running():
            print("Stop the bot first:  Stop-ScheduledTask -TaskName MeshScore")
            return 1
        names = ops.restore_backup(args.restore, data_dir)
        print(f"Restored {len(names)} files from {args.restore} (the replaced ones are kept as *.bak):")
        for n in names:
            print("  " + n)
        return 0
    zp = ops.make_backup(data_dir, args.dir or "", keep=args.keep)
    print(f"Backup written: {zp}")
    return 0


def ops_running():
    from . import ops
    config.load_config(config.find_config())
    return ops.bot_running(int(CFG.get("instance_port", 0) or 0))


# ---------------------------------------------------------------- check
async def _check_async(mods, radio_too):
    """Probe each bot's data source and (optionally) the radio. Returns the number of failures."""
    from .hub import Hub
    from .radio import LibTransport, Transport
    fails = 0
    probe_hub = Hub(Transport())
    for name, c in mods.items():
        if not c.get("enabled", True):
            continue
        from .leagues import registry
        typ = str(c.get("type", name)).lower()
        try:
            mod = registry()[typ](probe_hub, name)
            what = c.get("team") or "league"
            fn = getattr(mod, "self_test", None)
            if fn:
                msg = await fn()
            else:
                if hasattr(mod, "load_teams"):
                    await mod.load_teams()
                games = await mod.refresh(0) if hasattr(mod, "refresh") else None
                if games is None and hasattr(mod, "scores"):
                    await mod.scores.refresh_live(0)
                    games = mod.scores.all_games()
                msg = f"{len(games)} games in the data window" if games is not None else "reachable"
            print(f"  [ok]   {name:<12} {typ} {what}: {msg}")
        except Exception as e:
            fails += 1
            print(f"  [FAIL] {name:<12} {typ}: {type(e).__name__}: {e}")
    if radio_too:
        t = LibTransport()
        try:
            names = await t.open()
            have = {n.lstrip("#").lower() for n in names.values()}
            print(f"  [ok]   radio       connected via {t.target()}; channels: " + (", ".join("#" + n for n in names.values()) or "(none)"))
            for name, c in mods.items():
                if not c.get("enabled", True):
                    continue
                ch = str(c.get("channel", "")).lstrip("#").lower()
                if c.get("channel_index") is None and ch not in have:
                    fails += 1
                    print(f"  [FAIL] {name:<12} channel #{ch} is not on the radio - add it in the MeshCore app")
        except Exception as e:
            fails += 1
            print(f"  [FAIL] radio       {type(e).__name__}: {e}")
        finally:
            await t.stop()
    return fails


def cmd_check(args):
    path = config.load_config(args.config)
    if not path:
        print("No meshscore.toml found. Run `meshscore init` first.")
        return 2
    print(f"Config: {path}")
    bad = config.problems()
    for b in bad:
        print(f"  [FAIL] {b}")
    if bad:
        return 2
    print("  [ok]   settings are valid")
    setup_logging()
    CFG["log_file"] = ""
    setup_logging()
    init_tz()
    from .leagues import load_plugins
    load_plugins(CFG.get("plugin_dirs"))
    fails = asyncio.run(_check_async(CFG["modules"], not args.offline))
    print("\nAll good." if not fails else f"\n{fails} problem(s).")
    return 1 if fails else 0


# ---------------------------------------------------------------- small tools
def cmd_ports(args):
    ports = list_ports()
    for p in ports:
        print(f"{p.device:<12} {p.description}")
    if not ports:
        print("No serial ports found. Is the radio plugged in (Companion USB firmware)?")
    return 0


def cmd_scan_ble(args):
    print("Scanning 8 s ...")
    for name, addr, rssi in sorted(scan_ble_devices(), key=lambda r: -(r[2] or -999)):
        if name:
            print(f"  {'*' if is_mesh_name(name) else ' '} {addr}  {name}  ({rssi} dBm)")
    return 0


def cmd_teams(args):
    from .teams import SOURCES, team_list
    if args.league not in SOURCES:
        print("League must be one of: " + ", ".join(sorted(SOURCES)))
        return 2
    rows = team_list(args.league)
    for code, name in rows:
        print(f"{code:<6} {name}")
    if not rows:
        print("Could not load the team list (network?).")
        return 1
    return 0


def cmd_dump(args):
    config.load_config(args.config)
    setup_logging()
    init_tz()
    CFG["log_file"] = ""
    what, team = args.what, args.team
    if what in ("hockeytech", "ohl"):
        from .leagues.hockeytech import api_get, scorebar_url
        data = api_get(scorebar_url(0, 1))
        sb = ((data or {}).get("SiteKit", {}).get("Scorebar") or [])
        print(json.dumps(sb[0] if sb else data, indent=2)[:4000])
    elif what == "summary":
        from .leagues.hockeytech import api_get, parse_goals, parse_shots, parse_stars, summary_url
        data = api_get(summary_url(team))
        print(json.dumps(data, indent=2)[:6000])
        print("\nparsed goals:", parse_goals(data), "| shots:", parse_shots(data), "| stars:", parse_stars(data))
    elif what == "nhl":
        from .leagues.nhl import NhlModule, nhl_get, parse_ngame
        data = nhl_get(f"/club-schedule-season/{(team or 'TOR').lower()}/now")
        games = [x for x in (parse_ngame(g) for g in (data or {}).get("games", [])) if x]
        today = now().date()
        for g in games:
            if g.start and abs((g.start.date() - today).days) <= 4:
                print(g.date, g.id, g.state, "|", NhlModule.line(g))
        print(f"({len(games)} games in the season schedule)")
    elif what == "nfl":
        from .leagues.nfl import ESPN_NFL, NFL_SEASON_TYPES, NflModule, espn_get, parse_nfl_game
        from .core import _i
        games = {}
        for phase, code in NFL_SEASON_TYPES.items():
            data = espn_get(f"{ESPN_NFL}/teams/{(team or 'BUF').lower()}/schedule", {"seasontype": code})
            for e in (data or {}).get("events", []):
                g = parse_nfl_game(e["competitions"][0], phase, e["id"], e.get("date"), _i((e.get("week") or {}).get("number")))
                if g:
                    games[g.id] = g
        today = now().date()
        for g in sorted(games.values(), key=lambda g: g.start or now()):
            if g.start and abs((g.start.date() - today).days) <= 10:
                print(g.start.date(), g.id, g.state, g.phase, "|", NflModule.line(g))
        print(f"({len(games)} games in the season schedule)")
    elif what == "mlb":
        from datetime import timedelta
        from .leagues.mlb import MlbModule, TEAM_ABBR, mlb_get, parse_mgame
        tid = 0
        data = mlb_get("/v1/teams", {"sportId": 1, "season": now().year})
        for t in (data or {}).get("teams", []):
            TEAM_ABBR[t["id"]] = t.get("abbreviation", "")
            if (team or "TOR").upper() in (t.get("abbreviation"), str(t["id"])):
                tid = t["id"]
        today = now().date()
        data = mlb_get("/v1/schedule", {"sportId": 1, "teamId": tid, "hydrate": "linescore,team,decisions,probablePitcher",
                                        "startDate": (today - timedelta(days=3)).isoformat(),
                                        "endDate": (today + timedelta(days=3)).isoformat()})
        for day in (data or {}).get("dates", []):
            for g in day["games"]:
                m = parse_mgame(g)
                print(day["date"], m.pk, m.state, "|", MlbModule.line(m) if m else "parse failed")
    elif what in ("post", "postseason"):
        from .leagues.mlb_postseason import dump_post
        asyncio.run(dump_post(int(team) if team and team.isdigit() else 0))
    else:
        print("what must be one of: hockeytech, summary GAME_ID, nhl [TEAM], mlb [TEAM], nfl [TEAM], post [YEAR]")
        return 2
    return 0


# ---------------------------------------------------------------- entry point
def build_parser():
    ap = argparse.ArgumentParser(prog="meshscore", description="Live sports scores for MeshCore mesh channels.")
    ap.add_argument("--version", action="version", version=f"meshscore {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    def add_cfg(p):
        p.add_argument("-c", "--config", help="path to meshscore.toml (default: ./meshscore.toml or the user config dir)")

    p = sub.add_parser("run", help="run the bot (the default command)")
    add_cfg(p)
    p.add_argument("--data-dir", help="where state and log files go (default: next to the config file)")
    p.add_argument("--quiet", action="store_true", help="no 'active' message in the channels at start (default)")
    p.add_argument("--announce", action="store_true", help="post an 'active' message in each channel at start")
    p.add_argument("--forever", action="store_true", help="restart by itself after an unexpected error (for services)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("health", help="is the bot, the radio and every web board OK? (exit code 1 = needs a hand)")
    add_cfg(p)
    p.add_argument("--data-dir", help="where state and log files are (default: next to the config file)")
    p.add_argument("--summary", action="store_true", help="one line only")
    p.add_argument("--notify", action="store_true",
                   help="send a phone/chat alert (see [alerts]) when the state changes: broken, still broken, recovered")
    p.add_argument("--quiet", action="store_true", help="print nothing unless an alert was sent (for schedulers)")
    p.add_argument("--test-alert", action="store_true", help="send a test message to the [alerts] targets and exit")
    p.set_defaults(fn=cmd_health)

    p = sub.add_parser("backup", help="zip settings + game/bot state into backups/ (or --restore one)")
    add_cfg(p)
    p.add_argument("--data-dir", help="where state files are (default: next to the config file)")
    p.add_argument("--dir", help="where to put the zip (default: <data dir>/backups)")
    p.add_argument("--keep", type=int, default=14, help="how many backups to keep (default 14)")
    p.add_argument("--restore", metavar="ZIP", help="restore this backup (the bot must be stopped)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("init", help="interactive setup: radio, channels, teams -> meshscore.toml")
    p.add_argument("-o", "--output", default=config.CONFIG_NAME, help="where to write the config")
    p.set_defaults(fn=lambda a: 0 if run_init(a.output) else 1)

    p = sub.add_parser("check", help="validate the config, test the data sources and the radio")
    add_cfg(p)
    p.add_argument("--offline", action="store_true", help="skip the radio connection")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("bots", help="list / add / remove / enable / disable the bots in your config")
    p.add_argument("action", nargs="?", choices=["list", "add", "remove", "enable", "disable"], default="list")
    p.add_argument("target", nargs="?", help="bot name (remove / enable / disable)")
    p.add_argument("--type", help="add: nhl | mlb | nfl | ohl | mlb-postseason | market | chess | pokemon | word | trivia | pickem | coverage | weather")
    p.add_argument("--team", help="add: team code")
    p.add_argument("--channel", help='add: channel on the radio, e.g. "#jays"')
    p.add_argument("--name", help="add: bot name (also its command prefix)")
    p.add_argument("--tag", help="add: text in messages instead of the team code")
    p.add_argument("--label", help="add: long name")
    p.add_argument("--postseason", action="store_true",
                   help="add an MLB team bot: also post every other MLB postseason game on its channel")
    add_cfg(p)
    p.set_defaults(fn=cmd_bots)

    p = sub.add_parser("edit", help="open meshscore.toml in your text editor")
    add_cfg(p)
    p.set_defaults(fn=cmd_edit)

    sub.add_parser("ports", help="list USB serial ports").set_defaults(fn=cmd_ports)
    sub.add_parser("scan-ble", help="list nearby Bluetooth devices").set_defaults(fn=cmd_scan_ble)

    p = sub.add_parser("teams", help="list team codes for a league")
    p.add_argument("league", help="nhl | mlb | nfl | ohl")
    p.set_defaults(fn=cmd_teams)

    p = sub.add_parser("dump", help="print what a data source returns (debugging)")
    p.add_argument("what", help="hockeytech | summary | nhl | mlb | nfl | post")
    p.add_argument("team", nargs="?", help="team code, game id or year, depending on `what`")
    add_cfg(p)
    p.set_defaults(fn=cmd_dump)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252 and choke on the emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):                       # bare `meshscore`: run if configured, else guide
        if config.find_config():
            args = build_parser().parse_args(["run"])
        else:
            print("MeshScore: live sports scores for MeshCore.\n\nFirst time? Run:  meshscore init\n"
                  "Then:            meshscore check   and   meshscore run\n(meshscore --help lists everything)")
            return 0
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"Config problem: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
