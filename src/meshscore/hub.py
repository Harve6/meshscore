"""The hub: owns the radio link, the transmit queue and command routing for every league plugin."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .core import CFG, SELF_MARKERS, fit, fmt_uptime, norm_mode
from .radio import chan_key
from .sameair import AirBudget, SameAir

log = logging.getLogger("meshscore")

TX_RETRY_WAIT = 2      # seconds before the one retry of a failed transmission
ALERT_TTL = 900        # an alert still queued after this long (radio was down) is stale news - dropped (finals are kept)
REPLY_TTL = 120        # likewise for an answer to a command nobody is waiting for any more


@dataclass
class Event:
    kind: str
    teams: tuple
    texts: list = field(default_factory=list)
    cup: bool = False          # a Memorial Cup game: never thinned out by busy-night mode
    prio: bool = False         # a priority team's game (#ohl): always gets full alerts


class Hub:
    """Owns the radio link, the transmit queue and command routing. Sports are pluggable modules,
    each bound to its own channel (module.name / .channel / .tag / .commands / .poll_loop / .help)."""

    def __init__(self, transport):
        self.t = transport
        self.modules = {}
        self.by_channel = {}
        self.aliases = {}
        self.out = asyncio.Queue()
        self.cmdq = asyncio.Queue(maxsize=10)
        self.cooldowns = {}
        self.started = time.time()
        self.booted = False
        self.same_air = SameAir(path=CFG.get("same_air_state") or None)   # listen-before-send dedupe with other bots
        self.heard = {}        # chan_key -> {sender name: last time heard}: lets a web page say the radio is hearing people
        self.airtime = AirBudget()                                         # optional messages-per-hour cap

    def add(self, mod):
        self.modules[mod.name] = mod
        self.by_channel.setdefault(chan_key(mod.channel), []).append(mod)   # modules may share a channel (first match wins)
        for a in getattr(mod, "aliases", ()):
            self.aliases[a] = mod

    # ---------------- sending ----------------
    def say_chan(self, channel, texts, reply=False):
        """Queue channel posts. reply=True marks an answer to a command (deduped by exact text, never budget-capped)."""
        for t in [texts] if isinstance(texts, str) else texts:
            self._enqueue(("chan", channel, fit(t), {"reply": reply, "t": time.time()}))

    def say_dm(self, key, text):
        self._enqueue(("dm", key, fit(text), {"t": time.time()}))

    def _enqueue(self, item):
        if self.out.qsize() >= 25:
            log.warning("Out queue full, dropping: %s", item[2])
            return
        self.out.put_nowait(item)

    def reply(self, mod, inc, texts):
        for t in texts[: CFG["max_reply_msgs"]]:
            if inc.is_dm and inc.sender_key:
                self.say_dm(inc.sender_key, t)
            else:
                self.say_chan(mod.channel, t, reply=True)

    async def tx_worker(self):
        while True:
            kind, dest, text, meta = await self.out.get()
            while not self.t.alive:
                await asyncio.sleep(1)
            age = time.time() - meta.get("t", time.time())
            if age > (REPLY_TTL if meta.get("reply") or kind == "dm" else ALERT_TTL) and AirBudget.tier(text) != 0:
                log.info("Dropping stale message (%ds old): %s", age, text)
                continue
            if kind == "chan":
                is_reply = bool(meta.get("reply"))
                if not is_reply and not self.airtime.allow(text):
                    log.info("AIRTIME budget reached (%d/h), not sending: %s", self.airtime.limit, text)
                    continue
                wait = self.same_air.delay(is_reply)
                if wait:
                    await asyncio.sleep(wait)       # peers on the air: a random head start for whoever noticed first
                if self.same_air.is_duplicate(dest, text, is_reply):
                    self.same_air.skipped += 1
                    log.info("SAME-AIR: already on the air, skipping: %s", text)
                    continue
            ok = False
            for attempt in (1, 2):
                try:
                    ok = await (self.t.send_channel(dest, text) if kind == "chan" else self.t.send_dm(dest, text))
                except Exception as e:
                    log.warning("TX error: %s", e)
                if ok:
                    break
                await asyncio.sleep(TX_RETRY_WAIT)
            if ok and kind == "chan":
                self.airtime.record()
            log.info("TX %s%s: %s", "OK " if ok else "FAIL ", f"[DM {dest[:6]}]" if kind == "dm" else f"[{dest}]", text)
            await asyncio.sleep(CFG["send_gap"])

    # ---------------- receiving ----------------
    def route(self, inc):
        """-> (module, command, arg) or None.  '!jays next' works anywhere; plain '!next' goes by channel
        (or to dm_default in a DM)."""
        parts = inc.text.split()
        cmd, rest = parts[0].lower(), parts[1:]
        mod = self.modules.get(cmd[1:]) or self.aliases.get(cmd[1:])
        if mod:  # explicit "!jays ..." / "!ohl ..." / "!ps ..."
            # "!jays" alone is its help; a bot can name another command for that (the market bot: "!market" = the indices)
            cmd, rest = ("!" + rest[0].lower(), rest[1:]) if rest else (getattr(mod, "bare_command", "!help"), [])
        elif inc.is_dm:
            mod = self.modules.get(CFG["dm_default"]) or next(iter(self.modules.values()), None)
            if mod is not None and cmd not in mod.commands:        # a plain "!up" DM: whichever bot has that command
                mod = next((m for m in self.modules.values() if cmd in m.commands), mod)
        else:
            mods = self.by_channel.get(chan_key(inc.channel), [])
            # bots sharing a channel: first one that has the command; a bot that lists it in `defer_cmds` steps aside
            # for another bot on the channel that also has it (a team bot's !series vs the postseason bot's)
            have = [x for x in mods if cmd in x.commands]
            have.sort(key=lambda x: cmd in getattr(x, "defer_cmds", ()))
            mod = have[0] if have else (mods[0] if mods else None)
        if not mod or cmd not in mod.commands:
            return None
        inc.rest = rest
        return mod, cmd, (rest[0] if rest else "")

    async def on_message(self, inc):
        text = inc.text.strip()
        if not inc.is_dm and inc.channel:
            self.same_air.hear(inc.channel, text, inc.sender_name)      # what the air already says (for dedupe)
            if inc.sender_name and not text.startswith(SELF_MARKERS):
                self.heard.setdefault(chan_key(inc.channel), {})[inc.sender_name] = time.time()
        if not text.startswith("!") or any(m in text for m in SELF_MARKERS) \
                or any(f"[{m.tag}" in text for m in self.modules.values()):
            return
        if inc.is_dm and not CFG["respond_to_dms"]:
            return
        inc.text = text
        routed = self.route(inc)
        if routed:
            try:
                self.cmdq.put_nowait((inc, *routed))
            except asyncio.QueueFull:
                log.warning("Command queue full, dropping %s", routed[1])

    async def cmd_worker(self):
        while True:
            inc, mod, cmd, arg = await self.cmdq.get()
            key = (mod.name, inc.sender_key if inc.is_dm else "chan", cmd,
                   arg.lower() if cmd in mod.arg_cmds else "")
            if cmd not in getattr(mod, "no_cooldown_cmds", ()) and \
                    time.time() - self.cooldowns.get(key, 0) < CFG["command_cooldown"]:
                log.info("Cooldown: %s %s", mod.name, cmd)
                continue
            self.cooldowns[key] = time.time()
            log.info("RX %s %s %s from %s%s", mod.name, cmd, arg, inc.sender_name or "?", " (DM)" if inc.is_dm else "")
            try:
                replies = await mod.commands[cmd](inc, arg)
            except Exception:
                log.exception("Command %s %s failed", mod.name, cmd)
                replies = [f"[{mod.tag} BOT] Error, try again later"]
            if replies:
                self.reply(mod, inc, replies)

    def heard_summary(self, channel, window=86400):
        """(seconds since anyone was heard on `channel` or None, distinct senders heard within `window`)."""
        now = time.time()
        seen = {n: t for n, t in self.heard.get(chan_key(channel), {}).items() if now - t <= window}
        last = max(seen.values()) if seen else None
        return (int(now - last) if last is not None else None), len(seen)

    # ---------------- shared command bits ----------------
    async def cmd_ping(self, mod, inc):
        extra = (f" | {inc.hops} hop{'s' if inc.hops != 1 else ''}" if inc.hops is not None else "") + \
                (f" | SNR {inc.snr}" if inc.snr is not None else "")
        return [f"[{mod.tag} BOT] pong{extra}"]

    async def status(self, mod, ok_at):
        age = time.time() - ok_at if ok_at else None
        api = f"{int(age)}s ago" if age is not None else "never"
        extra = await self.t.status_extra()
        air = f" | {self.airtime.per_hour()}/h" + (f" | dup-skipped {self.same_air.skipped}" if self.same_air.skipped else "") +               (f" | {len(self.same_air.peers)} peer bot{'s' if len(self.same_air.peers) != 1 else ''}" if self.same_air.peers else "")
        return [f"[{mod.tag} BOT] up {fmt_uptime(time.time() - self.started)} | API {api} | q{self.out.qsize()} | "
                f"level {norm_mode(mod.state.data['mode'])}{mod.status_extra()}{air}{extra}"]

    # ---------------- connection ----------------
    async def transport_supervisor(self):
        backoff = 5
        channels = {n: CFG["modules"][n] for n in self.modules}
        while True:
            try:
                await self.t.start(self.on_message, channels)
                self.same_air.own_name = getattr(self.t, "own_name", "") or ""
                backoff = 5
                if not self.booted:
                    self.booted = True
                    announced = set()
                    for m in self.modules.values() if CFG["announce_on_start"] else ():
                        if chan_key(m.channel) in self.t.chans and chan_key(m.channel) not in announced:
                            announced.add(chan_key(m.channel))
                            self.say_chan(m.channel, f"[{m.tag} BOT] {m.blurb} active on {m.channel} (!help)")
                while self.t.alive:
                    await asyncio.sleep(3)
                log.warning("Transport dropped; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Transport error: %s", e)
                if not getattr(self, "_hinted", False):
                    self._hinted = True
                    log.info("Hint: is the radio plugged in and switched on, and is the MeshCore app (or another program) "
                             "closed? `meshscore ports` / `meshscore scan-ble` show what is available. Retrying with backoff.")
            try:
                await self.t.stop()
            except Exception:
                pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def run(self):
        await asyncio.gather(self.tx_worker(), self.cmd_worker(), self.transport_supervisor(),
                             *[m.run() for m in self.modules.values()])
