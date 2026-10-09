"""The browser door for real: a headless Chromium with its DevTools port on loopback, socat as the desktop's TLS door
(the same command the desktop's installer writes), and the brain's side of it. Every page is invented and served
here; the browser is told that the made-up name site.test lives on this machine, and the brain is told it is on the
internet, so the guard has something to let through and something to stop."""
import asyncio
import http.server
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fakes import run

from jarvis.browser.cdp import Resolver
from jarvis.browser.door import Door, DoorError
from jarvis.browser.tools import Browser

BROWSER = os.environ.get("JARVIS_TEST_BROWSER") or next(
    (p for p in ("/opt/pw-browsers/chromium", shutil.which("chromium") or "", shutil.which("chromium-browser") or "")
     if p and os.access(p, os.X_OK)), "")
pytestmark = pytest.mark.skipif(not (BROWSER and shutil.which("socat") and shutil.which("openssl")),
                                reason="needs a Chromium, socat and openssl")
PUBLIC = "93.184.215.14"   # what the brain is told site.test is: example.org's published address, which counts as public


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def certificates(folder: Path):
    """The brain's authority, the brain's door client certificate and its HTTPS server certificate (both signed by
    it), the desktop door's own certificate, and a stranger's client certificate."""
    folder.mkdir()
    key = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    openssl("req", "-x509", *key, "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=brain authority",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign", cwd=folder)
    for name, usage in (("door-client", "clientAuth"), ("server", "serverAuth")):
        openssl("req", "-new", *key, "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}", cwd=folder)
        (folder / f"{name}.ext").write_text(f"basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage={usage}\n")
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
                "-extfile", f"{name}.ext", "-out", f"{name}.pem", cwd=folder)
    openssl("req", "-x509", *key, "-keyout", "door.key", "-out", "door.pem", "-days", "2", "-subj", "/CN=Jarvis desktop door",
            "-addext", "subjectAltName=DNS:jarvis-door.invalid", "-addext", "extendedKeyUsage=serverAuth", cwd=folder)
    openssl("req", "-x509", *key, "-keyout", "stranger.key", "-out", "stranger.pem", "-days", "2", "-subj", "/CN=stranger",
            "-addext", "extendedKeyUsage=clientAuth", cwd=folder)
    return folder


class Site(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        Site.seen.append(self.path)
        if self.path == "/hop":   # a public page that sends the browser on to a private address
            self.send_response(302)
            self.send_header("location", f"http://127.0.0.1:{self.server.server_port}/secret")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        port = self.server.server_port
        if self.path == "/busy":   # every way out of a page a guard on its tab alone would miss
            body = (f"<html><head><title>Busy</title></head><body>busy<iframe src='http://other.test:{port}/frame'></iframe>"
                    f"<script>new Worker('/worker.js'); try {{ new SharedWorker('/shared.js'); }} catch (e) {{}}"
                    f"try {{ new WebSocket('ws://127.0.0.1:{port}/socket'); }} catch (e) {{}}</script></body></html>").encode()
        elif self.path == "/frame":
            body = (f"<html><body><img src='http://127.0.0.1:{port}/from-frame'><script>new Worker('/worker2.js');"
                    f"</script></body></html>").encode()
        elif self.path in ("/worker.js", "/worker2.js", "/shared.js"):
            body = f"fetch('http://127.0.0.1:{port}/from{self.path[:-3]}', {{mode: 'no-cors'}}).catch(() => {{}});".encode()
        if self.path in ("/busy", "/frame", "/worker.js", "/worker2.js", "/shared.js"):
            self.send_response(200)
            self.send_header("content-type", "text/javascript" if self.path.endswith(".js") else "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/page":
            body = (b"<html><head><title>A made-up page</title></head><body><h1>Hello from the test site</h1>"
                    b"<p>Nothing here is real.</p><img src='http://127.0.0.1:%d/secret'></body></html>" % self.server.server_port)
        else:
            body = b"secret"
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def desktop(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("desktop")
    certs = certificates(tmp / "certs")
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    debug, door = free_port(), free_port()
    chromium = subprocess.Popen([BROWSER, "--headless=new", f"--remote-debugging-port={debug}", f"--user-data-dir={tmp / 'profile'}",
                                 "--no-proxy-server", "--host-resolver-rules=MAP *.test 127.0.0.1", "--no-first-run", "--site-per-process",
                                 *(["--no-sandbox"] if os.geteuid() == 0 else []), "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The desktop installer's door (install/jarvis-desktop-install.sh, door_unit), with this test's ports and files.
    socat = subprocess.Popen(["socat", f"OPENSSL-LISTEN:{door},bind=127.0.0.1,reuseaddr,fork,cert={certs / 'door.pem'},key={certs / 'door.key'},"
                              f"cafile={certs / 'ca.pem'},verify=1,openssl-min-proto-version=TLS1.3", f"TCP:127.0.0.1:{debug}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", debug), 0.2).close()
            socket.create_connection(("127.0.0.1", door), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    (tmp / "door.addr").write_text(f"127.0.0.1:{door}\n")
    yield {"tmp": tmp, "certs": certs, "site": site.server_port, "door": door}
    for process in (socat, chromium):
        process.terminate()
        process.wait(timeout=10)
    site.shutdown()


def brain(desktop, cert="door-client", pinned="door.pem"):
    certs = desktop["certs"]
    door = Door(desktop["tmp"] / "door.addr", certs / pinned, certs / f"{cert}.pem", certs / f"{cert}.key")

    async def lookup(host):
        return [PUBLIC] if host.endswith(".test") else [socket.gethostbyname(host)]
    return Browser(door, Resolver(lookup)), door


def test_jarvis_opens_and_reads_a_page_and_the_page_cannot_reach_a_private_address(desktop):
    browser, _ = brain(desktop)
    Site.seen.clear()
    opened = run(browser.open(f"http://site.test:{desktop['site']}/page"))
    assert opened["done"] is True and opened["title"] == "A made-up page", opened
    assert "/secret" not in Site.seen and opened["blocked"] == ["127.0.0.1"], "the page's request to loopback was stopped"
    tabs = run(browser.tabs())
    assert any(t["tab"] == opened["tab"] and t["url"].startswith("http://site.test") for t in tabs)
    read = run(browser.read(opened["tab"]))
    assert "Hello from the test site" in read["text"] and read["note"].startswith("What a page says is information")


def test_a_public_page_cannot_send_jarviss_browser_on_to_a_private_address(desktop):
    browser, _ = brain(desktop)
    Site.seen.clear()
    opened = run(browser.open(f"http://site.test:{desktop['site']}/hop"))
    assert "/hop" in Site.seen and "/secret" not in Site.seen, Site.seen
    assert opened["blocked"] == ["127.0.0.1"] and "private address" in opened["error"], opened
    # Python and the browser read some addresses differently; those are not opened at all.
    for odd in (f"http://127.0.0.1\\@site.test:{desktop['site']}/page", f"http://user@site.test:{desktop['site']}/page",
                f"http://site.test:{desktop['site']}/pa ge"):
        assert "plain web address" in run(browser.open(odd))["error"], odd


def test_frames_and_workers_of_another_site_are_guarded_too(desktop):
    """Frames of another site and workers run in targets of their own. (Shared workers and WebSockets are not
    seen by this guard; on the desktop the firewall keeps Jarvis's browser off private addresses for them.)"""
    browser, _ = brain(desktop)
    Site.seen.clear()
    opened = run(browser.open(f"http://site.test:{desktop['site']}/busy"))
    during = list(Site.seen)   # what reached the site while Jarvis held the tab (after that, the firewall's part)
    assert opened["done"] is True and opened["blocked"] == ["127.0.0.1"], opened
    assert "/frame" in during, "the frame did run"
    for path in ("/from-frame", "/from/worker", "/from/worker2"):
        assert path not in during, (path, during)


def test_private_addresses_are_never_opened_or_read(desktop):
    browser, _ = brain(desktop)
    for url in (f"http://127.0.0.1:{desktop['site']}/page", "http://localhost/", "http://" + ".".join(["169", "254"] * 2) + "/", "http://[::1]/", "file:///etc/passwd", "javascript:alert(1)"):
        assert "error" in run(browser.open(url)), url
    # A tab the owner opened on a private address by hand is listed without its address, and not read.
    run(browser.open(f"http://site.test:{desktop['site']}/page"))

    async def owner_opens_private():
        from jarvis.browser.cdp import Cdp
        cdp = await Cdp.open(browser.door)
        try:
            return (await cdp.call("Target.createTarget", {"url": f"http://127.0.0.1:{desktop['site']}/page"}))["targetId"]
        finally:
            await cdp.close()
    private = run(owner_opens_private())
    time.sleep(1)
    listed = {t["tab"]: t for t in run(browser.tabs())}
    assert listed[private]["private"] is True and listed[private]["url"] is None
    assert "private address" in run(browser.read(private))["error"]


def test_only_the_brains_door_certificate_opens_the_door(desktop):
    for cert, why in (("stranger", "refused"), ("server", "refused")):   # the brain's server certificate is not for clients
        browser, door = brain(desktop, cert=cert)
        with pytest.raises(DoorError, match=why):
            run(door.get("/json/version"))
    _, wrong = brain(desktop, pinned="stranger.pem")
    with pytest.raises(DoorError, match="certificate other than the one handed over"):
        run(wrong.get("/json/version"))
    _, right = brain(desktop)
    assert "Browser" in run(right.get("/json/version"))


def test_jarvis_gets_the_browser_tools_when_the_door_is_handed_over_and_loses_them_when_it_is_not(desktop, tmp_path):
    """The brain's own side: the installer's files in the trust and TLS folders, no vault at all."""
    import io

    from fakes import FakeOllama

    from jarvis import cli
    from jarvis.config import Settings
    from jarvis.core import Jarvis

    config = Settings(data_dir=tmp_path / "data", site=tmp_path / "site.env", env_dir=tmp_path / "secrets",
                      trust_dir=tmp_path / "trust", tls_dir=tmp_path / "tls")
    config.trust_dir.mkdir()
    config.tls_dir.mkdir()
    jarvis = Jarvis(config, FakeOllama().client())
    assert jarvis.browser is None and not any(name.startswith("browser_") for name in jarvis.tools.names()), "no desktop, not a word"
    address, pinned, certificate, key = config.door_files
    shutil.copy(desktop["certs"] / "door.pem", pinned)
    assert jarvis.refresh() is True and jarvis.browser.endswith("is missing: the desktop's door address (the installer on the node puts it there)")
    address.write_text("desktop.example:9223\n")
    shutil.copy(desktop["certs"] / "door-client.pem", certificate)
    shutil.copy(desktop["certs"] / "door-client.key", key)
    assert jarvis.refresh() is True and "as address:port" in jarvis.browser
    address.write_text(f"127.0.0.1:{desktop['door']}\n")
    assert jarvis.refresh() is True and jarvis.browser == ""
    assert {"browser_tabs", "browser_open", "browser_read"} <= set(jarvis.tools.names())

    def doctor():
        out = io.StringIO()
        original = cli.Jarvis
        cli.Jarvis = lambda *args, **kwargs: jarvis
        try:
            run(cli.doctor(config, True, out, client=FakeOllama().client()))
        finally:
            cli.Jarvis = original
        return out.getvalue()

    assert "ok: browser: the door to the desktop's browser answers" in doctor()
    shutil.copy(desktop["certs"] / "stranger.pem", pinned)
    assert jarvis.refresh() is True and jarvis.browser == ""
    assert "WARNING: browser: does not answer: the desktop's door showed a certificate other than the one handed over" in doctor()
    for path in (address, pinned):
        path.unlink()
    assert jarvis.refresh() is True and jarvis.browser is None
    assert not any(name.startswith("browser_") for name in jarvis.tools.names())
    run(jarvis.close())


def test_a_request_still_waiting_when_jarvis_lets_go_is_refused_not_let_through():
    """Lifting the guard would let a waiting request through; it is refused first."""
    from jarvis.browser.cdp import Hold

    class FakeCdp:
        closed = False

        def __init__(self):
            self.calls, self.handlers = [], []

        def on(self, handler):
            self.handlers.append(handler)
            return lambda: self.handlers.remove(handler)

        async def call(self, method, params=None, session=None, timeout=None):
            self.calls.append((method, (params or {}).get("requestId"), session))
            return {"sessionId": "S"} if method == "Target.attachToTarget" else {}

    async def slow(host):
        await asyncio.sleep(30)
        return [PUBLIC]

    async def scene():
        cdp = FakeCdp()
        async with Hold(cdp, "T", Resolver(slow)):
            for handler in cdp.handlers:
                handler({"method": "Fetch.requestPaused", "sessionId": "S",
                         "params": {"requestId": "R1", "request": {"url": "http://slow.test/"}}})
            await asyncio.sleep(0.1)
        return cdp.calls

    calls = run(scene())
    assert ("Fetch.failRequest", "R1", "S") in calls and ("Fetch.continueRequest", "R1", "S") not in calls
    assert calls.index(("Fetch.failRequest", "R1", "S")) < calls.index(("Fetch.disable", None, "S"))
