"""What the game's memory says (Pokemon Red/Blue, US/EU). Addresses from the community pokered disassembly.

Everything works on an object with ``read(addr) -> int`` so it can be tested without an emulator or a ROM. Species
names are read out of the ROM itself (MonsterNames), so no copyrighted text is shipped with MeshScore.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- addresses (WRAM)
ADDR_PLAYER_NAME = 0xD158          # 11 bytes, game text
ADDR_PARTY_COUNT = 0xD163
ADDR_PARTY_MONS = 0xD16B           # 6 x 44 bytes
ADDR_PARTY_NICKS = 0xD2B5          # 6 x 11 bytes
ADDR_MAP = 0xD35E
ADDR_Y, ADDR_X = 0xD361, 0xD362
ADDR_BADGES = 0xD356               # one bit per badge
ADDR_MONEY = 0xD347                # 3 bytes BCD
ADDR_IN_BATTLE = 0xD057            # 0 none, 1 wild, 2 trainer, 0xFF lost
ADDR_DEX_OWNED = 0xD2F7            # 19 bytes of bits
ADDR_DEX_SEEN = 0xD30A
MON_SIZE, NICK_SIZE = 44, 11
OFF_SPECIES, OFF_HP, OFF_LEVEL, OFF_MAXHP = 0, 1, 0x21, 0x22

# ---- ROM: MonsterNames, 190 entries of 10 bytes, internal-index order (1 = Rhydon)
ROM_MONSTER_NAMES = 0x1C21E
MONSTER_COUNT = 190

_CHARS = {0x7F: " ", 0xE3: "-", 0xE8: ".", 0xE7: "!", 0xE6: "?", 0xE0: "'", 0xF4: ",", 0xBA: "e", 0xEF: "M", 0xF5: "F",
          0x9A: "(", 0x9B: ")", 0x9C: ":", 0x9D: ";", 0x9E: "[", 0x9F: "]", 0xE1: "PK", 0xE2: "MN"}
_CHARS.update({0x80 + i: chr(65 + i) for i in range(26)})
_CHARS.update({0xA0 + i: chr(97 + i) for i in range(26)})
_CHARS.update({0xF6 + i: str(i) for i in range(10)})


def decode_text(raw) -> str:
    """Game text -> str (stops at the 0x50 terminator)."""
    out = []
    for b in raw:
        if b == 0x50:
            break
        out.append(_CHARS.get(b, ""))
    return "".join(out).strip()


def species_names_from_rom(rom: bytes) -> list[str]:
    """Internal species index (1-based) -> name. Index 0 / unused slots become '?'."""
    names = ["?"]
    for i in range(MONSTER_COUNT):
        chunk = rom[ROM_MONSTER_NAMES + i * 10: ROM_MONSTER_NAMES + (i + 1) * 10]
        n = decode_text(chunk)
        names.append(n if n and n.upper() != "MISSINGNO." else "?")
    return names


# ---- maps (the ones worth naming; the rest read "Area 0x..")
_MAPS = {0: "Pallet Town", 1: "Viridian City", 2: "Pewter City", 3: "Cerulean City", 4: "Lavender Town",
         5: "Vermilion City", 6: "Celadon City", 7: "Fuchsia City", 8: "Cinnabar Island", 9: "Indigo Plateau",
         10: "Saffron City",
         0x25: "Red's House 1F", 0x26: "Red's Room", 0x27: "Blue's House", 0x28: "Oak's Lab",
         0x29: "Viridian Pokecenter", 0x2A: "Viridian Mart", 0x2B: "Viridian School", 0x2C: "Viridian House",
         0x2D: "Viridian Gym", 0x2E: "Diglett's Cave", 0x2F: "Forest North Gate", 0x30: "Trade House",
         0x31: "Route 2 Gate", 0x32: "Forest South Gate", 0x33: "Viridian Forest", 0x34: "Pewter Museum 1F",
         0x35: "Pewter Museum 2F", 0x36: "Pewter Gym", 0x37: "Pewter House", 0x38: "Pewter Mart",
         0x39: "Pewter House", 0x3A: "Pewter Pokecenter", 0x3B: "Mt. Moon 1F", 0x3C: "Mt. Moon B1F",
         0x3D: "Mt. Moon B2F"}
_MAPS.update({0x0C + n: f"Route {n + 1}" for n in range(25)})


def map_name(map_id: int) -> str:
    return _MAPS.get(map_id, f"Area {map_id:02X}")


@dataclass
class Mon:
    name: str
    species: str
    level: int
    hp: int
    max_hp: int


@dataclass
class Status:
    started: bool = False           # the game proper is under way (not the title screen / intro)
    player: str = ""
    map_id: int = 0
    map_name: str = ""
    x: int = 0
    y: int = 0
    battle: int = 0                 # 0 none, 1 wild, 2 trainer, -1 lost
    badges: int = 0
    money: int = 0
    owned: int = 0
    seen: int = 0
    party: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["party"] = [m.__dict__ for m in self.party]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Status":
        s = Status(**{k: v for k, v in d.items() if k != "party"})
        s.party = [Mon(**m) for m in d.get("party", [])]
        return s


def _bcd(bs) -> int:
    n = 0
    for b in bs:
        n = n * 100 + ((b >> 4) & 0xF) * 10 + (b & 0xF)
    return n


def _bits(bs) -> int:
    return sum(bin(b).count("1") for b in bs)


def read_status(mem, species_names: list[str]) -> Status:
    """`mem` has read(addr) -> int."""
    rd = mem.read
    name = decode_text([rd(ADDR_PLAYER_NAME + i) for i in range(11)])
    count = min(rd(ADDR_PARTY_COUNT), 6)
    party = []
    for i in range(count):
        base = ADDR_PARTY_MONS + i * MON_SIZE
        sp = rd(base + OFF_SPECIES)
        nick = decode_text([rd(ADDR_PARTY_NICKS + i * NICK_SIZE + j) for j in range(NICK_SIZE)])
        sp_name = species_names[sp] if 0 < sp < len(species_names) else "?"
        party.append(Mon(nick or sp_name, sp_name, rd(base + OFF_LEVEL),
                         rd(base + OFF_HP) * 256 + rd(base + OFF_HP + 1),
                         rd(base + OFF_MAXHP) * 256 + rd(base + OFF_MAXHP + 1)))
    battle = rd(ADDR_IN_BATTLE)
    mid = rd(ADDR_MAP)
    return Status(
        started=bool(mid or rd(ADDR_X) or rd(ADDR_Y) or count), player=name, map_id=mid, map_name=map_name(mid),
        x=rd(ADDR_X), y=rd(ADDR_Y),
        battle=-1 if battle == 0xFF else battle if battle in (0, 1, 2) else 0,
        badges=_bits([rd(ADDR_BADGES)]), money=_bcd([rd(ADDR_MONEY + i) for i in range(3)]),
        owned=_bits([rd(ADDR_DEX_OWNED + i) for i in range(19)]), seen=_bits([rd(ADDR_DEX_SEEN + i) for i in range(19)]),
        party=party)


def diff_events(old: Status | None, new: Status) -> list[tuple[str, str]]:
    """What happened between two statuses, as [(kind, text)] - the things worth a radio message. Nothing is reported
    on the first sighting (old is None) or before the game has really started."""
    if old is None or not new.started or not old.started:
        return []
    ev = []
    if new.badges > old.badges:
        ev.append(("badge", f"🏅 Badge {new.badges}/8!"))
    if new.owned > old.owned:
        ev.append(("caught", f"📕 Pokedex: {new.owned} caught"))
    for i, m in enumerate(new.party[: len(old.party)]):
        o = old.party[i]
        if m.species != o.species and m.level >= o.level:
            ev.append(("evolve", f"✨ {o.name} evolved into {m.species}!"))
        elif m.level > o.level:
            ev.append(("levelup", f"⬆ {m.name} is now L{m.level}!"))
    if len(new.party) > len(old.party) and not new.battle and new.owned <= old.owned:
        ev.append(("caught", f"➕ {new.party[-1].name} joined the party"))
    if new.map_id != old.map_id and not new.battle:
        ev.append(("map", f"📍 {new.map_name}"))
    if new.battle > 0 and old.battle <= 0:
        lead = next((m for m in new.party if m.hp > 0), None)
        who = "Trainer battle!" if new.battle == 2 else "A wild Pokemon appeared!"
        ev.append(("battle", f"⚔ {who}" + (f" {lead.name} L{lead.level} {lead.hp}/{lead.max_hp}" if lead else "")))
    if old.party and any(m.hp > 0 for m in old.party) and new.party and all(m.hp == 0 for m in new.party):
        ev.append(("faint", "💀 The whole party fainted..."))
    return ev


def summary(st: Status) -> str:
    """One status line (no tag) for !where."""
    if not st.started:
        return "Game not started yet (title screen / intro)"
    lead = next((m for m in st.party if m.hp > 0), st.party[0] if st.party else None)
    mon = f" | {lead.name} L{lead.level} {lead.hp}/{lead.max_hp}" if lead else ""
    fight = " | IN BATTLE" if st.battle > 0 else ""
    return f"{st.player} @ {st.map_name}{mon} | badges {st.badges}/8 | dex {st.owned}{fight}"
