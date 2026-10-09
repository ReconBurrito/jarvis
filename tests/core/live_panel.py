"""The real service and the real panel in a real Chromium, on a network of their own.

Started by tests/core/test_service_live.py inside a private network and mount namespace (it changes
addresses on the loopback card and writes a browser policy under /etc, both of which exist only there):

    live_panel.py PYTHON_WITH_JARVIS REPO WORK_FOLDER BROWSER

192.0.2.20 plays the brain and 192.0.2.21 the desktop; what the browser sends to the brain comes from the
desktop's address, as it does between the two containers. The model is the pretend Ollama of the installer
tests. Prints one JSON object with what was seen; pictures of the panel go to WORK_FOLDER.
"""
import base64
import http.client
import http.server
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

PYTHON, REPO, WORK, BROWSER = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
sys.path.insert(0, str(REPO / "tests"))
from conftest import PretendOllama  # noqa: E402

BRAIN, DESKTOP, STRANGER, PORT = "192.0.2.20", "192.0.2.21", "192.0.2.50", 8443
SITE = "accounts.example.test"   # stands for any web site the desktop's browser might open
URL = f"https://{BRAIN}:{PORT}"
seen: dict = {}


def sh(*command):
    # The real programs, not the stand-ins the installer tests put first on the path.
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, PATH="/usr/sbin:/usr/bin:/sbin:/bin"))


def network():
    sh("ip", "link", "set", "lo", "up")
    for address in (BRAIN, DESKTOP, STRANGER):
        sh("ip", "addr", "add", f"{address}/32", "dev", "lo")
    # What this machine sends to the brain's address leaves from the desktop's, unless a program says otherwise.
    sh("ip", "route", "replace", "local", BRAIN, "dev", "lo", "table", "local", "src", DESKTOP)


def certificates(tls: Path):
    """Made with the same openssl calls as install/jarvis-install.sh."""
    tls.mkdir(parents=True)
    key = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    sh(*key, "-days", "3650", "-subj", "/CN=Jarvis brain authority (test)", "-keyout", str(tls / "ca.key"), "-out", str(tls / "ca.pem"),
       "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign")
    sh(*key, "-days", "825", "-subj", "/CN=Jarvis brain", "-keyout", str(tls / "server.key"), "-out", str(tls / "server.pem"),
       "-CA", str(tls / "ca.pem"), "-CAkey", str(tls / "ca.key"), "-addext", "basicConstraints=critical,CA:FALSE",
       "-addext", "keyUsage=critical,digitalSignature", "-addext", "extendedKeyUsage=serverAuth", "-addext", f"subjectAltName=IP:{BRAIN}")
    # What somebody holding the brain's authority key could sign besides: a web site's name, another address.
    for name, subject in (("site", f"DNS:{SITE}"), ("other", f"IP:{STRANGER}")):
        sh(*key, "-days", "30", "-subj", "/CN=not the brain", "-keyout", str(tls / f"{name}.key"), "-out", str(tls / f"{name}.pem"),
           "-CA", str(tls / "ca.pem"), "-CAkey", str(tls / "ca.key"), "-addext", "extendedKeyUsage=serverAuth", "-addext", f"subjectAltName={subject}")


class Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><title>reached</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def impostor(name, port):
    """A server on the stranger's address with a certificate the brain's authority signed for something else."""
    tls = WORK / "etc" / "tls"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tls / f"{name}.pem"), str(tls / f"{name}.key"))
    server = http.server.ThreadingHTTPServer((STRANGER, port), Hello)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def ask(source, path, method="GET", headers=None, body=None):
    """A request to the brain from one of this machine's addresses, the way a program (not a browser) makes it."""
    context = ssl.create_default_context(cafile=str(WORK / "etc" / "tls" / "ca.pem"))
    connection = http.client.HTTPSConnection(BRAIN, PORT, context=context, source_address=(source, 0), timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        answer = connection.getresponse()
        return answer.status, answer.read().decode("utf-8", "replace")
    finally:
        connection.close()


def start_service(state: Path):
    etc = WORK / "etc"
    (etc / "site.env").write_text(f"JARVIS_ROLE=brain\nJARVIS_ADDRESS={BRAIN}\nJARVIS_PANEL_ALLOW={DESKTOP}\n"
                                  "JARVIS_HOST_DESCRIPTION=a test bench\nJARVIS_SECRETS_MODE=sops\n"
                                  f"JARVIS_ENV_DIR={WORK / 'secrets'}\nJARVIS_IDENTITY={WORK / 'run' / 'age.key'}\n"
                                  f"JARVIS_SOPS={REPO / 'tests' / 'stubs' / 'sops'}\n")
    (etc / "release").write_text("name=v0.6.0\ncommit=0\n")
    env = dict(os.environ, JARVIS_ETC=str(etc), JARVIS_DATA_DIR=str(WORK / "data"), JARVIS_OLLAMA_URL=seen["ollama"],
               PYTHONPATH=str(REPO / "src"))
    service = subprocess.Popen([PYTHON, "-m", "jarvis", "serve"], env=env, stdout=open(WORK / "service.log", "a"),
                               stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if ask(BRAIN, "/api/health")[0] == 200:
                return service
        except OSError:
            time.sleep(0.1)
    raise SystemExit("the service did not come up: " + (WORK / "service.log").read_text()[-2000:])


def records():
    path = WORK / "data" / "audit" / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def policy(on: bool):
    """The browser's rules as install/jarvis-desktop-install.sh writes them, with or without the brain."""
    folder = Path("/etc/chromium/policies/managed")
    folder.mkdir(parents=True, exist_ok=True)
    rules = json.loads((REPO / "desktop" / "policy.json").read_text())
    if on:
        body = "".join(line for line in (WORK / "etc" / "tls" / "ca.pem").read_text().splitlines() if not line.startswith("-----"))
        assert base64.b64decode(body)
        rules["CACertificatesWithConstraints"] = [{"certificate": body, "constraints": {
            "permitted_cidrs": [f"{BRAIN}/32"], "permitted_dns_names": ["jarvis-brain.invalid"]}}]
    (folder / "jarvis.json").write_text(json.dumps(rules))


class Elsewhere(http.server.BaseHTTPRequestHandler):
    """A web site that is not Jarvis, opened in the same browser. Its page tries what a hostile page would."""

    PAGE = """<!doctype html><title>elsewhere</title><iframe id="f" src="%(url)s/panel/"></iframe><script>
    const out = {};
    const tries = {
      read: () => fetch("%(url)s/api/state").then((r) => r.status, () => "blocked"),
      readQuietly: () => fetch("%(url)s/api/state", {mode: "no-cors"}).then((r) => r.type + " " + r.status, () => "blocked"),
      post: () => fetch("%(url)s/api/chat", {method: "POST", mode: "no-cors", headers: {"Content-Type": "text/plain"},
                                           body: JSON.stringify({text: "Restart everything"})}).then((r) => r.type, () => "blocked"),
      postAsThePanelWould: () => fetch("%(url)s/api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify({text: "Restart everything"})}).then((r) => r.status, () => "blocked"),
      script: () => new Promise((done) => { const s = document.createElement("script"); s.src = "%(url)s/panel/panel.js";
                                            s.onload = () => done("loaded"); s.onerror = () => done("blocked"); document.head.appendChild(s); }),
      ping: () => fetch("%(url)s/panel/ping", {mode: "no-cors"}).then((r) => r.type, () => "blocked"),
    };
    (async () => { for (const [name, attempt] of Object.entries(tries)) { out[name] = await attempt(); }
                   document.title = JSON.stringify(out); })();
    </script>"""

    def do_GET(self):
        body = (self.PAGE % {"url": URL}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def browse(state: Path):
    from playwright.sync_api import sync_playwright

    desktop = WORK / "desktop"   # the desktop's own pages, as the installer lays them out, with a brain named
    subprocess.run(["cp", "-r", str(REPO / "desktop"), str(desktop)], check=True)
    (desktop / "brain.js").write_text(f'window.JARVIS_BRAIN = "{URL}";\n')
    elsewhere = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=elsewhere.serve_forever, daemon=True).start()

    with sync_playwright() as play:
        def open_browser():
            # No proxy of the machine the test runs on: this network has only the two addresses.
            browser = play.chromium.launch(executable_path=BROWSER, headless=True, args=[
                "--no-proxy-server", f"--host-resolver-rules=MAP {SITE} {STRANGER}"])
            page = browser.new_page(viewport={"width": 400, "height": 900})
            problems = []
            page.on("console", lambda message: problems.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: problems.append(str(error)))
            return browser, page, problems

        # 1. Without the brain's authority in the browser's rules, the start page cannot reach the brain.
        policy(False)
        browser, page, _ = open_browser()
        page.goto(f"file://{desktop}/panel.html")
        page.wait_for_function("() => document.getElementById('state').textContent.includes('does not answer')", timeout=20000)
        seen["untrusted"] = {"url": page.url.split("/")[-1], "state": page.inner_text("#state")}
        page.screenshot(path=str(WORK / "1-start-page-no-brain.png"))
        browser.close()

        # 2. With it, the start page finds the brain and becomes the panel.
        # (The panel allows no script but its own, so everything asked of the page here is asked as a function,
        # which the browser's own tooling may call; a line of script handed over as text would be refused.)
        policy(True)
        browser, page, problems = open_browser()
        page.goto(f"file://{desktop}/panel.html")
        page.wait_for_url(f"{URL}/panel/", timeout=20000)
        page.wait_for_function("() => document.getElementById('state-text').textContent === 'Ready.'", timeout=20000)
        seen["panel"] = {"url": page.url, "state": page.inner_text("#state-text"), "release": page.inner_text("#release"),
                         "up": page.inner_text("#up"), "load": page.inner_text("#load"), "model": page.inner_text("#model"),
                         "empty": page.is_visible("#empty"), "send_disabled": page.is_disabled("#send"),
                         "font": page.evaluate("() => document.fonts.check('15px Oxanium')"),
                         "vault": page.inner_text("#vault"), "cluster": page.inner_text("#cluster")}
        # The owner unlocks on the brain: the running service notices, and the panel shows it.
        (WORK / "run").mkdir()
        (WORK / "run" / "age.key").write_bytes((WORK / "keys" / "age.key").read_bytes())
        (WORK / "run" / "recipient").write_bytes((WORK / "keys" / "recipient").read_bytes())
        page.wait_for_function("() => document.getElementById('vault').textContent === 'unlocked'", timeout=60000)
        page.wait_for_function("() => document.getElementById('cluster').textContent === 'does not answer'", timeout=60000)
        seen["unlocked"] = {"vault": page.inner_text("#vault"), "cluster": page.inner_text("#cluster"),
                            "why": page.get_attribute("#cluster", "title")}
        page.screenshot(path=str(WORK / "2-panel-ready.png"))

        # 2b. The brain's authority is trusted for the brain's address and for nothing else it might sign.
        impostor("site", 8444)
        impostor("other", 8445)
        seen["authority_elsewhere"] = {}
        for name, url in (("a_web_sites_name", f"https://{SITE}:8444/"), ("another_address", f"https://{STRANGER}:8445/")):
            probe = browser.new_page()
            try:
                probe.goto(url, timeout=15000)
                seen["authority_elsewhere"][name] = "reached: " + probe.title()
            except Exception as refused:
                seen["authority_elsewhere"][name] = re.search(r"net::\w+", str(refused)).group(0)
            probe.close()

        # 3. A question and its answer.
        page.fill("#text", "How long has this machine been up?")
        page.press("#text", "Enter")
        page.wait_for_function("() => document.querySelector('.said.jarvis.done') !== null", timeout=20000)
        page.wait_for_function("() => document.getElementById('state-text').textContent === 'Ready.'")
        seen["answer"] = {"you": page.inner_text(".said.you"), "jarvis": page.inner_text(".said.jarvis"),
                          "box": page.input_value("#text"), "button": page.inner_text("#send")}
        page.screenshot(path=str(WORK / "3-panel-answer.png"))

        # 4. Shown again after the panel was opened anew (the desktop reopens a closed panel).
        page.reload()
        page.wait_for_function("() => document.querySelectorAll('.said').length === 2", timeout=20000)
        seen["after_reload"] = [page.inner_text(".said.you"), page.inner_text(".said.jarvis")]

        # 5. Stop, while the model is still thinking.
        (state / "chat-slow").write_text("30")
        page.fill("#text", "Tell me a very long story.")
        page.click("#send")
        page.wait_for_function("() => document.getElementById('send').textContent === 'Stop'")
        seen["while_answering"] = {"state": page.inner_text("#state-text"), "new_disabled": page.is_disabled("#new")}
        page.screenshot(path=str(WORK / "4-panel-thinking.png"))
        # A second window of the panel, opened meanwhile: it says where the answer is being written, cannot
        # talk over it, and catches up when it has ended.
        second = browser.new_page(viewport={"width": 400, "height": 900})
        second.goto(f"{URL}/panel/")
        second.wait_for_function("() => document.getElementById('state-text').textContent === 'Answering in another window.'", timeout=20000)
        second.fill("#text", "Me too")
        seen["second_window"] = {"said": second.locator(".said").count(), "send_disabled": second.is_disabled("#send"),
                                 "last": second.locator(".said.jarvis").last.get_attribute("class")}
        page.click("#send")
        page.wait_for_function("() => document.querySelector('.said.jarvis.stopped') !== null", timeout=10000)
        (state / "chat-slow").unlink()
        second.wait_for_function("() => document.getElementById('state-text').textContent === 'Ready.'", timeout=20000)
        seen["second_window"]["after"] = {"last": second.locator(".said.jarvis").last.get_attribute("class"),
                                          "note": second.inner_text(".said.jarvis.stopped .note"),
                                          "send_disabled": second.is_disabled("#send")}
        second.close()
        seen["stopped"] = {"state": page.inner_text("#state-text"), "note": page.inner_text(".said.jarvis.stopped .note")}
        for _ in range(50):   # the service notices the reader has gone and gives the turn up
            if records()[-1]["kind"] == "turn_aborted":
                break
            time.sleep(0.1)
        seen["stopped"]["audit"] = records()[-1]["kind"]
        page.screenshot(path=str(WORK / "5-panel-stopped.png"))

        # 6. The next question does not carry the stopped one along.
        page.fill("#text", "And now?")
        page.press("#text", "Enter")
        page.wait_for_function("() => document.querySelectorAll('.said.jarvis.done').length === 2", timeout=20000)

        # 7. A page from elsewhere, in the same browser.
        before = len(records())
        other = browser.new_page()
        other.goto(f"http://127.0.0.1:{elsewhere.server_address[1]}/")
        other.wait_for_function("() => document.title.startsWith('{')", timeout=20000)
        seen["elsewhere"] = json.loads(other.title())
        frame = other.frames[1] if len(other.frames) > 1 else None
        seen["elsewhere"]["frame_shows_panel"] = bool(frame and frame.url.startswith(URL) and frame.query_selector("#log"))
        seen["elsewhere"]["new_records"] = [r["kind"] for r in records()[before:]]
        other.close()

        # 8. A new conversation.
        page.click("#new")
        page.wait_for_function("() => document.querySelectorAll('.said').length === 0")
        seen["new"] = {"empty": page.is_visible("#empty"), "state": page.inner_text("#state-text")}

        # 9. The brain goes away and comes back.
        seen["service"].terminate()
        seen["service"].wait(timeout=20)
        page.wait_for_function("() => document.getElementById('state-text').textContent.includes('does not answer')", timeout=60000)
        seen["brain_away"] = {"state": page.inner_text("#state-text"), "send_disabled": page.is_disabled("#send")}
        page.screenshot(path=str(WORK / "6-panel-brain-away.png"))
        seen["service"] = start_service(state)
        page.wait_for_function("() => document.getElementById('state-text').textContent === 'Ready.'", timeout=30000)
        seen["brain_back"] = page.inner_text("#state-text")
        seen["problems"] = [text for text in problems if "ERR_CONNECTION_REFUSED" not in text and "Failed to load resource" not in text]
        browser.close()
    elsewhere.shutdown()


def main():
    state = WORK / "state"
    state.mkdir(parents=True)
    (state / "ollama-running").touch()
    (state / "ollama-models").write_text("qwen3:8b\n")
    network()
    certificates(WORK / "etc" / "tls")
    seen["ollama"] = PretendOllama(state).url
    # A vault for this brain, locked: the service starts without its identity, as after a restart.
    sys.path.insert(0, str(REPO / "tests" / "core"))
    from vaults import identity, vault_file
    _, public = identity(WORK / "keys")
    vault_file(WORK / "secrets" / "lab.enc.env", {"JARVIS_PVE_TOKEN_ID": "jarvis@pve!ro", "JARVIS_PVE_TOKEN_SECRET": "s" * 36,
                                                        "JARVIS_PVE_HOSTS": "198.51.100.11"}, public)
    (WORK / "etc" / "trust").mkdir(parents=True, exist_ok=True)
    (WORK / "etc" / "trust" / "proxmox-ca.pem").write_bytes((WORK / "etc" / "tls" / "ca.pem").read_bytes())
    seen["service"] = start_service(state)
    try:
        panel = {"Sec-Fetch-Site": "same-origin"}
        chat = json.dumps({"text": "Restart everything"})
        seen["programs"] = {
            # This machine pretending to be the desktop through a header a proxy would set.
            "self_naming_the_desktop": ask(BRAIN, "/api/state", headers=dict(panel, **{"X-Forwarded-For": DESKTOP, "X-Real-IP": DESKTOP}))[0],
            # What a page from elsewhere makes the desktop's browser send, as the brain sees it.
            "desktop_cross_site_post": ask(DESKTOP, "/api/chat", "POST", {"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1",
                                                                         "Content-Type": "text/plain"}, chat)[0],
            "desktop_cross_site_as_json": ask(DESKTOP, "/api/chat", "POST", {"Sec-Fetch-Site": "cross-site", "Origin": "http://127.0.0.1",
                                                                            "Content-Type": "application/json"}, chat)[0],
            "desktop_under_another_name": ask(DESKTOP, "/api/state", headers=dict(panel, Host="evil.example.test:8443"))[0],
            "desktop_as_panel": ask(DESKTOP, "/api/state", headers=panel)[0],
            "desktop_without_saying_so": ask(DESKTOP, "/api/state")[0],
            "stranger": ask(STRANGER, "/api/state", headers=panel)[0],
            "self_health": ask(BRAIN, "/api/health"),
            "self_state": ask(BRAIN, "/api/state", headers=panel)[0],
            "headers": None,
        }
        context = ssl.create_default_context(cafile=str(WORK / "etc" / "tls" / "ca.pem"))
        connection = http.client.HTTPSConnection(BRAIN, PORT, context=context, source_address=(DESKTOP, 0), timeout=10)
        connection.request("GET", "/panel/", headers={"Sec-Fetch-Site": "none"})
        answer = connection.getresponse()
        seen["programs"]["headers"] = {key.lower(): value for key, value in answer.getheaders()}
        seen["programs"]["page_has_title"] = bool(re.search(r"<title>Jarvis</title>", answer.read().decode()))
        connection.close()
        browse(state)
    finally:
        seen["service"].terminate()
        seen["service"].wait(timeout=20)
    seen["kinds"] = [record["kind"] for record in records()]
    seen["startup"] = next(record["data"] for record in records() if record["kind"] == "startup")
    del seen["service"]
    print(json.dumps(seen))


main()
