"""The radio's join links for a bot's web page: its channel and its own contact card.

Shared by the chess and Pokemon bots. The page shows both as QR codes: the channel one is how people start playing,
the contact one lets the bot's DM acks reach them (a DM from a node that is not in your contacts is dropped).
"""
from __future__ import annotations

import logging

log = logging.getLogger("meshscore")

_svg_cache: dict = {}


def qr_svg(text: str) -> str:
    """An inline SVG QR code for `text` (error correction M, 2-module quiet zone), or "" when `segno` is not
    installed. Generated here rather than in the visitor's browser: it is identical for everybody, scales without
    blurring, and can be checked with an independent decoder."""
    if not text:
        return ""
    if text in _svg_cache:
        return _svg_cache[text]
    try:
        import segno
        m = segno.make(text, error="m").matrix
    except Exception:
        return ""
    n, b, parts = len(m), 2, []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                s = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{s + b} {y + b}h{x - s}v1h-{x - s}z")
            else:
                x += 1
    size = n + 2 * b
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
           f'<rect width="{size}" height="{size}" fill="#fff"/><path fill="#000" d="{"".join(parts)}"/></svg>')
    _svg_cache[text] = svg
    return svg


class CardsMixin:
    """Needs self.cfg, self.hub, self.pub (a SitePublisher), self.state, self.channel. Call `_cards_init()` once."""

    def _cards_init(self):
        d = self.state.data
        d.setdefault("channel_card", None)         # {"name", "uri"}: how to join the channel
        d.setdefault("contact", None)              # {"name", "uri"}: the radio's contact card
        self._contact_tried, self._contact_ok = 0.0, False

    async def _refresh_cards(self, now):
        """Read the radio's channel link and contact card once per start (retry every 10 min while unknown)."""
        if not self.cfg.get("show_contact", True) or not self.pub.enabled or self._contact_ok \
                or now - self._contact_tried < 600:
            return
        if not getattr(self.hub.t, "alive", False):              # the radio is not connected yet: try again next poll
            return
        self._contact_tried = now
        try:
            uri = await self.hub.t.own_contact_uri()
        except Exception as e:
            log.debug("contact card unavailable: %s", e)
            return
        try:
            link = await self.hub.t.own_contact_link()
        except Exception as e:
            log.debug("contact link unavailable: %s", e)
            link = ""
        try:
            curi = await self.hub.t.channel_uri(self.channel)
        except Exception as e:
            log.debug("channel link unavailable: %s", e)
            curi = ""
        if curi.startswith("meshcore://"):
            self.state.data["channel_card"] = {"name": self.channel, "uri": curi}
        if uri and uri.startswith("meshcore://"):
            card = {"name": getattr(self.hub.t, "own_name", "") or "", "uri": uri}
            if link.startswith("meshcore://contact/add"):
                card["link"] = link
                card["public_key"] = link.split("public_key=", 1)[1].split("&", 1)[0]
            self.state.data["contact"] = card
        if uri and curi:
            self._contact_ok = True
        self.state.save()

    def _cards_for_site(self) -> dict:
        d = self.state.data
        show = self.cfg.get("show_contact", True)
        chan = {"name": self.channel}
        if show and d.get("channel_card"):
            chan = {"name": self.channel, **d["channel_card"]}
            chan["svg"] = qr_svg(chan.get("uri", ""))
        contact = d.get("contact") if show else None
        if contact:
            contact = {**contact, "svg": qr_svg(contact.get("link") or contact.get("uri", ""))}
        return {"contact": contact, "channel": chan}
