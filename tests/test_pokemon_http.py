"""Publishing a bot's page to a small web service (the Cloudflare Worker in contrib/cloudflare-board). Offline: a local
HTTP server plays the Worker."""
import asyncio
import base64
import http.server
import json
import os
import threading

import pytest

from meshscore import config
from meshscore.chessplay.publish import HttpPublisher
from meshscore.core import CFG

from test_pokemon import Rig, run  # noqa: F401  (the harness)

PAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "meshscore", "pokeplay", "site")


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.reset()
    CFG.update(send_gap=0, command_cooldown=0, log_file="", timezone="America/Toronto")
    yield
    config.reset()


def serve():
    got = {"puts": [], "status": 200}

    class H(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            got["puts"].append({"path": self.path, "auth": self.headers.get("Authorization"), "files": body["files"]})
            self.send_response(got["status"])
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


def test_uploads_only_what_changed_and_survives_outages():
    srv, got = serve()
    try:
        now = [100.0]
        pub = HttpPublisher(f"http://127.0.0.1:{srv.server_address[1]}/", "s3cret", min_interval=5, clock=lambda: now[0],
                            template_dir=PAGE_DIR)
        assert pub.tick({"updated": 1, "round": 1}, {"screen.png": b"PNG1"}) is True
        put = got["puts"][0]
        assert put["path"] == "/api/files" and put["auth"] == "Bearer s3cret"
        assert set(put["files"]) == {"state.json", "screen.png", "index.html"}
        assert base64.b64decode(put["files"]["screen.png"]["b64"]) == b"PNG1"
        assert put["files"]["screen.png"]["type"] == "image/png"
        now[0] += 3
        assert pub.tick({"updated": 2, "round": 1}, {"screen.png": b"PNG1"}) is False       # only "updated" moved
        assert len(got["puts"]) == 1
        assert pub.tick({"updated": 3, "round": 2}, {"screen.png": b"PNG2"}) is False       # inside the interval ...
        now[0] += 10
        assert pub.tick({"updated": 4, "round": 2}, {"screen.png": b"PNG2"}) is True        # ... then sent, without index.html
        assert set(got["puts"][1]["files"]) == {"state.json", "screen.png"}
        got["status"] = 500                                                                 # the service errors: no exception
        now[0] += 10
        assert pub.tick({"updated": 5, "round": 3}, {"screen.png": b"PNG3"}) is False and pub.failures == 1
        got["status"] = 200
        now[0] += 100
        assert pub.tick({"updated": 6, "round": 3}, {"screen.png": b"PNG3"}) is True and pub.failures == 0
    finally:
        srv.shutdown()


def test_nothing_listening_or_not_configured_is_harmless():
    dead = HttpPublisher("http://127.0.0.1:9", "t", min_interval=0, timeout=1)
    assert dead.tick({"a": 1}) is False and dead.failures == 1
    assert HttpPublisher("", "").tick({"a": 1}) is False
    assert HttpPublisher("http://x", "").enabled is False             # no token: never sends


def test_the_bot_uses_the_web_service_when_site_api_is_set():
    async def go():
        srv, got = serve()
        try:
            async with Rig(site_api=f"http://127.0.0.1:{srv.server_address[1]}", site_token="tok",
                           publish_min_seconds=0) as r:
                await r.tick()
                await r.say("!up")
                await r.tick(31)
                assert got["puts"], "nothing was uploaded"
                st = json.loads(base64.b64decode(got["puts"][-1]["files"]["state.json"]["b64"]))
                assert st["round"] == 1
        finally:
            srv.shutdown()
    run(go())


def test_the_chess_bot_can_use_the_web_service_too():
    import test_chess as C

    async def go():
        srv, got = serve()
        try:
            async with C.Rig(site_api=f"http://127.0.0.1:{srv.server_address[1]}", site_token="tok",
                             publish_min_seconds=0, opponent=C.Scripted("e7e5")) as r:
                await r.tick()
                assert got["puts"], "nothing was uploaded"
                files = got["puts"][-1]["files"]
                assert {"state.json", "index.html"} <= set(files)
                page = base64.b64decode(files["index.html"]["b64"]).decode()
                assert "Mesh Chess" in page                                  # the chess page, not the Pokemon one
                st = json.loads(base64.b64decode(files["state.json"]["b64"]))
                assert st["game"]["id"] == 1 and "fen" in st["game"]
        finally:
            srv.shutdown()
    asyncio.run(go())


def test_each_page_links_to_the_other_board_when_both_bots_run_together():
    from meshscore.cli import build_hub
    from test_chess import Radio
    CFG["modules"] = {
        "chess": {"enabled": True, "type": "chess", "channel": "#chess", "puzzle": False, "site_url": "https://c.test/"},
        "pokemon": {"enabled": True, "type": "pokemon", "channel": "#pokemon", "emulator": "fake",
                    "site_url": "https://p.test/"},
    }
    hub = build_hub(Radio())
    c, p = hub.modules["chess"], hub.modules["pokemon"]
    assert c.site_state()["links"] == [{"title": "Mesh Plays Pokemon", "url": "https://p.test/"}]
    assert p.site_state()["links"] == [{"title": "Mesh Chess", "url": "https://c.test/"}]
    CFG["modules"] = {"chess": {"enabled": True, "type": "chess", "channel": "#chess", "puzzle": False}}
    assert build_hub(Radio()).modules["chess"].site_state()["links"] == []


def test_an_idle_board_still_sends_a_heartbeat_and_retries_a_reset_connection():
    import requests
    from meshscore.chessplay.publish import HttpPublisher
    srv, got = serve()
    try:
        now = [1000.0]
        pub = HttpPublisher(f"http://127.0.0.1:{srv.server_address[1]}", "t", min_interval=1, clock=lambda: now[0],
                            template_dir=PAGE_DIR, heartbeat=600)
        assert pub.tick({"updated": 1, "a": 1}) is True and len(got["puts"]) == 1
        now[0] += 300
        assert pub.tick({"updated": 2, "a": 1}) is False and len(got["puts"]) == 1          # idle: nothing to say yet
        now[0] += 400
        assert pub.tick({"updated": 3, "a": 1}) is True                                      # 700 s since the last upload
        assert set(got["puts"][-1]["files"]) == {"state.json"}                                # only the "still alive" state
        assert json.loads(base64.b64decode(got["puts"][-1]["files"]["state.json"]["b64"]))["updated"] == 3
        # a stale keep-alive connection: the first attempt is reset, the second (fresh) one goes through
        calls = {"n": 0}

        class Flaky:
            def __init__(self, real):
                self.real = real

            def put(self, *a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise requests.ConnectionError("reset")
                return self.real.put(*a, **k)

            def close(self):
                pass
        pub2 = HttpPublisher(f"http://127.0.0.1:{srv.server_address[1]}", "t", min_interval=0, clock=lambda: now[0],
                             template_dir=PAGE_DIR, session=Flaky(requests.Session()))
        assert pub2.tick({"updated": 1, "a": 9}) is True and pub2.failures == 0 and calls["n"] == 1 or calls["n"] == 2
    finally:
        srv.shutdown()
