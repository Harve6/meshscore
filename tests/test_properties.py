"""Property-based tests (hypothesis): invariants that must hold for *any* input, not just the examples we thought of."""
import random
import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meshscore import config
from meshscore.config import ConfigError, parse, render
from meshscore.core import CFG, fit, fits, fmt_uptime, pack
from meshscore.sameair import SameAir, kind_of, letters, looks_like_bot, norm, score_key

SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
text = st.text(max_size=300)      # includes emoji, combining marks, control characters, lone surrogates are excluded by hypothesis


@pytest.fixture(autouse=True)
def defaults():
    config.reset()
    yield
    config.reset()


# ---------------------------------------------------------------- fitting text into a MeshCore message
@SETTINGS
@given(text, st.integers(min_value=4, max_value=200))
def test_fit_never_exceeds_the_limit_and_stays_valid_utf8(t, limit):
    out = fit(t, limit)
    assert len(out.encode("utf-8")) <= limit
    out.encode("utf-8")                                   # no lone surrogates / broken sequences
    if fits(t, limit):
        assert out == t                                   # short text is untouched
    else:
        assert out.endswith("...")


@SETTINGS
@given(st.text(min_size=1, max_size=20), st.lists(st.text(min_size=1, max_size=60), max_size=14), st.integers(1, 4))
def test_pack_every_message_fits_and_keeps_order(header, items, max_msgs):
    msgs = pack(header, items, max_msgs=max_msgs)
    assert len(msgs) <= max_msgs
    assert all(fits(m) for m in msgs), [m for m in msgs if not fits(m)]
    assert all(m.startswith(header) for m in msgs)
    # items that survive keep their order (an item can be trimmed or dropped, never reordered)
    seen, last = " | ".join(msgs), -1
    for it in items:
        k = seen.find(it, last + 1)
        if k >= 0:
            last = k


@SETTINGS
@given(st.lists(st.text(alphabet="abcdefg 123", min_size=1, max_size=12), min_size=1, max_size=6))
def test_pack_uses_one_message_when_everything_fits(items):
    header = "[X]"
    one = f"{header} {' | '.join(items)}"
    if fits(one):
        assert pack(header, items) == [one]


@given(st.integers(min_value=0, max_value=10 ** 7))
def test_uptime_format_is_short(sec):
    out = fmt_uptime(sec)
    assert re.fullmatch(r"\d+d\d+h|\d+h\d+m|\d+m", out)


# ---------------------------------------------------------------- same-air recognition
@SETTINGS
@given(text)
def test_signature_functions_never_raise(t):
    kind_of(t), score_key(t), norm(t), letters(t), looks_like_bot(t)


@SETTINGS
@given(text, st.sampled_from(["#a", "#b"]))
def test_a_heard_post_is_a_duplicate_of_itself_and_only_on_its_channel(t, ch):
    t = t.strip()
    if not t:
        return
    sa = SameAir({"enabled": True, "window_seconds": 180}, rng=random.Random(0), clock=lambda: 100.0)
    sa.hear(ch, t, "Other")
    other = "#b" if ch == "#a" else "#a"
    # same alert text heard on the same channel is a duplicate whenever it has a score or any word characters
    if score_key(t) is not None or norm(t):
        assert sa.is_duplicate(ch, t)
        assert not sa.is_duplicate(other, t)


@SETTINGS
@given(st.integers(0, 6), st.floats(0, 20), st.floats(0, 5), st.integers(0, 3))
def test_delay_is_bounded_by_the_line_up(rank, slot, jitter, peers):
    sa = SameAir({"enabled": True, "slot_seconds": slot, "jitter_seconds": jitter, "rank": rank,
                  "peers": [f"P{i}" for i in range(peers)]}, rng=random.Random(1))
    d = sa.delay()
    if peers:
        assert 0 <= d <= rank * slot + jitter + 1e-9
    else:
        assert d == 0


# ---------------------------------------------------------------- configuration
@SETTINGS
@given(text)
def test_parse_only_ever_raises_config_error(t):
    try:
        parse(t)
    except ConfigError:
        pass


ident = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1, max_size=12)
value = st.text(max_size=40)


@SETTINGS
@given(ident, ident, value, value)
def test_rendered_config_always_parses_back(name, team, label, tz):
    """Whatever strings a user types into the wizard (quotes, backslashes, newlines, unicode), the file we write must
    be valid TOML that says the same thing."""
    bots = {name: {"type": "nhl", "team": team, "label": label, "channel": "#" + name}}
    parsed = parse(render({"transport": "serial", "port": "COM3"}, bots, tz))
    assert parsed["modules"][name]["label"] == label and parsed["modules"][name]["team"] == team
    if tz:
        assert parsed["timezone"] == tz


def test_defaults_survive_reset():
    CFG["send_gap"] = 99
    config.reset()
    assert CFG["send_gap"] == 6 and CFG["modules"] == {}
