"""!help on the chess and Pokemon bots ends with their own web page."""
import asyncio

import pytest

from meshscore import config
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Incoming

from test_chess import Radio


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    yield
    config.reset()


def help_for(module, channel, cfg):
    CFG["modules"] = {module: {"enabled": True, "channel": "#" + channel, **cfg}}
    hub = build_hub(Radio())
    mod = hub.modules[module]
    return asyncio.run(mod.commands["!help"](Incoming("!help", False, "Ann", "", 1, 5.0, channel), ""))


def test_chess_help_has_its_link_and_fits_a_packet():
    out = help_for("chess", "chess", {"type": "chess", "puzzle": False, "site_url": "https://meshscore-chess.harve6.workers.dev/"})
    assert len(out) == 2 and out[0].startswith("[CHESS BOT]") and "!move" in out[0]
    assert out[1] == "[CHESS] Mesh Chess: https://meshscore-chess.harve6.workers.dev/"
    assert all(len(x.encode()) <= 130 for x in out)


def test_pokemon_help_has_its_link_and_all_the_buttons():
    out = help_for("pokemon", "pokemon", {"type": "pokemon", "emulator": "fake", "site_url": "https://meshscore-board.harve6.workers.dev/",
                                           "roms": {"red": "r", "blue": "b"}})
    assert out[0].startswith("[POKEMON BOT]") and "!select" in out[0] and "!game" in out[0]
    assert out[1] == "[POKEMON] Mesh Plays Pokemon: https://meshscore-board.harve6.workers.dev/"
    assert all(len(x.encode()) <= 130 for x in out)


def test_no_link_line_when_no_site_url():
    out = help_for("chess", "chess", {"type": "chess", "puzzle": False})
    assert len(out) == 1
