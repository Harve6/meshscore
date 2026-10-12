"""Re-record the league-API fixtures used by the real-data suites.

    python tests/record_fixtures.py            # all suites
    python tests/record_fixtures.py nfl phases # just these

Needs internet. Each suite runs against the live APIs while every response is saved to tests/fixtures/<suite>.json.gz
(together with the moment of recording - replay freezes the bot's clock there). A suite that fails while recording does
not overwrite its fixture. Review `git diff --stat` and commit the result.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = ["offline", "postseason", "phases", "nfl"]


def main(argv):
    names = argv or ALL
    fixtures = os.path.join(HERE, "fixtures")
    rc = 0
    for name in names:
        path = os.path.join(fixtures, f"{name}.json.gz")
        backup = None
        if os.path.exists(path):
            backup = tempfile.mktemp(suffix=".json.gz")
            shutil.copy(path, backup)
            os.remove(path)                    # a recording run must not replay the old file
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MESHSCORE_FIXTURES": "record"}
        print(f"== recording {name} ...", flush=True)
        res = subprocess.run([sys.executable, os.path.join(HERE, "suites", f"suite_{name}.py")], env=env)
        if res.returncode != 0 or not os.path.exists(path):
            print(f"   {name} FAILED while recording - keeping the previous fixture")
            if backup:
                shutil.copy(backup, path)
            rc = 1
        elif backup:
            os.remove(backup)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
