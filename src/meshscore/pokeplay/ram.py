"""What the game's memory says (Pokemon Red / Blue / Yellow, US/EU). Addresses from the community pokered / pokeyellow
disassemblies, checked against the real ROMs (map, money, party species/HP/level, Pokedex).

Everything works on an object with ``read(addr) -> int`` so it can be tested without an emulator or a ROM. Species
names are read out of the ROM itself (MonsterNames), so no copyrighted text is shipped with MeshScore.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Layout:
    """Where things live in WRAM for one version of the game."""
    name: str
    player_name: int
    party_count: int
    party_mons: int            # 6 x 44 bytes
    party_nicks: int           # 6 x 11 bytes
    map_id: int
    y: int
    x: int
    badges: int                # one bit per badge
    money: int                 # 3 bytes BCD
    in_battle: int             # 0 none, 1 wild, 2 trainer, 0xFF lost
    dex_owned: int             # 19 bytes of bits
    dex_seen: int


RED_BLUE = Layout("red/blue", player_name=0xD158, party_count=0xD163, party_mons=0xD16B, party_nicks=0xD2B5,
                  map_id=0xD35E, y=0xD361, x=0xD362, badges=0xD356, money=0xD347, in_battle=0xD057,
                  dex_owned=0xD2F7, dex_seen=0xD30A)
# Yellow's WRAM is the same table shifted down one byte in this region (verified: map, money, party on the real ROM).
YELLOW = Layout("yellow", player_name=0xD157, party_count=0xD162, party_mons=0xD16A, party_nicks=0xD2B4,
                map_id=0xD35D, y=0xD360, x=0xD361, badges=0xD355, money=0xD346, in_battle=0xD056,
                dex_owned=0xD2F6, dex_seen=0xD309)

MON_SIZE, NICK_SIZE = 44, 11
OFF_SPECIES, OFF_HP, OFF_LEVEL, OFF_MAXHP = 0, 1, 0x21, 0x22


def layout_for_title(title: str) -> Layout:
    """The cartridge header title ('POKEMON RED', 'POKEMON YELLOW' ...) -> memory layout."""
    return YELLOW if "YELLOW" in (title or "").upper() else RED_BLUE


# ---- ROM: MonsterNames, 190 entries of 10 bytes, internal-index order (1 = Rhydon)
ROM_MONSTER_NAMES = 0x1C21E          # Red / Blue (US/EU); Yellow keeps it elsewhere - found by searching
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


def _enc(s: str) -> bytes:
    return bytes(0x80 + ord(c) - 65 for c in s.upper() if "A" <= c.upper() <= "Z")


def find_monster_names(rom: bytes) -> int:
    """Offset of the species name table in a ROM image (Rhydon, Kangaskhan, ...), or the Red/Blue default."""
    key = _enc("RHYDON") + b"\x50" * 4 + _enc("KANGASKHAN")
    i = rom.find(key)
    return i if i >= 0 else ROM_MONSTER_NAMES


def species_names_from_rom(rom: bytes, offset: int | None = None) -> list[str]:
    """Internal species index (1-based) -> name. Index 0 / unused slots become '?'."""
    base = find_monster_names(rom) if offset is None else offset
    names = ["?"]
    for i in range(MONSTER_COUNT):
        chunk = rom[base + i * 10: base + (i + 1) * 10]
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


def read_status(mem, species_names: list[str], layout: Layout = RED_BLUE) -> Status:
    """`mem` has read(addr) -> int."""
    rd, L = mem.read, layout
    name = decode_text([rd(L.player_name + i) for i in range(11)])
    count = min(rd(L.party_count), 6)
    party = []
    for i in range(count):
        base = L.party_mons + i * MON_SIZE
        sp = rd(base + OFF_SPECIES)
        nick = decode_text([rd(L.party_nicks + i * NICK_SIZE + j) for j in range(NICK_SIZE)])
        sp_name = species_names[sp] if 0 < sp < len(species_names) else "?"
        party.append(Mon(nick or sp_name, sp_name, rd(base + OFF_LEVEL),
                         rd(base + OFF_HP) * 256 + rd(base + OFF_HP + 1),
                         rd(base + OFF_MAXHP) * 256 + rd(base + OFF_MAXHP + 1)))
    battle = rd(L.in_battle)
    mid = rd(L.map_id)
    return Status(
        started=bool(mid or rd(L.x) or rd(L.y) or count), player=name, map_id=mid, map_name=map_name(mid),
        x=rd(L.x), y=rd(L.y),
        battle=-1 if battle == 0xFF else battle if battle in (0, 1, 2) else 0,
        badges=_bits([rd(L.badges)]), money=_bcd([rd(L.money + i) for i in range(3)]),
        owned=_bits([rd(L.dex_owned + i) for i in range(19)]), seen=_bits([rd(L.dex_seen + i) for i in range(19)]),
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


# kept for older imports / tests: the Red/Blue addresses under their original names
ADDR_PLAYER_NAME, ADDR_PARTY_COUNT, ADDR_PARTY_MONS = RED_BLUE.player_name, RED_BLUE.party_count, RED_BLUE.party_mons
ADDR_PARTY_NICKS, ADDR_MAP, ADDR_Y, ADDR_X = RED_BLUE.party_nicks, RED_BLUE.map_id, RED_BLUE.y, RED_BLUE.x
ADDR_BADGES, ADDR_MONEY, ADDR_IN_BATTLE = RED_BLUE.badges, RED_BLUE.money, RED_BLUE.in_battle
ADDR_DEX_OWNED, ADDR_DEX_SEEN = RED_BLUE.dex_owned, RED_BLUE.dex_seen
