"""Coverage: who reaches the bot's radio, and from how far.

Every message the radio hears (on any channel it has, bots' channels or not) says how many hops it travelled and how
strong it arrived. This bot never posts to a channel; it keeps those numbers and shows them on the shared web board
(a hop histogram, the signal-strength spread, recent senders, a 14-day activity strip) and answers `!coverage`.
Node names are hidden on the page unless `show_names = true`. See docs/daily-games.md.
"""
from __future__ import annotations

import logging
import time

from ..core import fit
from ..dailyplay.base import DailyModule

log = logging.getLogger("meshscore")

KEEP_DAYS = 30
SNR_SAMPLES = 600
SENDERS_KEPT = 400


def hop_bucket(h) -> str:
    return "?" if h is None else str(h) if h < 3 else "3+"


def percentile(values, q: float):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


class CoverageModule(DailyModule):
    label = "Coverage"
    json_name = "coverage"
    board_title = "Mesh Coverage"
    kinds = {"max": set(), "min": set(), "off": set()}         # it never posts to a channel
    min_blurb = "nothing (it only listens)"
    bare_command = "!coverage"

    def __init__(self, hub, name):
        super().__init__(hub, name)
        self.show_names = bool(self.cfg.get("show_names", False))
        self.blurb = "coverage stats"
        d = self.state.data
        d.setdefault("senders", {})        # name -> {"id","n","first","last","hops","snr","last_hops"}
        d.setdefault("days", {})           # date -> {"n": messages, "hops": {bucket: n}, "senders": [names]}
        d.setdefault("snr", [])            # [(ts, snr)] newest last
        d.setdefault("next_id", 1)
        self._dirty = False

    # ---------------------------------------------------------------- listening
    def on_heard(self, inc):
        """Called by the hub for every channel message the radio hears."""
        if not inc.sender_name:
            return
        d = self.state.data
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        s = d["senders"].get(inc.sender_name)
        if s is None:
            s = d["senders"][inc.sender_name] = {"id": d["next_id"], "n": 0, "first": now, "last": now, "hops": {}, "snr": None,
                                                 "last_hops": None}
            d["next_id"] += 1
        b = hop_bucket(inc.hops)
        s["n"] += 1
        s["last"] = now
        s["last_hops"] = inc.hops
        s["hops"][b] = s["hops"].get(b, 0) + 1
        dd = d["days"].setdefault(day, {"n": 0, "hops": {}, "senders": []})
        dd["n"] += 1
        dd["hops"][b] = dd["hops"].get(b, 0) + 1
        if inc.sender_name not in dd["senders"]:
            dd["senders"].append(inc.sender_name)
        if isinstance(inc.snr, (int, float)):
            s["snr"] = max(s["snr"], inc.snr) if s["snr"] is not None else inc.snr
            d["snr"].append([int(now), float(inc.snr)])
            del d["snr"][:-SNR_SAMPLES]
        self._dirty = True

    # ---------------------------------------------------------------- the loop
    async def poll(self):
        d = self.state.data
        now = self.clock()
        cutoff_day = time.strftime("%Y-%m-%d", time.localtime(now - KEEP_DAYS * 86400))
        for k in [k for k in d["days"] if k < cutoff_day]:
            del d["days"][k]
        if len(d["senders"]) > SENDERS_KEPT:                     # forget the longest-quiet nodes
            keep = sorted(d["senders"].items(), key=lambda kv: -kv[1]["last"])[:SENDERS_KEPT]
            d["senders"] = dict(keep)
        if self._dirty:
            self.state.save()
            self._dirty = False
        await self.publish()
        await self._refresh_cards(now)
        self.mark_ok()
        return 30

    # ---------------------------------------------------------------- numbers
    def window(self, hours: float) -> dict:
        d = self.state.data
        now = self.clock()
        cutoff = now - hours * 3600
        nodes = [(n, s) for n, s in d["senders"].items() if s["last"] >= cutoff]
        snr = [v for t, v in d["snr"] if t >= cutoff]
        hops = {}
        for day, dd in d["days"].items():
            if day >= time.strftime("%Y-%m-%d", time.localtime(cutoff)):
                for b, n in dd["hops"].items():
                    hops[b] = hops.get(b, 0) + n
        msgs = sum(dd["n"] for day, dd in d["days"].items() if day >= time.strftime("%Y-%m-%d", time.localtime(cutoff)))
        return {"nodes": len(nodes), "msgs": msgs, "hops": {k: hops.get(k, 0) for k in ("0", "1", "2", "3+")},
                "snr": {"p10": percentile(snr, .1), "p50": percentile(snr, .5), "p90": percentile(snr, .9), "n": len(snr)}}

    # ---------------------------------------------------------------- commands
    def extra_commands(self):
        return {"!coverage": self.cmd_coverage}

    def help_text(self):
        return "!coverage: who the radio hears and from how far"

    def status_extra(self):
        return f" | nodes {len(self.state.data.get('senders', {}))}"

    async def cmd_coverage(self, inc, arg):
        w = self.window(24)
        if not w["msgs"]:
            return [fit(f"[{self.tag}] The radio has not heard anyone in the last 24 h")]
        h = w["hops"]
        snr = f" | SNR median {w['snr']['p50']:.0f} (weakest tenth {w['snr']['p10']:.0f})" if w["snr"]["p50"] is not None else ""
        url = f" {self.site_url}" if self.site_url else ""
        return [fit(f"[{self.tag}] 24h: {w['nodes']} nodes, {w['msgs']} msgs | hops 0:{h['0']} 1:{h['1']} 2:{h['2']} 3+:{h['3+']}{snr}{url}")]

    # ---------------------------------------------------------------- the shared web board
    def site_state(self) -> dict:
        d = self.state.data
        now = self.clock()
        recent = sorted(d["senders"].items(), key=lambda kv: -kv[1]["last"])[:12]
        days = []
        for i in range(13, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            dd = d["days"].get(day)
            days.append({"date": day, "msgs": dd["n"] if dd else 0, "nodes": len(dd["senders"]) if dd else 0})
        return {
            **self.base_state(),
            "h24": self.window(24),
            "d7": self.window(24 * 7),
            "recent": [{"who": n if self.show_names else f"Node {s['id']}", "hops": s["last_hops"], "best_snr": s["snr"],
                        "last": int(s["last"]), "messages": s["n"]} for n, s in recent],
            "days": days,
            "show_names": self.show_names,
        }
