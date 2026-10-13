"""The MeshCore radio link: a transport interface plus the `meshcore` library implementation (serial / BLE / TCP)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional


from .core import CFG

log = logging.getLogger("meshscore")


# ==========================================================================
# TRANSPORTS
# ==========================================================================
@dataclass
class Incoming:
    text: str
    is_dm: bool
    sender_name: str = ""
    sender_key: str = ""
    hops: Optional[int] = None
    snr: Optional[float] = None
    channel: str = ""          # channel name the message arrived on ("" for DMs)
    rest: list = field(default_factory=list)   # every word after the command (commands like !follow need multi-word names)


class Transport:
    name = "base"
    alive = False
    own_name = ""            # this radio's node name (so a bot never mistakes its own posts for another bot's)

    async def start(self, on_message, channels): raise NotImplementedError
    async def stop(self): pass
    async def send_channel(self, channel, text): return False
    async def send_dm(self, key, text): return False
    def find_contact(self, name=None, key=None): return None
    async def status_extra(self): return ""


def chan_key(name):
    return str(name).strip().lstrip("#").lower()


class LibTransport(Transport):
    """Persistent connection using the `meshcore` Python library (TCP / serial / BLE)."""

    def __init__(self):
        self.mc = None
        self.radio_names = None  # {slot: channel name} read by open(), used once by start()
        self.chans = {}          # chan_key -> radio channel index
        self.idx_name = {}       # radio channel index -> chan_key
        self.on_message = None
        self.alive = False
        self.ET = None
        self.name = f"meshcore-{CFG['transport']}"

    def target(self):
        t = CFG["transport"]
        if t == "tcp":
            return f"{CFG['host']}:{CFG['tcp_port']}"
        return (CFG["ble_address"] or "BLE") if t == "ble" else CFG["port"]

    async def _connect(self):
        from meshcore import MeshCore
        t = CFG["transport"]
        if t == "tcp":
            return await MeshCore.create_tcp(CFG["host"], int(CFG["tcp_port"]))
        if t == "ble":
            return await MeshCore.create_ble(CFG["ble_address"] or None, pin=CFG["ble_pin"] or None)
        return await MeshCore.create_serial(CFG["port"], CFG["baud"])

    async def open(self):
        """Connect and read the radio's channel names ({slot: name}); start() then reuses this connection."""
        from meshcore import EventType
        self.ET = EventType
        await self._ensure_connected()
        self.radio_names = await self._read_channel_names()
        return self.radio_names

    async def _ensure_connected(self):
        if self.mc is not None:
            return
        log.info("Connecting to %s (%s) ...", self.target(), CFG["transport"])
        mc = await self._connect()
        if mc is None:
            raise ConnectionError(f"Could not connect to {self.target()} "
                                  f"(radio off, wrong address, or another app connected?)")
        self.mc = mc
        info = getattr(mc, "self_info", None) or {}
        self.own_name = str(info.get("name", "") or "")
        await self._load_contacts()

    async def start(self, on_message, channels):
        """channels: {module_name: {"channel": "#ohl", "channel_index": None}}"""
        from meshcore import EventType
        self.ET, self.on_message = EventType, on_message
        await self._ensure_connected()
        await self._map_channels(channels)
        mc = self.mc
        mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_chan)
        mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
        if getattr(EventType, "DISCONNECTED", None) is not None:
            mc.subscribe(EventType.DISCONNECTED, self._on_disc)
        await mc.start_auto_message_fetching()
        self.alive = True
        log.info("Connected. Channels: %s", ", ".join(f"#{k}=slot {i}" for k, i in self.chans.items()))

    async def stop(self):
        self.alive = False
        if self.mc:
            try:
                await self.mc.disconnect()
            except Exception:
                pass
            self.mc = None

    async def _load_contacts(self):
        try:
            if hasattr(self.mc, "ensure_contacts"):
                await self.mc.ensure_contacts()
            else:
                await self.mc.commands.get_contacts()
        except Exception as e:
            log.warning("Could not load contacts: %s", e)

    async def _read_channel_names(self):
        # Read every slot: channels can sit after a run of empty slots (e.g. #bills in slot 7 with 4-6 unused).
        names = {}
        for idx in range(16):
            try:
                r = await self.mc.commands.get_channel(idx)
            except Exception:
                r = None
            name = ""
            if r is not None and r.type != self.ET.ERROR:
                name = str((r.payload or {}).get("channel_name", "")).strip("\x00 ")
            if name:
                names[idx] = name
        log.info("Scanned radio channel slots 0-15, found %d: %s", len(names),
                 ", ".join(f"{i}={n}" for i, n in names.items()) or "none")
        return names

    async def _map_channels(self, channels):
        wanted = {chan_key(c["channel"]): c.get("channel_index") for c in channels.values()}
        found = {k: int(i) for k, i in wanted.items() if i is not None}
        names = self.radio_names if self.radio_names is not None else await self._read_channel_names()
        self.radio_names = None
        seen = [f"{i}:{n}" for i, n in names.items()]
        for idx, name in names.items():
            if chan_key(name) in wanted and chan_key(name) not in found:
                found[chan_key(name)] = idx
        missing = [k for k in wanted if k not in found]
        if missing:
            log.error("Channel(s) not on the radio: %s (radio has: %s). Add them in the MeshCore app, "
                      "or set channel_index in meshscore.toml.", ", ".join("#" + m for m in missing), seen)
        if not found:
            raise RuntimeError("None of the configured channels exist on the radio")
        self.chans = found
        self.idx_name = {i: k for k, i in found.items()}

    async def _on_disc(self, event):
        log.warning("Radio disconnected")
        self.alive = False

    async def _on_chan(self, event):
        try:
            p = event.payload or {}
            chan = self.idx_name.get(p.get("channel_idx"))
            if chan is None:
                return
            raw = str(p.get("text", ""))
            name, sep, body = raw.partition(": ")
            if not sep or len(name) > 40:
                name, body = "", raw
            hops = p.get("path_len")
            await self.on_message(Incoming(body.strip(), False, name, "",
                                           hops if isinstance(hops, int) and hops < 64 else None,
                                           p.get("SNR"), chan))
        except Exception:
            log.exception("channel handler")

    async def _on_dm(self, event):
        try:
            p = event.payload or {}
            prefix = str(p.get("pubkey_prefix", ""))
            c = self.find_contact(key=prefix)
            if c is None:
                await self._load_contacts()
                c = self.find_contact(key=prefix)
            await self.on_message(Incoming(str(p.get("text", "")).strip(), True,
                                           (c or {}).get("adv_name", ""), (c or {}).get("public_key", prefix),
                                           None, p.get("SNR")))
        except Exception:
            log.exception("DM handler")

    def find_contact(self, name=None, key=None):
        contacts = getattr(self.mc, "contacts", None) or {}
        for k, c in contacts.items():
            pk = str(c.get("public_key") or k).lower()
            if key and pk.startswith(key.lower()[:12]):
                return {**c, "public_key": pk}
            if name and str(c.get("adv_name", "")).strip().lower() == name.strip().lower():
                return {**c, "public_key": pk}
        return None

    async def send_channel(self, channel, text):
        idx = self.chans.get(chan_key(channel))
        if idx is None:
            log.warning("TX to unknown channel %s", channel)
            return False
        r = await self.mc.commands.send_chan_msg(idx, text)
        return r is not None and r.type != self.ET.ERROR

    async def send_dm(self, key, text):
        c = self.find_contact(key=key)
        if c is None:
            await self._load_contacts()
            c = self.find_contact(key=key)
        if c is None:
            log.warning("DM failed: %s not in contacts", key[:12])
            return False
        r = await self.mc.commands.send_msg(c, text)
        return r is not None and r.type != self.ET.ERROR

    async def status_extra(self):
        try:
            r = await self.mc.commands.get_bat()
            lvl = (r.payload or {}).get("level")
            return f" | batt {lvl / 1000:.2f}V" if lvl else ""
        except Exception:
            return ""

