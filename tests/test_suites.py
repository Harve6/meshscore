"""The real-data suites (tests/suites/suite_*.py).

Each suite replays *real* game data through the bot against a fake radio and checks the exact messages that would go
on the air. The league responses were recorded once from the live APIs into tests/fixtures/*.json.gz, so:

* `test_suite_replay`  runs fully offline and deterministically (this is what CI runs on every push);
* `test_suite_live`    talks to the real APIs - a canary for "did a league change its feed?" (scheduled weekly in
                       CI, marked `network`; the free APIs occasionally return an empty page, so one retry).

Re-record the fixtures with `python tests/record_fixtures.py` after a feed change, then commit them.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suites")
SUITES = ["offline", "postseason", "phases", "nfl"]


def run_suite(name, mode):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MESHSCORE_FIXTURES": mode}
    return subprocess.run([sys.executable, os.path.join(HERE, f"suite_{name}.py")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=900)


def check(name, res):
    tail = "\n".join((res.stdout + res.stderr).splitlines()[-25:])
    misses = [ln for ln in res.stderr.splitlines() if ln.startswith("FIXTURE MISS")]
    assert not misses, f"suite_{name}.py asked for data that is not in its fixture (re-record?):\n" + "\n".join(misses[:5])
    assert res.returncode == 0 and "ALL OK" in res.stdout, f"suite_{name}.py failed:\n{tail}"


@pytest.mark.parametrize("name", SUITES)
def test_suite_replay(name):
    check(name, run_suite(name, "replay"))


@pytest.mark.network
@pytest.mark.parametrize("name", SUITES)
def test_suite_live(name):
    res = run_suite(name, "live")
    if res.returncode != 0:                      # transient empty API responses: one retry
        res = run_suite(name, "live")
    check(name, res)
