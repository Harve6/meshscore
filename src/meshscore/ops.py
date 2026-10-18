"""Looking after a running MeshScore without help: `meshscore health` and `meshscore backup`.

* ``health(...)`` reads the log, the instance lock and each web board and says, in plain words, what is fine and what
  needs a hand (radio unplugged, a day of failed posts, a stale web board, no recent backup ...).
* ``make_backup(...)`` zips the settings and every game/bot state into ``backups/`` and keeps the newest few. ROMs are
  never included (they are large and yours to keep). The zip holds ``meshscore.toml``, which has your upload tokens:
  keep the backups private.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime

BACKUP_PATTERNS = ("meshscore.toml", "*_state.json", "*.state", "pokemon-inputs.log", "meshscore_same_air.json",
                   "meshscore_phases.json")
BACKUP_DIR = "backups"
KEEP = 14
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+) (.*)$")


# ---------------------------------------------------------------- backups
def make_backup(data_dir: str = ".", out_dir: str = "", keep: int = KEEP, clock=time.time) -> str:
    """Zip the settings + state files of `data_dir` into `out_dir` (default data_dir/backups). Returns the zip path."""
    data_dir = os.path.abspath(data_dir)
    out_dir = os.path.abspath(out_dir or os.path.join(data_dir, BACKUP_DIR))
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.fromtimestamp(clock()).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, f"meshscore-{stamp}.zip")
    files = sorted({f for p in BACKUP_PATTERNS for f in glob.glob(os.path.join(data_dir, p)) if os.path.isfile(f)})
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, os.path.basename(f))
    os.replace(tmp, path)
    for old in list_backups(out_dir)[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def list_backups(out_dir: str) -> list[str]:
    """Newest first."""
    return sorted(glob.glob(os.path.join(out_dir, "meshscore-*.zip")), reverse=True)


def restore_backup(zip_path: str, data_dir: str = ".") -> list[str]:
    """Put a backup's files back into `data_dir` (the bot must be stopped). The files it replaces are kept as *.bak."""
    restored = []
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if os.path.basename(name) != name or name.startswith("."):
                continue                                    # only plain file names, never paths
            dst = os.path.join(data_dir, name)
            if os.path.exists(dst):
                shutil.copyfile(dst, dst + ".bak")
            with z.open(name) as src, open(dst + ".tmp", "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(dst + ".tmp", dst)
            restored.append(name)
    return restored


# ---------------------------------------------------------------- health
@dataclass
class Check:
    name: str
    level: str          # "ok" | "warn" | "fail"
    detail: str

    @property
    def mark(self) -> str:
        return {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[self.level]


def bot_running(port: int) -> bool:
    """Is a MeshScore holding its single-instance port right now?"""
    if not port:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", int(port)))
        return False                                        # we got it: nobody else has it
    except OSError:
        return True
    finally:
        s.close()


def _log_rows(data_dir: str, log_file: str, limit: int = 6000):
    rows = []
    for p in (os.path.join(data_dir, log_file + ".1"), os.path.join(data_dir, log_file)):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines():
                    m = _TS.match(line)
                    if m:
                        try:
                            rows.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp(), m.group(2), m.group(3)))
                        except ValueError:
                            pass
        except OSError:
            continue
    rows.sort(key=lambda r: r[0])
    return rows[-limit:]


def _ago(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 120:
        return f"{s} s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def health(data_dir: str, cfg: dict, now: float | None = None, fetch=None, running=None) -> list[Check]:
    """Every check, in the order a person would look. `fetch(url) -> dict` and `running() -> bool` can be replaced
    in tests."""
    now = time.time() if now is None else now
    out = []

    # 1. is it running?
    is_running = running() if running else bot_running(int(cfg.get("instance_port", 0) or 0))
    out.append(Check("bot running", "ok" if is_running else "fail",
                     "yes" if is_running else "no MeshScore is running - start it (Start-ScheduledTask -TaskName MeshScore)"))

    # 2-4. what the log says
    rows = _log_rows(data_dir, cfg.get("log_file") or "meshscore.log")
    if not rows:
        out.append(Check("log", "warn", "no log lines found"))
    else:
        last_line = rows[-1][0]
        connected = [r[0] for r in rows if r[2].startswith("Connected. Channels:")]
        transport_err = [r for r in rows if "Transport error" in r[2]]
        tx_ok = [r[0] for r in rows if r[2].startswith("TX OK")]
        day = [r for r in rows if r[0] >= now - 86400]
        fails_24h = sum(1 for r in day if r[2].startswith("TX FAIL"))
        crashes_24h = sum(1 for r in day if "stopped because of an unexpected error" in r[2])
        # radio: the latest of "connected" and "transport error" decides
        last_conn = connected[-1] if connected else None
        last_err = transport_err[-1] if transport_err else None
        if last_err and (not last_conn or last_err[0] > last_conn):
            reason = last_err[2].split("Transport error:", 1)[-1].strip()[:90]
            hint = " - unplug the radio, wait 5 s, plug it back in" if "not functioning" in reason or "could not open" in reason else ""
            out.append(Check("radio link", "fail", f"down since {_ago(now - last_err[0])}: {reason}{hint}"))
        elif last_conn:
            out.append(Check("radio link", "ok", f"connected {_ago(now - last_conn)}"))
        else:
            out.append(Check("radio link", "warn", "no connection in the log yet"))
        # sending: a run of failures at the end of the log is the silent-radio case
        tail = [r for r in rows if r[2].startswith(("TX OK", "TX FAIL"))][-5:]
        streak = 0
        for r in reversed(tail):
            if r[2].startswith("TX FAIL"):
                streak += 1
            else:
                break
        if streak >= 3:
            out.append(Check("sending", "fail", f"the last {streak} messages failed to send ({fails_24h} in 24 h)"))
        elif fails_24h:
            out.append(Check("sending", "warn", f"{fails_24h} message(s) failed in the last 24 h; latest OK {_ago(now - tx_ok[-1]) if tx_ok else 'never'}"))
        elif tx_ok:
            quiet = now - tx_ok[-1]
            out.append(Check("sending", "ok" if quiet < 36 * 3600 else "warn",
                             f"last message sent {_ago(quiet)}" + ("" if quiet < 36 * 3600 else " - unusually quiet")))
        else:
            out.append(Check("sending", "warn", "nothing sent yet"))
        out.append(Check("crashes", "ok" if not crashes_24h else "warn",
                         "none in 24 h" if not crashes_24h else f"{crashes_24h} restart(s) after an error in 24 h - see meshscore.log"))
        if is_running and now - last_line > 6 * 3600:
            out.append(Check("log", "warn", f"no log line for {_ago(now - last_line)} (fine if nothing happened)"))

    # 5. the web boards
    for name, m in (cfg.get("modules") or {}).items():
        url = str(m.get("site_url") or "").strip()
        if not url or not m.get("enabled", True):
            continue
        try:
            st = (fetch or _fetch_json)(url.rstrip("/") + "/state.json")
            age = now - float(st.get("updated", 0))
            out.append(Check(f"board {name}", "ok" if age < 1800 else "warn",
                             f"updated {_ago(age)}" + ("" if age < 1800 else " - the page may look offline")))
        except Exception as e:
            out.append(Check(f"board {name}", "warn", f"could not read {url}: {str(e)[:80]}"))

    # 6. backups
    backups = list_backups(os.path.join(data_dir, BACKUP_DIR))
    if not backups:
        out.append(Check("backups", "warn", "none yet - run: meshscore backup"))
    else:
        age = now - os.path.getmtime(backups[0])
        out.append(Check("backups", "ok" if age < 3 * 86400 else "warn", f"{len(backups)} kept, newest {_ago(age)}"))

    # 7. disk
    try:
        free = shutil.disk_usage(data_dir).free
        out.append(Check("disk", "ok" if free > 1_000_000_000 else "warn", f"{free / 1e9:.1f} GB free"))
    except OSError:
        pass
    return out


def _fetch_json(url: str) -> dict:
    import requests
    r = requests.get(url, params={"t": int(time.time())}, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- phone / chat alerts
ALERT_STATE = "health-state.json"


def notify(alerts: dict, title: str, text: str, urgent: bool = False, post=None) -> list[str]:
    """Send `text` to ntfy.sh and/or a Discord webhook (whichever are configured). Returns what went wrong, if
    anything ([] = every configured channel accepted it). `post(url, **kw)` can be replaced in tests."""
    import requests
    post = post or (lambda url, **kw: requests.post(url, timeout=15, **kw))
    problems = []
    topic = str((alerts or {}).get("ntfy_topic") or "").strip()
    if topic:
        server = str(alerts.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
        try:
            r = post(f"{server}/{topic}", data=text.encode("utf-8"),
                     headers={"Title": title, "Priority": "high" if urgent else "default",
                              "Tags": "rotating_light" if urgent else "white_check_mark"})
            if r.status_code >= 300:
                problems.append(f"ntfy: HTTP {r.status_code}")
        except Exception as e:
            problems.append(f"ntfy: {str(e)[:80]}")
    hook = str((alerts or {}).get("discord_webhook") or "").strip()
    if hook:
        try:
            r = post(hook, json={"content": f"**{title}**\n{text}"})
            if r.status_code >= 300:
                problems.append(f"discord: HTTP {r.status_code}")
        except Exception as e:
            problems.append(f"discord: {str(e)[:80]}")
    return problems


def health_notify(data_dir: str, alerts: dict, checks: list[Check], now: float | None = None, post=None) -> str:
    """Tell the owner when the situation CHANGES: broken, still broken (a reminder every `repeat_hours`), recovered.
    Returns the message sent ("" when nothing needed saying). State lives in health-state.json."""
    import json
    now = time.time() if now is None else now
    path = os.path.join(data_dir, ALERT_STATE)
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    bad = [c for c in checks if c.level == "fail"]
    cur = "fail" if bad else "ok"
    prev = st.get("status", "ok")
    msg = ""
    if cur == "fail":
        detail = summary_line(checks)
        repeat = float((alerts or {}).get("repeat_hours", 6) or 0) * 3600
        if prev != "fail":
            msg = f"MeshScore needs a hand: {detail}"
            st = {"status": "fail", "since": now, "last_sent": now, "summary": detail}
        elif repeat and now - float(st.get("last_sent", 0)) >= repeat:
            msg = f"Still broken ({_ago(now - float(st.get('since', now))).replace(' ago', '')}): {detail}"
            st["last_sent"] = now
        else:
            st["summary"] = detail
    elif prev == "fail":
        msg = f"MeshScore is back to normal (was down {_ago(now - float(st.get('since', now))).replace(' ago', '')})"
        st = {"status": "ok", "since": now}
    else:
        st = {"status": "ok", "since": st.get("since", now)}
    if msg:
        errs = notify(alerts, "MeshScore", msg, urgent=cur == "fail", post=post)
        if errs:                                             # it did not go out: try again next time, do not lose the news
            st["last_sent"] = 0 if cur == "fail" else st.get("last_sent", 0)
            if cur == "ok":
                st = {"status": "fail", "since": st.get("since", now), "last_sent": 0, "summary": "recovered (alert pending)"}
            msg = f"{msg}  [could not send: {'; '.join(errs)}]"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, path)
    return msg


def report(checks: list[Check]) -> str:
    lines = [f"[{c.mark}] {c.name:<14} {c.detail}" for c in checks]
    bad = [c for c in checks if c.level == "fail"]
    warn = [c for c in checks if c.level == "warn"]
    lines.append("")
    lines.append("All good." if not bad and not warn else
                 f"{len(bad)} problem(s), {len(warn)} warning(s)." if bad else f"OK, {len(warn)} warning(s).")
    return "\n".join(lines)


def summary_line(checks: list[Check]) -> str:
    bad = [c for c in checks if c.level == "fail"]
    if bad:
        return "; ".join(f"{c.name}: {c.detail}" for c in bad)[:240]
    return "all good"
