"""The Game Boy: PyBoy for real, ``FakeGameBoy`` for tests.

Both expose the same small surface:  ``press(button, steps)``, ``png()``, ``read(addr)``, ``save(path)``, ``close()``,
``species_names``.  PyBoy is imported lazily, so MeshScore without the ``pokemon`` extra still starts.
"""
from __future__ import annotations

import io
import os

BUTTONS = ("up", "down", "left", "right", "a", "b", "start", "select")
DIRECTIONS = ("up", "down", "left", "right")
ALIASES = {"u": "up", "d": "down", "l": "left", "r": "right", "sel": "select", "back": "select", "north": "up",
           "south": "down", "west": "left", "east": "right"}

BOOT_FRAMES = 1500          # a fresh game is fast-forwarded through the logos to the title / intro
WALK_FRAMES = 16            # holding a direction this long moves one tile
TAP_FRAMES = 6
TAP_GAP = 36               # at least this many frames between repeated A / B taps
SETTLE_FRAMES = 30          # the minimum wait after an input ...
SETTLE_CAP = 360            # ... and the most we wait (6 s of game time) for text to finish printing / animations to end
STABLE_CHECKS = 4           # the picture must be unchanged for this many looks (9 frames apart) to count as "settled"


def norm_button(word: str) -> str | None:
    w = (word or "").strip().lower().lstrip("!")
    w = ALIASES.get(w, w)
    return w if w in BUTTONS else None


class GameBoy:
    """Pokemon Red/Blue running in PyBoy, headless and as fast as the CPU allows, only moving when told to."""

    def __init__(self, rom_path: str, state_path: str = "", boot_frames: int = BOOT_FRAMES):
        from pyboy import PyBoy                                   # optional dependency (pip install pyboy pillow)
        from . import ram
        if not os.path.isfile(rom_path):
            raise FileNotFoundError(f"ROM not found: {rom_path}")
        self.state_path = state_path
        with open(rom_path, "rb") as f:
            self.species_names = ram.species_names_from_rom(f.read())
        self.pb = PyBoy(rom_path, window="null", sound_emulated=False)
        self.pb.set_emulation_speed(0)
        self.title = str(getattr(self.pb, "cartridge_title", "") or "")
        self.layout = ram.layout_for_title(self.title)          # Yellow keeps its variables one byte lower
        self.resumed = False
        if state_path and os.path.isfile(state_path):
            try:
                with open(state_path, "rb") as f:
                    self.pb.load_state(f)
                self.resumed = True
            except Exception:                                      # a corrupt / old state: start fresh
                self.pb.tick(boot_frames, False)
        else:
            self.pb.tick(boot_frames, False)
        self._unblank()

    def press(self, button: str, steps: int = 1):
        if button in DIRECTIONS:
            hold = WALK_FRAMES * max(1, steps) + 2
            self.pb.button(button, hold)
            self.pb.tick(hold + SETTLE_FRAMES, False)
            self._settle()
        else:
            taps = max(1, steps) if button in ("a", "b") else 1
            for i in range(taps):                                  # !a 8: several taps, each text box printed in full
                self.pb.button(button, TAP_FRAMES)
                self.pb.tick(TAP_FRAMES + (TAP_GAP if i < taps - 1 else SETTLE_FRAMES), False)
                self._settle(cap=SETTLE_CAP if i == taps - 1 else 150)
        self._unblank()

    def _picture(self) -> bytes:
        """The screen without its bottom-right corner, where the "more text" arrow blinks forever."""
        a = self.pb.screen.ndarray.copy()
        a[120:, 136:] = 0
        return a.tobytes()

    def _settle(self, cap: int = SETTLE_CAP):
        """Let the game run (no input) until the picture stops changing: a text box finishes printing, a fade or
        a walk animation ends. Without this the page showed half a sentence and people pressed again."""
        waited, last, same = 0, None, 0
        while waited < cap:
            self.pb.tick(8, False)
            self.pb.tick(1, True)
            waited += 9
            try:
                pic = self._picture()
            except Exception:
                return
            same = same + 1 if pic == last else 0
            last = pic
            if same >= STABLE_CHECKS:
                return

    def _unblank(self, tries: int = 12):
        """A screen that is one flat colour is a fade between scenes: let the game run (no input) until it is not."""
        self.pb.tick(1, True)
        for _ in range(tries):
            try:
                a = self.pb.screen.ndarray
                if not (a == a[0, 0]).all():
                    return
            except Exception:
                return
            self.pb.tick(30, False)
            self.pb.tick(1, True)

    def read(self, addr: int) -> int:
        return int(self.pb.memory[addr])

    def png(self) -> bytes:
        buf = io.BytesIO()
        self.pb.screen.image.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue()

    def save(self, path: str = ""):
        path = path or self.state_path
        if not path:
            return
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            self.pb.save_state(f)
        os.replace(tmp, path)

    def close(self):
        try:
            self.pb.stop(save=False)
        except Exception:
            pass


class FakeGameBoy:
    """A pretend Game Boy for tests: a person who walks on a grid, a few RAM cells, a 'screen' that is just bytes.
    `script` can set RAM cells as the game 'progresses': {round_number: {addr: value}}."""

    def __init__(self, script=None, state_path: str = ""):
        self.ram: dict[int, int] = {}
        self.script = script or {}
        self.presses: list[tuple[str, int]] = []
        from . import ram
        self.species_names = ["?"] + [f"MON{i}" for i in range(1, 191)]
        self.title = "POKEMON RED"
        self.layout = ram.RED_BLUE
        self.state_path = state_path
        self.saves = 0
        self.closed = False
        self.resumed = False
        if state_path and os.path.isfile(state_path):
            import json
            with open(state_path, encoding="utf-8") as f:
                d = json.load(f)
            self.ram = {int(k): v for k, v in d["ram"].items()}
            self.presses = [tuple(p) for p in d["presses"]]
            self.resumed = True

    def press(self, button: str, steps: int = 1):
        self.presses.append((button, steps))
        n = len(self.presses)
        for k, v in self.script.get(n, {}).items():
            self.ram[k] = v
        if button in DIRECTIONS:
            dx, dy = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[button]
            self.ram[0xD362] = (self.ram.get(0xD362, 5) + dx * steps) % 256
            self.ram[0xD361] = (self.ram.get(0xD361, 5) + dy * steps) % 256

    def read(self, addr: int) -> int:
        return self.ram.get(addr, 0)

    def png(self) -> bytes:
        return b"\x89PNG-fake-" + str(len(self.presses)).encode()

    def save(self, path: str = ""):
        path = path or self.state_path
        if not path:
            return
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ram": self.ram, "presses": self.presses}, f)
        self.saves += 1

    def close(self):
        self.closed = True
