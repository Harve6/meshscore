"""The real-data suites.

Each suite in tests/suites/ replays *real* game data (fetched live from the league APIs - past games are pinned by id
or date, so the suites do not depend on what is being played today) through the bot against a fake radio, and checks
the exact messages that would go on the air.

They need internet access, and the free league APIs occasionally return an empty page, so each suite gets one retry.
CI runs the offline tests only (`pytest -m "not network"`); run everything locally with `pytest`.
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "suites")
SUITES = ["offline", "postseason", "phases", "nfl"]


def run_suite(name):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, os.path.join(HERE, f"suite_{name}.py")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, timeout=900)


@pytest.mark.network
@pytest.mark.parametrize("name", SUITES)
def test_suite(name):
    res = run_suite(name)
    if res.returncode != 0:                      # transient empty API responses: one retry
        res = run_suite(name)
    tail = "\n".join((res.stdout + res.stderr).splitlines()[-25:])
    assert res.returncode == 0 and "ALL OK" in res.stdout, f"suite_{name}.py failed:\n{tail}"
