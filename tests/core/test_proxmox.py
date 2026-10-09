"""The read-only Proxmox tools, against a played API and, for the certificate checks, a real TLS server."""
import json

import httpx
import pytest
from fakes import run

from jarvis.adapters.proxmox import ProxmoxClient, ProxmoxError, make_proxmox_tools

SECRET = "11111111-2222-3333-4444-555555555555"
TOKEN_ID = "jarvis@pve!ro"

CLUSTER_STATUS = [
    {"type": "cluster", "name": "lab", "nodes": 2, "quorate": 1, "version": 4},
    {"type": "node", "name": "pve1", "online": 1, "ip": "192.0.2.11", "nodeid": 1, "local": 1},
    {"type": "node", "name": "pve2", "online": 1, "ip": "192.0.2.12", "nodeid": 2, "local": 0},
]
NODES = [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.18, "maxcpu": 32, "mem": 60 * 2**30, "maxmem": 96 * 2**30, "uptime": 360000},
    {"type": "node", "node": "pve2", "status": "online", "cpu": 0.09, "maxcpu": 8, "mem": 14 * 2**30, "maxmem": 32 * 2**30, "uptime": 7200},
]
VMS = [
    {"type": "lxc", "vmid": 211, "name": "proxy", "node": "pve1", "status": "running", "mem": 2**29, "maxmem": 2**30, "template": 0},
    {"type": "qemu", "vmid": 212, "name": "backup", "node": "pve1", "status": "running", "mem": 2**31, "maxmem": 2**32, "template": 0},
    {"type": "lxc", "vmid": 213, "name": "dns2", "node": "pve2", "status": "stopped", "mem": 0, "maxmem": 2**30, "template": 0},
    {"type": "qemu", "vmid": 900, "name": "tmpl", "node": "pve2", "status": "stopped", "template": 1},
]
STORAGE = [
    {"type": "storage", "storage": "local-lvm", "node": "pve1", "status": "available", "disk": 50 * 2**30, "maxdisk": 200 * 2**30, "plugintype": "lvmthin", "shared": 0},
    {"type": "storage", "storage": "pbs", "node": "pve1", "status": "available", "disk": 300 * 2**30, "maxdisk": 1000 * 2**30, "plugintype": "pbs", "shared": 1},
    {"type": "storage", "storage": "pbs", "node": "pve2", "status": "available", "disk": 300 * 2**30, "maxdisk": 1000 * 2**30, "plugintype": "pbs", "shared": 1},
]
NODE_STATUS = {
    "cpu": 0.18, "loadavg": ["0.52", "0.40", "0.31"], "uptime": 360000,
    "memory": {"total": 96 * 2**30, "used": 60 * 2**30, "free": 36 * 2**30},
    "rootfs": {"total": 100 * 2**30, "used": 25 * 2**30},
    "pveversion": "pve-manager/9.0.3/abcdef", "kversion": "Linux 6.8.12-4-pve",
    "current-kernel": {"release": "6.8.12-4-pve"}, "cpuinfo": {"model": "Example CPU", "cpus": 16},
}


class FakePve:
    def __init__(self, down=(), status=200):
        self.requests = []
        self.down = set(down)
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in self.down:
            raise httpx.ConnectTimeout("timed out", request=request)
        if request.headers.get("authorization") != f"PVEAPIToken={TOKEN_ID}={SECRET}":
            return httpx.Response(401)
        if self.status != 200:
            return httpx.Response(self.status)
        path, kind = request.url.path, request.url.params.get("type")
        if path == "/api2/json/cluster/status":
            data = CLUSTER_STATUS
        elif path == "/api2/json/cluster/resources":
            data = {"node": NODES, "vm": VMS, "storage": STORAGE}[kind]
        elif path == "/api2/json/nodes/pve1/status":
            data = NODE_STATUS
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"data": data})

    def client(self, hosts=("192.0.2.11", "192.0.2.12"), secret=SECRET):
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return ProxmoxClient(list(hosts), TOKEN_ID, secret, http=http)


def tools_for(fake, **kwargs):
    return {t.name: t for t in make_proxmox_tools(fake.client(**kwargs))}


def test_cluster_status():
    fake = FakePve()
    result = run(tools_for(fake)["proxmox_cluster_status"].handler({}))
    assert result["cluster"] == "lab" and result["quorate"] is True
    first = result["nodes"][0]
    assert first == {"node": "pve1", "online": True, "cpu_percent": 18.0, "cpu_count": 32,
                     "memory_percent": 62.5, "memory_total_gib": 96.0, "uptime_hours": 100.0}
    assert all(r.method == "GET" for r in fake.requests)
    assert all(r.url.scheme == "https" and r.url.port == 8006 for r in fake.requests)


def test_guests_with_filters_and_no_templates():
    tools = tools_for(FakePve())
    everything = run(tools["proxmox_guests"].handler({}))
    assert everything["count"] == 3 and everything["running"] == 2 and everything["stopped"] == 1
    assert [g["id"] for g in everything["guests"]] == [211, 212, 213]
    assert everything["guests"][0] == {"id": 211, "name": "proxy", "kind": "container", "node": "pve1",
                                       "status": "running", "memory_percent": 50.0}
    stopped = run(tools["proxmox_guests"].handler({"status": "stopped"}))
    assert [g["name"] for g in stopped["guests"]] == ["dns2"]
    on_two = run(tools["proxmox_guests"].handler({"node": "pve2"}))
    assert on_two["count"] == 1
    assert "error" in run(tools["proxmox_guests"].handler({"status": "deleted"}))


def test_node_status_and_name_checks():
    fake = FakePve()
    tools = tools_for(fake)
    good = run(tools["proxmox_node_status"].handler({"node": "pve1"}))
    assert good["memory_percent"] == 62.5 and good["root_disk_percent"] == 25.0
    assert good["kernel"] == "6.8.12-4-pve" and good["pve_version"].startswith("pve-manager/9.0.3")
    before = len(fake.requests)
    for bad in ("../access/users", "pve1/../../access", "", "a b", "x" * 80):
        assert "error" in run(tools["proxmox_node_status"].handler({"node": bad}))
    assert len(fake.requests) == before, "a malformed name must never reach the API"
    unknown = run(tools["proxmox_node_status"].handler({"node": "pmx99"}))
    assert "unknown node" in unknown["error"] and "pve2" in unknown["error"]


def test_storage_lists_shared_pools_once():
    result = run(tools_for(FakePve())["proxmox_storage"].handler({}))
    assert [(s["storage"], s["node"]) for s in result["storages"]] == [("local-lvm", "pve1"), ("pbs", "all nodes")]
    assert result["storages"][0]["used_percent"] == 25.0 and result["storages"][1]["total_gib"] == 1000.0


def test_fails_over_to_the_second_node():
    fake = FakePve(down={"192.0.2.11"})
    result = run(tools_for(fake)["proxmox_cluster_status"].handler({}))
    assert result["quorate"] is True
    assert {r.url.host for r in fake.requests} == {"192.0.2.11", "192.0.2.12"}


def test_errors_are_plain_and_never_show_the_token():
    all_down = run(tools_for(FakePve(down={"192.0.2.11", "192.0.2.12"}))["proxmox_storage"].handler({}))
    refused = run(tools_for(FakePve(), secret="wrong")["proxmox_storage"].handler({}))
    broken = run(tools_for(FakePve(status=500))["proxmox_storage"].handler({}))
    assert "no Proxmox node answered" in all_down["error"] and "ConnectTimeout" in all_down["error"]
    assert "refused the read-only token (401)" in refused["error"]
    assert "HTTP 500" in broken["error"]
    for result in (all_down, refused, broken):
        assert SECRET not in json.dumps(result) and "wrong" not in json.dumps(result)


def test_client_has_no_way_to_write_and_refuses_unverified_tls():
    client = FakePve().client()
    assert not any(hasattr(client, verb) for verb in ("post", "put", "delete", "patch", "request"))
    with pytest.raises(ValueError, match="CA file"):
        ProxmoxClient(["192.0.2.11"], TOKEN_ID, SECRET)
    with pytest.raises(ValueError):
        ProxmoxClient([], TOKEN_ID, SECRET, http=httpx.AsyncClient())


def test_host_entries_can_name_the_certificate():
    fake = FakePve()
    client = fake.client(hosts=("192.0.2.11=pve1", "192.0.2.12"))
    assert client.targets == [("192.0.2.11", "pve1"), ("192.0.2.12", None)]
    run(client.get("/cluster/status"))
    assert fake.requests[0].url.host == "192.0.2.11"
    assert fake.requests[0].extensions.get("sni_hostname") == "pve1"


def test_named_certificate_is_verified_for_real(tmp_path):
    """A real TLS server whose certificate lists a name but not its address, as on a node renumbered after its certificate was made."""
    import datetime
    import ipaddress
    import socket
    import threading

    import uvicorn
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def make(subject, issuer_cert, issuer_key, sans=None, ca=False):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
        now = datetime.datetime.now(datetime.timezone.utc)
        builder = (
            x509.CertificateBuilder().subject_name(name)
            .issuer_name(issuer_cert.subject if issuer_cert else name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key((issuer_key or key).public_key()), critical=False
            )
            .add_extension(
                x509.KeyUsage(digital_signature=True, key_cert_sign=ca, crl_sign=ca, content_commitment=False,
                              key_encipherment=False, data_encipherment=False, key_agreement=False,
                              encipher_only=False, decipher_only=False),
                critical=True,
            )
        )
        if sans:
            builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        return builder.sign(issuer_key or key, hashes.SHA256()), key

    ca_cert, ca_key = make("test cluster CA", None, None, ca=True)
    # A certificate that names the node and an address other than the one in use.
    node_cert, node_key = make("node", ca_cert, ca_key, sans=[x509.DNSName("pve1"), x509.IPAddress(ipaddress.ip_address("192.0.2.66"))])
    pem = serialization.Encoding.PEM
    (tmp_path / "ca.pem").write_bytes(ca_cert.public_bytes(pem))
    (tmp_path / "node.pem").write_bytes(node_cert.public_bytes(pem))
    (tmp_path / "node.key").write_bytes(node_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    async def api(scope, receive, send):
        if scope["type"] != "http":
            return
        body = json.dumps({"data": CLUSTER_STATUS}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(api, ssl_certfile=str(tmp_path / "node.pem"), ssl_keyfile=str(tmp_path / "node.key"), log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            threading.Event().wait(0.05)

        by_name = ProxmoxClient(["127.0.0.1=pve1"], TOKEN_ID, SECRET, ca_file=tmp_path / "ca.pem", port=port)
        assert run(by_name.get("/cluster/status"))[0]["name"] == "lab"

        by_address = ProxmoxClient(["127.0.0.1"], TOKEN_ID, SECRET, ca_file=tmp_path / "ca.pem", port=port)
        with pytest.raises(ProxmoxError, match="ConnectError"):
            run(by_address.get("/cluster/status"))

        wrong_name = ProxmoxClient(["127.0.0.1=pve2"], TOKEN_ID, SECRET, ca_file=tmp_path / "ca.pem", port=port)
        with pytest.raises(ProxmoxError, match="ConnectError"):
            run(wrong_name.get("/cluster/status"))

        other_ca, _ = make("another CA", None, None, ca=True)
        (tmp_path / "other.pem").write_bytes(other_ca.public_bytes(pem))
        wrong_ca = ProxmoxClient(["127.0.0.1=pve1"], TOKEN_ID, SECRET, ca_file=tmp_path / "other.pem", port=port)
        with pytest.raises(ProxmoxError, match="ConnectError"):
            run(wrong_ca.get("/cluster/status"))
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_an_ipv6_host_is_written_as_a_url_needs_it_and_a_bad_one_does_not_stop_the_next():
    fake = FakePve(down={"[2001:db8::1]"})
    client = fake.client(hosts=("2001:db8::1", "192.0.2.12"))
    assert client.targets[0] == ("[2001:db8::1]", None)
    assert run(client.get("/cluster/status"))[0]["name"] == "lab"
    broken = fake.client(hosts=("bad host name", "192.0.2.12"))
    assert run(broken.get("/cluster/status"))[0]["name"] == "lab"
