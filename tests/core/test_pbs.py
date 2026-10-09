import datetime
import ipaddress
import json
import socket
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
from fakes import run
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from jarvis.adapters.common import AdapterError
from jarvis.adapters.pbs import PbsClient, make_pbs_tools

SECRET = "99999999-8888-7777-6666-555555555555"
TOKEN_ID = "jarvis@pbs!ro"
NOW = 1_800_000_000.0
HOUR = 3600

USAGE = [{"store": "backups", "total": 1000 * 2**30, "used": 300 * 2**30, "avail": 700 * 2**30, "history": [0.1, 0.2]}]
GROUPS = [
    {"backup-type": "ct", "backup-id": "211", "last-backup": NOW - 11 * HOUR, "backup-count": 30, "owner": "backup@pbs!node"},
    {"backup-type": "vm", "backup-id": "212", "last-backup": NOW - 12 * HOUR, "backup-count": 28},
    {"backup-type": "ct", "backup-id": "214", "last-backup": NOW - 200 * HOUR, "backup-count": 4},
]
APT_UPID = "UPID:pbs:0000A1B2:00C3D4E5:00000012:6B49D200:aptupdate::root@pam:"
TASKS = [
    {"upid": "UPID:pbs:0000A1B1:00C3D4E4:00000011:6B49D1FF:backup:backups\\x3act-214:backup@pbs!node:", "worker_type": "backup",
     "worker_id": "backups:ct/214", "starttime": NOW - 30 * HOUR, "status": "ERROR: connection reset " + "x" * 300},
    {"upid": APT_UPID, "worker_type": "aptupdate", "worker_id": None, "starttime": NOW - 5 * HOUR, "status": "WARNINGS: 1"},
    {"upid": "not a upid", "worker_type": "prune", "worker_id": "backups", "starttime": NOW - 50 * HOUR, "status": "ERROR: x"},
    {"upid": "UPID:pbs:0000A1B0:00C3D4E3:00000010:6B49D1FE:verify:backups:root@pam:", "worker_type": "verify", "worker_id": "backups",
     "starttime": NOW - 80 * HOUR, "status": "ERROR: old"},
]
APT_LOG = [{"n": n + 1, "t": f"Hit:{n} http://deb.debian.org/debian trixie InRelease"} for n in range(20)] + [
    {"n": 21, "t": "Err:21 https://mirror.example/debian trixie InRelease"},
    {"n": 22, "t": "  404  Not Found [IP: 203.0.113.9 443]"},
    {"n": 23, "t": ""},
    {"n": 24, "t": "E: Failed to fetch https://mirror.example/debian/dists/trixie/InRelease  404  Not Found " + "y" * 300},
    {"n": 25, "t": "TASK WARNINGS: 1"},
]


class FakePbs:
    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"PBSAPIToken={TOKEN_ID}:{SECRET}":
            return httpx.Response(401)
        path = request.url.path
        if path == "/api2/json/status/datastore-usage":
            data = USAGE
        elif path == "/api2/json/admin/datastore/backups/groups":
            data = GROUPS
        elif path == "/api2/json/nodes/localhost/tasks":
            data = TASKS
        elif request.url.raw_path.split(b"?")[0] == b"/api2/json/nodes/localhost/tasks/" + APT_UPID.replace(":", "%3A").replace("@", "%40").encode() + b"/log":
            data = APT_LOG
        elif path.startswith("/api2/json/nodes/localhost/tasks/") and path.endswith("/log"):
            return httpx.Response(403)
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"data": data})

    def tools(self, secret=SECRET):
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        client = PbsClient(["192.0.2.40=pbs.lab.example"], TOKEN_ID, secret, http=http)
        return {t.name: t for t in make_pbs_tools(client, clock=lambda: NOW)}


def test_datastores():
    fake = FakePbs()
    result = run(fake.tools()["pbs_datastores"].handler({}))
    assert result == {"datastores": [{"datastore": "backups", "used_percent": 30.0, "total_gib": 1000.0, "free_gib": 700.0}]}
    request = fake.requests[0]
    assert request.method == "GET" and request.url.port == 8007 and request.url.host == "192.0.2.40"
    assert request.extensions.get("sni_hostname") == "pbs.lab.example"


def test_backups_reports_fresh_and_stale():
    result = run(FakePbs().tools()["pbs_backups"].handler({}))["datastores"][0]
    assert result["guests_with_backups"] == 3 and result["fresh"] == 2
    assert result["older_than_limit"] == ["ct/214"] and result["limit_hours"] == 36
    assert result["newest_hours_ago"] == 11.0
    assert result["groups"][0] == {"guest": "ct/211", "last_backup_hours_ago": 11.0, "snapshots": 30}
    tight = run(FakePbs().tools()["pbs_backups"].handler({"max_age_hours": 11.5, "datastore": "backups"}))["datastores"][0]
    assert tight["older_than_limit"] == ["vm/212", "ct/214"]


def test_backups_checks_the_datastore_name():
    fake = FakePbs()
    tools = fake.tools()
    for bad in ("../../access", "backups/../x", "a b"):
        assert "error" in run(tools["pbs_backups"].handler({"datastore": bad}))
    assert fake.requests == []
    unknown = run(tools["pbs_backups"].handler({"datastore": "nope"}))
    assert "unknown datastore" in unknown["error"] and "backups" in unknown["error"]
    assert "error" in run(tools["pbs_backups"].handler({"max_age_hours": "soon"}))


def test_failed_tasks_are_trimmed_and_bounded():
    fake = FakePbs()
    result = run(fake.tools()["pbs_failed_tasks"].handler({"days": 400}))
    assert result["days"] == 30 and result["failed_count"] == 4
    task = result["failed"][0]
    assert task["task"] == "backup" and task["target"] == "backups:ct/214" and task["started_hours_ago"] == 30.0
    assert len(task["result"]) == 160
    params = fake.requests[0].url.params
    assert params["errors"] == "true" and params["limit"] == "20" and params["since"] == str(int(NOW - 30 * 86400))


def test_failed_tasks_carry_the_end_of_their_own_log():
    fake = FakePbs()
    failed = run(fake.tools()["pbs_failed_tasks"].handler({}))["failed"]
    apt = next(t for t in failed if t["task"] == "aptupdate")
    assert apt["result"] == "WARNINGS: 1" and len(apt["log_end"]) == 12, "the last twelve lines that say something"
    assert apt["log_end"][-1] == "TASK WARNINGS: 1" and apt["log_end"][-3] == "  404  Not Found [IP: 203.0.113.9 443]"
    assert apt["log_end"][-2].startswith("E: Failed to fetch https://mirror.example") and len(apt["log_end"][-2]) == 200
    # A log the token may not read, and a task without a usable id, say so instead of failing the whole answer.
    assert "could not be read" in failed[0]["log_end"] and "refused the read-only token (403)" in failed[0]["log_end"]
    assert failed[2]["log_end"] == "the log could not be read: the task has no usable id"
    assert "log_end" not in failed[3], "only the newest three are read"
    logs = [r for r in fake.requests if r.url.path.endswith("/log")]
    assert len(logs) == 2 and all(r.method == "GET" and r.url.params["limit"] == "500" for r in logs)
    assert b"/tasks/UPID%3Apbs%3A" in logs[0].url.raw_path, "the id is one path segment, whatever it contains"


def test_refused_token_is_a_plain_error():
    result = run(FakePbs().tools(secret="wrong")["pbs_datastores"].handler({}))
    assert "refused the read-only token (401)" in result["error"] and "wrong" not in json.dumps(result)






def pbs_shaped_cert():
    """Self-signed, CA:FALSE, server-auth only, names but no address: the shape of a default PBS certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Proxmox Backup Server"),
                      x509.NameAttribute(NameOID.COMMON_NAME, "pbs.lab.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost"),
            x509.DNSName("pbs"), x509.DNSName("pbs.lab.example")]), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return cert.public_bytes(pem), key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def test_pinned_self_signed_certificate_is_verified_for_real(tmp_path):
    cert_pem, key_pem = pbs_shaped_cert()
    (tmp_path / "pbs.pem").write_bytes(cert_pem)
    (tmp_path / "pbs.key").write_bytes(key_pem)
    impostor_pem, _ = pbs_shaped_cert()
    (tmp_path / "impostor.pem").write_bytes(impostor_pem)

    async def api(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps({"data": USAGE}).encode()})

    sock = socket.socket()
    sock.bind(("127.0.0.2", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, ssl_certfile=str(tmp_path / "pbs.pem"), ssl_keyfile=str(tmp_path / "pbs.key"), log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            threading.Event().wait(0.05)

        # 127.0.0.2 is not in the certificate, as a self-signed one seldom names an address.
        pinned = PbsClient(["127.0.0.2=pbs.lab.example"], TOKEN_ID, SECRET, cert_file=tmp_path / "pbs.pem", port=port)
        assert run(pinned.store_names()) == ["backups"]

        by_address = PbsClient(["127.0.0.2"], TOKEN_ID, SECRET, cert_file=tmp_path / "pbs.pem", port=port)
        with pytest.raises(AdapterError, match="ConnectError"):
            run(by_address.store_names())

        # Same names, different key: what a machine in the middle would present.
        other_pin = PbsClient(["127.0.0.2=pbs.lab.example"], TOKEN_ID, SECRET, cert_file=tmp_path / "impostor.pem", port=port)
        with pytest.raises(AdapterError, match="ConnectError"):
            run(other_pin.store_names())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
