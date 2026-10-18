"""Phone / chat alerts: sent when the state changes, not on every check."""
import os

import pytest

from meshscore import cli, config, ops
from meshscore.core import CFG

NOW = 1_800_000_000.0


class Rec:
    """Stands in for requests.post."""
    def __init__(self, status=200, boom=False):
        self.calls, self.status, self.boom = [], status, boom

    def __call__(self, url, **kw):
        if self.boom:
            raise OSError("no network")
        self.calls.append((url, kw))
        return type("R", (), {"status_code": self.status})()


def bad():
    return [ops.Check("radio link", "fail", "down since 1 min ago: unplug the radio")]


def good():
    return [ops.Check("radio link", "ok", "connected")]


ALERTS = {"ntfy_topic": "mine-abc", "ntfy_server": "https://ntfy.example", "discord_webhook": "https://discord.example/hook",
          "repeat_hours": 6}


def test_notify_posts_to_ntfy_and_discord():
    post = Rec()
    assert ops.notify(ALERTS, "T", "hello", urgent=True, post=post) == []
    (u1, k1), (u2, k2) = post.calls
    assert u1 == "https://ntfy.example/mine-abc" and k1["data"] == b"hello" and k1["headers"]["Priority"] == "high"
    assert u2 == "https://discord.example/hook" and "hello" in k2["json"]["content"]
    assert ops.notify({}, "T", "x", post=post) == [] and len(post.calls) == 2          # nothing configured: nothing sent
    assert ops.notify(ALERTS, "T", "x", post=Rec(status=500))[0].startswith("ntfy: HTTP 500")
    assert "no network" in ops.notify(ALERTS, "T", "x", post=Rec(boom=True))[0]


def test_only_changes_are_announced(tmp_path):
    d, post = str(tmp_path), Rec()
    assert ops.health_notify(d, ALERTS, good(), NOW, post) == "" and not post.calls         # all fine: silence
    m = ops.health_notify(d, ALERTS, bad(), NOW + 60, post)
    assert m.startswith("MeshScore needs a hand: radio link") and len(post.calls) == 2       # ntfy + discord
    n = len(post.calls)
    assert ops.health_notify(d, ALERTS, bad(), NOW + 900, post) == "" and len(post.calls) == n   # still broken: quiet
    again = ops.health_notify(d, ALERTS, bad(), NOW + 60 + 6 * 3600 + 5, post)
    assert again.startswith("Still broken (6 h") and len(post.calls) == n + 2                 # a reminder after 6 hours
    back = ops.health_notify(d, ALERTS, good(), NOW + 7 * 3600, post)
    assert back.startswith("MeshScore is back to normal (was down 6 h")
    assert ops.health_notify(d, ALERTS, good(), NOW + 8 * 3600, post) == ""


def test_a_failed_send_is_retried_next_time(tmp_path):
    d = str(tmp_path)
    m = ops.health_notify(d, ALERTS, bad(), NOW, Rec(boom=True))
    assert "could not send" in m
    ok = Rec()
    m2 = ops.health_notify(d, ALERTS, bad(), NOW + 60, ok)                                     # next check: tries again
    assert m2.startswith("Still broken") and ok.calls


def test_cli_test_alert_and_notify(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config.reset()
    (tmp_path / "meshscore.toml").write_text(
        '[radio]\ntransport = "serial"\nport = "COM3"\n[alerts]\nntfy_topic = "t1"\n'
        '[bots.x]\ntype = "nhl"\nteam = "TOR"\nchannel = "#l"\n')
    sent = Rec()
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: sent(url, **kw))
    assert cli.cmd_health(cli.build_parser().parse_args(["health", "--test-alert"])) == 0
    assert sent.calls[0][0] == "https://ntfy.sh/t1" and "Test alert sent" in capsys.readouterr().out
    monkeypatch.setattr(ops, "bot_running", lambda port: False)                                # the bot "is not running"
    args = cli.build_parser().parse_args(["health", "--notify", "--quiet"])
    assert cli.cmd_health(args) == 1
    assert "needs a hand" in capsys.readouterr().out and os.path.exists("health-state.json")
    assert cli.cmd_health(args) == 1 and capsys.readouterr().out == ""                         # the second check is silent
    config.reset()
    (tmp_path / "meshscore.toml").write_text('[radio]\ntransport = "serial"\nport = "COM3"\n[bots.x]\ntype = "nhl"\nteam = "TOR"\nchannel = "#l"\n')
    assert cli.cmd_health(cli.build_parser().parse_args(["health", "--test-alert"])) == 1      # nothing configured
    assert "No alert target" in capsys.readouterr().out


def test_alerts_section_is_validated(tmp_path):
    with pytest.raises(config.ConfigError, match="no setting called 'nope'"):
        config.parse('[alerts]\nnope = 1\n')
    assert config.parse('[alerts]\nntfy_topic = "z"\n')["alerts"]["ntfy_topic"] == "z"
    assert CFG["alerts"]["ntfy_server"] == "https://ntfy.sh"
