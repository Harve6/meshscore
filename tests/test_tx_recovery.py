"""A radio that is connected but refuses every message (hung, replugged, cold) must trigger a reconnect, not a silent day."""
import asyncio

import pytest

from meshscore import config
from meshscore.cli import build_hub
from meshscore.core import CFG
from meshscore.radio import Transport
import meshscore.hub as hubmod


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="")
    CFG["modules"] = {"chess": {"enabled": True, "type": "chess", "channel": "#chess", "puzzle": False}}
    monkeypatch.setattr(hubmod, "TX_RETRY_WAIT", 0.001)
    yield
    config.reset()


class Dead(Transport):
    name = "dead"

    def __init__(self, ok_after=None):
        self.alive = True
        self.sent, self.dm_attempts, self.chan_attempts = [], 0, 0
        self.ok_after = ok_after

    async def send_channel(self, channel, text):
        self.chan_attempts += 1
        if self.ok_after is not None and self.chan_attempts > self.ok_after:
            self.sent.append(text)
            return True
        return False

    async def send_dm(self, key, text):
        self.dm_attempts += 1
        return False


def pump(hub, texts, kind="chan"):
    async def go():
        w = asyncio.create_task(hub.tx_worker())
        for t in texts:
            hub.say_chan("#chess", t) if kind == "chan" else hub.say_dm("ab" * 16, t)
        await asyncio.sleep(0.4)
        w.cancel()
    asyncio.run(go())


def test_three_failed_posts_in_a_row_drop_the_link_so_it_reconnects():
    t = Dead()
    hub = build_hub(t)
    pump(hub, ["one", "two", "three"])
    assert t.alive is False                       # the supervisor sees this and reconnects
    assert hub.tx_fail_streak == 0                # and the count starts again for the new link


def test_one_success_resets_the_count_and_a_few_failures_do_not_reset_anything():
    t = Dead(ok_after=2)                          # 2 attempts fail (one message + its retry), then the radio works
    hub = build_hub(t)
    pump(hub, ["one", "two", "three"])
    assert t.alive is True and t.sent == ["two", "three"]


def test_dms_to_unknown_contacts_are_not_a_dead_radio():
    t = Dead()
    hub = build_hub(t)
    pump(hub, ["a", "b", "c", "d"], kind="dm")
    assert t.alive is True and t.dm_attempts >= 4
