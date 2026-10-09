"""The service the panel talks to: who gets in, what the panel is given, and one conversation kept in step
with what Jarvis remembers of it. The network is played here; tests/core/test_service_live.py starts the
real thing."""
import asyncio
import json

import httpx
from fakes import FakeOllama, run, text_chunks, tool_chunks

from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.server import HEADERS, SURFACE, create_app

BRAIN, DESKTOP, OTHER = "192.0.2.20", "192.0.2.21", "192.0.2.50"
ORIGIN = f"https://{BRAIN}:8443"
PANEL = {"sec-fetch-site": "same-origin", "host": f"{BRAIN}:8443"}


def settings(tmp_path, **changes):
    values = {"data_dir": tmp_path / "data", "release": "v0.6.0", "site": tmp_path / "site.env", "address": BRAIN,
              "panel_allow": (DESKTOP,)}
    values.update(changes)
    return Settings(**values)


class Bench:
    def __init__(self, tmp_path, fake=None, **changes):
        self.fake = fake or FakeOllama()
        self.settings = settings(tmp_path, **changes)
        self.jarvis = Jarvis(self.settings, self.fake.client(), SURFACE)
        self.app = create_app(self.jarvis, self.settings)

    def records(self):
        path = self.settings.audit_path
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def client(self, peer=DESKTOP):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(peer, 40000)), base_url=ORIGIN)

    def ask(self, method, path, peer=DESKTOP, headers=PANEL, **kwargs):
        async def go():
            async with self.client(peer) as client:
                return await client.request(method, path, headers=headers, **kwargs)
        return run(go())

    def post(self, path, body, **kwargs):
        headers = dict(PANEL, origin=ORIGIN, **kwargs.pop("headers", {}))
        return self.ask("POST", path, headers=headers, json=body, **kwargs)


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_only_the_named_addresses_get_in_and_this_machine_only_its_own_check(tmp_path):
    bench = Bench(tmp_path)
    assert bench.ask("GET", "/api/state").status_code == 200
    refused = bench.ask("GET", "/api/state", peer=OTHER)
    assert refused.status_code == 403 and refused.json() == {"error": "this address may not talk to Jarvis"}
    assert bench.ask("GET", "/panel/", peer=OTHER).status_code == 403
    for own in ("127.0.0.1", "::1", BRAIN):
        assert bench.ask("GET", "/api/health", peer=own, headers={}).json() == {"ok": True, "release": "v0.6.0"}
        assert bench.ask("GET", "/api/state", peer=own).status_code == 403
        assert bench.ask("GET", "/panel/", peer=own).status_code == 403
    # A network may be named instead of single addresses; nobody named means nobody.
    assert Bench(tmp_path / "net", panel_allow=("192.0.2.0/24",)).ask("GET", "/api/state", peer=OTHER).status_code == 200
    assert Bench(tmp_path / "none", panel_allow=()).ask("GET", "/api/state").status_code == 403


def test_a_page_from_elsewhere_in_the_same_browser_gets_nothing(tmp_path):
    bench = Bench(tmp_path, FakeOllama([text_chunks("Hello.")]))
    elsewhere = "https://example.org"
    # It fetches, posts, or puts a piece of the panel into itself.
    for method, path, headers in (
        ("GET", "/api/state", {"sec-fetch-site": "cross-site"}),
        ("GET", "/api/state", {}),                                             # not a browser's request at all
        ("GET", "/api/state", {"sec-fetch-site": "same-site"}),
        ("GET", "/panel/panel.js", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"}),
        ("GET", "/panel/", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}),
        ("POST", "/api/chat", {"sec-fetch-site": "cross-site", "origin": elsewhere, "content-type": "text/plain"}),
        ("POST", "/api/chat", {"sec-fetch-site": "same-origin", "origin": elsewhere, "content-type": "application/json"}),
        ("POST", "/api/chat", {"sec-fetch-site": "same-origin", "origin": ORIGIN, "content-type": "text/plain"}),
        ("POST", "/api/new", {"sec-fetch-site": "none", "content-type": "application/json"}),
        ("POST", "/panel/", {"sec-fetch-site": "same-origin", "origin": ORIGIN, "content-type": "application/json"}),
    ):
        refused = bench.ask(method, path, headers=dict(headers, host=f"{BRAIN}:8443"), content=b'{"text": "Restart everything"}')
        assert refused.status_code == 403, (method, path, headers, refused.text)
    # A name of its own that it points at this machine.
    refused = bench.ask("GET", "/api/state", headers={"sec-fetch-site": "same-origin", "host": "evil.example.org:8443"})
    assert refused.status_code == 403 and "another name" in refused.json()["error"]
    assert bench.fake.requests == [], "nothing reached the model"
    # It may send the owner to the panel as a whole page, and that page cannot be put inside another.
    whole = bench.ask("GET", "/panel/", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "host": f"{BRAIN}:8443"})
    assert whole.status_code == 200 and whole.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in whole.headers["content-security-policy"]
    # Whether the brain is there is all another page may learn.
    ping = bench.ask("GET", "/panel/ping", headers={"sec-fetch-site": "cross-site", "host": f"{BRAIN}:8443"})
    assert ping.status_code == 204 and ping.content == b"" and ping.headers["cross-origin-resource-policy"] == "cross-origin"


def test_refusals_are_audited_without_filling_the_log(tmp_path):
    bench = Bench(tmp_path)
    for _ in range(50):
        bench.ask("GET", "/api/state", peer=OTHER)
    denied = [r for r in bench.records() if r["kind"] == "request_denied"]
    assert len(denied) == 1 and denied[0]["data"] == {"client": OTHER, "path": "/api/state", "reason": "this address may not talk to Jarvis"}
    bench.app._last_note -= 60
    bench.ask("GET", "/api/state", peer=OTHER)
    assert [r["data"].get("more_since_last") for r in bench.records() if r["kind"] == "request_denied"] == [None, 49]


def test_the_panel_is_served_whole_and_only_what_belongs_to_it(tmp_path):
    bench = Bench(tmp_path)
    page = bench.ask("GET", "/panel/", headers={"sec-fetch-site": "none", "host": f"{BRAIN}:8443"})
    assert page.status_code == 200 and page.headers["content-type"] == "text/html; charset=utf-8"
    assert '<script src="panel.js"></script>' in page.text and "<style" not in page.text and "onclick" not in page.text
    for name, kind in (("panel.js", "text/javascript; charset=utf-8"), ("panel.css", "text/css; charset=utf-8"),
                       ("Oxanium.ttf", "font/ttf"), ("icon.png", "image/png")):
        piece = bench.ask("GET", f"/panel/{name}")
        assert piece.status_code == 200 and piece.headers["content-type"] == kind and len(piece.content) > 500, name
        for header, value in HEADERS.items():
            assert piece.headers[header] == value, (name, header)
    assert "script-src 'self'" in HEADERS["content-security-policy"] and "unsafe" not in HEADERS["content-security-policy"]
    for missing in ("/panel/server.py", "/panel/..%2fserver.py", "/panel/%2e%2e/config.py", "/panel/index.html.bak", "/api/nothing"):
        assert bench.ask("GET", missing).status_code == 404, missing
    assert bench.ask("GET", "/", headers={"sec-fetch-site": "none", "host": f"{BRAIN}:8443"}).headers["location"] == "/panel/"
    # Every page the panel names is one the service has, and it names nothing from anywhere else.
    for name in ("index.html", "panel.css", "panel.js"):
        text = bench.ask("GET", f"/panel/{name}").text
        assert "http://" not in text.replace("http://www.w3.org/2000/svg", "") and "https://" not in text, name


def test_a_turn_reaches_the_panel_as_events_and_is_kept_for_showing_again(tmp_path):
    fake = FakeOllama([tool_chunks("local_status", {}), text_chunks("Up for ", "three hours.")])
    bench = Bench(tmp_path, fake)
    answer = bench.post("/api/chat", {"text": "  How long have you been up?  "})
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/event-stream")
    got = events(answer)
    assert [e["type"] for e in got if e["type"] != "token"] == ["route", "working", "tool", "done"]
    assert "".join(e["text"] for e in got if e["type"] == "token") == "Up for three hours."
    state = bench.ask("GET", "/api/state").json()
    assert state["conversation"] == [{"role": "you", "text": "How long have you been up?"},
                                     {"role": "jarvis", "text": "Up for three hours.", "state": "done"}]
    assert state["busy"] is False and state["release"] == "v0.6.0" and state["model"] == "qwen3:8b" and state["waiting"] == []
    # The model was told where its words are read, and the turn is in the audit log under the owner's name.
    assert "in your panel, which is docked beside a web browser" in fake.requests[0]["messages"][0]["content"]
    turn = bench.records()[-1]
    assert turn["kind"] == "turn" and turn["data"]["user"] == "owner" and turn["data"]["session"] == "panel"


def test_what_the_panel_sends_is_checked(tmp_path):
    bench = Bench(tmp_path)
    for body in ({}, {"text": ""}, {"text": "   "}, {"text": 7}, {"text": "x" * 4001}, ["text"], "text"):
        assert bench.post("/api/chat", body).status_code == 400, body
    broken = bench.ask("POST", "/api/chat", headers=dict(PANEL, origin=ORIGIN, **{"content-type": "application/json"}), content=b"{not json")
    assert broken.status_code == 400 and bench.fake.requests == []
    none = Bench(tmp_path / "none", model="none")
    refused = none.post("/api/chat", {"text": "Hello"})
    assert refused.status_code == 503 and "no local model is set" in refused.json()["error"]
    assert none.ask("GET", "/api/state").json()["model"] is None


def test_a_model_that_is_away_is_told_to_the_panel(tmp_path):
    bench = Bench(tmp_path, FakeOllama(down=True))
    got = events(bench.post("/api/chat", {"text": "Hello?"}))
    assert got[-1]["type"] == "error" and "ollama unreachable" in got[-1]["message"]
    last = bench.ask("GET", "/api/state").json()["conversation"][-1]
    assert last["state"] == "failed" and "ollama unreachable" in last["note"]
    assert bench.jarvis.router._sessions["panel"] == []


def test_one_answer_at_a_time_and_a_stopped_one_is_forgotten(tmp_path):
    """The panel goes away in the middle of an answer (or its Stop is pressed, which is the same thing)."""
    class Slow(FakeOllama):
        async def handler(self, request):
            if request.url.path == "/api/chat" and not self.requests:
                self.requests.append(json.loads(request.content))
                await asyncio.sleep(3600)
            return FakeOllama.handler(self, request)

    bench = Bench(tmp_path, Slow([text_chunks("Still here.")]))
    panel = bench.app.panel

    async def go():
        first = asyncio.ensure_future(panel.turn("Tell me a long story.").__anext__())
        while not bench.fake.requests:   # until the question is with the model
            await asyncio.sleep(0)
        async with bench.client() as client:
            busy = await client.get("/api/state", headers=PANEL)
            second = await client.post("/api/chat", headers=dict(PANEL, origin=ORIGIN), json={"text": "And another."})
            new = await client.post("/api/new", headers=dict(PANEL, origin=ORIGIN), json={})
        first.cancel()   # what the server does to an answer whose reader has gone
        try:
            await first
        except asyncio.CancelledError:
            pass
        return busy.json(), second, new

    busy, second, new = run(go())
    assert busy["busy"] is True and busy["conversation"][-1] == {"role": "jarvis", "text": "", "state": "answering"}
    assert second.status_code == 409 and new.status_code == 409 and second.json() == {"error": "Jarvis is still answering."}
    state = bench.ask("GET", "/api/state").json()
    assert state["busy"] is False and state["conversation"][-1]["state"] == "stopped"
    assert bench.jarvis.router._sessions["panel"] == [] and bench.records()[-1]["kind"] == "turn_aborted"
    # The next question is answered, and the model is not shown the one that was stopped.
    assert events(bench.post("/api/chat", {"text": "Are you there?"}))[-1]["type"] == "done"
    assert [m["content"] for m in bench.fake.requests[1]["messages"][1:]] == ["Are you there?"]


def test_a_new_conversation_forgets_the_one_before(tmp_path):
    fake = FakeOllama([text_chunks("One."), text_chunks("Two.")])
    bench = Bench(tmp_path, fake)
    bench.post("/api/chat", {"text": "First."})
    assert bench.post("/api/new", {}).json() == {"ok": True}
    assert bench.ask("GET", "/api/state").json()["conversation"] == []
    bench.post("/api/chat", {"text": "Second."})
    assert [m["content"] for m in fake.requests[1]["messages"][1:]] == ["Second."]


def test_the_readings_come_from_the_tool_and_not_more_often_than_needed(tmp_path):
    loaded = {"models": [{"name": "qwen3:8b", "size": 7_500_000_000, "size_vram": 7_500_000_000}]}
    bench = Bench(tmp_path, FakeOllama(ps=loaded))
    asked = []
    original = bench.fake.handler
    bench.fake.handler = lambda request: (asked.append(request.url.path), original(request))[1]
    bench.jarvis.client._transport = httpx.MockTransport(bench.fake.handler)
    pulse = bench.ask("GET", "/api/state").json()["pulse"]
    assert pulse["model_server_up"] is True and pulse["model_loaded"] is True and pulse["model_on_gpu_percent"] == 100
    assert pulse["uptime_hours"] >= 0 and pulse["cpu_count"] >= 1 and isinstance(pulse["host"], str)
    bench.ask("GET", "/api/state")
    assert asked.count("/api/ps") == 1 and [r for r in bench.records() if r["kind"] == "tool_call"] == []
    down = Bench(tmp_path / "down", FakeOllama(down=True)).ask("GET", "/api/state").json()["pulse"]
    assert down["model_server_up"] is False and down["model_loaded"] is False


def test_a_log_that_cannot_be_written_is_told_to_the_panel_not_left_as_silence(tmp_path):
    bench = Bench(tmp_path, FakeOllama([text_chunks("One."), text_chunks("Two.")]))
    bench.post("/api/chat", {"text": "First."})
    with open(bench.settings.audit_path, "a") as log:
        log.write("not a record\n")
    got = events(bench.post("/api/chat", {"text": "Second."}))
    assert got[-1]["type"] == "error" and "AuditDamaged" in got[-1]["message"]
    state = bench.ask("GET", "/api/state").json()
    assert state["busy"] is False and state["conversation"][-1]["state"] == "failed"


def test_the_panel_is_told_how_the_vault_and_the_cluster_stand(tmp_path):
    """Locked after a restart, then unlocked: the panel's readings follow, and so do the model's tools."""
    import subprocess

    from test_vault import TOKEN, VALUES, lab, to_lab, unlock
    from vaults import SOPS, identity, vault_file

    config = settings(tmp_path, env_dir=tmp_path / "secrets", secrets_mode="sops", identity=tmp_path / "run" / "age.key",
                      sops=SOPS, trust_dir=tmp_path / "trust")
    key, public = identity(tmp_path / "keys")
    vault_file(config.env_dir / "lab.enc.env", VALUES, public)
    config.trust_dir.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=test", "-keyout", "/dev/null", "-out", str(config.trust_dir / "proxmox-ca.pem")],
                   check=True, capture_output=True)
    jarvis = Jarvis(config, FakeOllama().client(), SURFACE, record_vault=True)
    app = create_app(jarvis, config)

    async def state():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(DESKTOP, 40000)), base_url=ORIGIN) as client:
            return (await client.get("/api/state", headers=PANEL)).json()

    locked = run(state())
    assert locked["vault"]["status"] == "locked" and "jarvis-unlock" in locked["vault"]["detail"]
    assert locked["lab"] == dict.fromkeys(("proxmox", "pbs", "opnsense", "dns", "switch", "notes"), "the vault is locked") and "cluster" not in locked["pulse"]
    unlock(config, key)
    assert jarvis.refresh() is True
    to_lab(jarvis)
    app.panel._pulse = (0.0, {})
    open_ = run(state())
    assert open_["vault"] == {"status": "unlocked", "detail": "3 values from 1 file"} and open_["lab"] == {"proxmox": "ready"}
    assert open_["pulse"]["cluster"] == {"nodes": 2, "online": 1, "quorate": True}
    assert TOKEN not in json.dumps(open_)


def test_the_running_service_notices_an_unlock_and_waits_for_an_answer_to_end(tmp_path, monkeypatch):
    """Through the service's own watch, as it runs: not a direct call."""
    import subprocess

    import jarvis.server as server
    from test_vault import VALUES, unlock
    from vaults import SOPS, identity, vault_file

    monkeypatch.setattr(server, "VAULT_EVERY", 0.05)
    config = settings(tmp_path, env_dir=tmp_path / "secrets", secrets_mode="sops", identity=tmp_path / "run" / "age.key",
                      sops=SOPS, trust_dir=tmp_path / "trust")
    key, public = identity(tmp_path / "keys")
    vault_file(config.env_dir / "lab.enc.env", VALUES, public)
    config.trust_dir.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=test", "-keyout", "/dev/null", "-out", str(config.trust_dir / "proxmox-ca.pem")],
                   check=True, capture_output=True)
    jarvis = Jarvis(config, FakeOllama().client(), SURFACE, record_vault=True)
    app = create_app(jarvis, config)

    async def go():
        sent = []
        started = asyncio.Event()
        stopping = asyncio.Event()

        async def receive():
            if not started.is_set():
                started.set()
                return {"type": "lifespan.startup"}
            await stopping.wait()
            return {"type": "lifespan.shutdown"}

        async def send(message):
            sent.append(message["type"])

        life = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))

        async def until(check, seconds=5):
            for _ in range(int(seconds / 0.02)):
                if check():
                    return True
                await asyncio.sleep(0.02)
            return False

        assert await until(lambda: "lifespan.startup.complete" in sent)
        assert jarvis.vault.status == "locked"
        app.panel.busy = True          # an answer is being written
        unlock(config, key)
        await asyncio.sleep(0.5)
        held = (jarvis.vault.status, jarvis.tools.names())
        app.panel.busy = False         # it ends
        noticed = await until(lambda: "proxmox_cluster_status" in jarvis.tools.names())
        stopping.set()
        await life
        return held, noticed

    held, noticed = run(go())
    assert held == ("locked", ["local_status"]), "nothing changes under an answer in progress"
    assert noticed and jarvis.vault.status == "unlocked"
