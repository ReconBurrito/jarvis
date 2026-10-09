import base64
import hashlib
import json
import ssl
from pathlib import Path

import httpx
from fakes import run

from jarvis.adapters.common import trust_context
from jarvis.adapters.opnsense import OpnsenseClient, make_opnsense_tools

KEY = "test-api-key-not-real"
SECRET = "test-api-secret-not-real"

ANSWERS = {
    "/api/diagnostics/system/system_information": {"name": "firewall.lab.example", "versions": ["OPNsense 25.1-amd64", "FreeBSD 14.2"]},
    "/api/diagnostics/system/system_time": {"uptime": "3 days, 04:05:06", "loadavg": "0.21, 0.30, 0.28", "config": "Wed Jan  1 12:00:00 UTC 2025"},
    "/api/diagnostics/system/system_resources": {"memory": {"total": "8589934592", "used": "2147483648"}},
    "/api/diagnostics/system/system_disk": {"devices": [{"mountpoint": "/", "used_pct": 12, "device": "zroot/ROOT/default"}]},
    "/api/diagnostics/system/system_temperature": [{"device": "cpu0", "temperature": "45.0"}, {"device": "cpu1", "temperature": "47.5"}],
    "/api/diagnostics/firewall/pf_states": {"current": "1234", "limit": "810000"},
}


class FakeOpnsense:
    def __init__(self, answers=None, fail=()):
        self.requests = []
        self.answers = dict(ANSWERS if answers is None else answers)
        self.fail = set(fail)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        expected = "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401)
        if request.url.path in self.fail:
            return httpx.Response(403)
        if request.url.path not in self.answers:
            return httpx.Response(404)
        return httpx.Response(200, json=self.answers[request.url.path])

    def tool(self, secret=SECRET):
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return make_opnsense_tools(OpnsenseClient(["192.0.2.1=firewall.lab.example"], KEY, secret, http=http))[0]


def test_status_is_assembled_from_the_dashboard_endpoints():
    fake = FakeOpnsense()
    result = run(fake.tool().handler({}))
    assert result == {
        "name": "firewall.lab.example",
        "versions": ["OPNsense 25.1-amd64", "FreeBSD 14.2"],
        "uptime": "3 days, 04:05:06",
        "load_average": "0.21, 0.30, 0.28",
        "last_config_change": "Wed Jan  1 12:00:00 UTC 2025",
        "memory_used_percent": 25.0,
        "disks": [{"mountpoint": "/", "used_percent": 12.0}],
        "hottest_sensor_c": 47.5,
        "firewall_states": {"current": 1234.0, "limit": 810000.0},
    }
    assert all(r.method == "GET" and r.url.port in (None, 443) and r.url.host == "192.0.2.1" for r in fake.requests)
    assert all(r.url.path.startswith("/api/diagnostics/") for r in fake.requests)
    assert fake.requests[0].extensions.get("sni_hostname") == "firewall.lab.example"


def test_unexpected_shapes_are_left_out_not_fatal():
    odd = dict(ANSWERS)
    odd["/api/diagnostics/system/system_resources"] = {"memory": "n/a"}
    odd["/api/diagnostics/system/system_disk"] = {"devices": "none"}
    odd["/api/diagnostics/system/system_temperature"] = []
    result = run(FakeOpnsense(odd).tool().handler({}))
    assert result["name"] == "firewall.lab.example"
    assert "memory_used_percent" not in result and "disks" not in result and "hottest_sensor_c" not in result
    assert "unavailable" not in result


def test_a_section_the_key_may_not_read_is_reported():
    result = run(FakeOpnsense(fail={"/api/diagnostics/firewall/pf_states"}).tool().handler({}))
    assert "firewall_states" not in result
    assert result["unavailable"] == ["states: 192.0.2.1 refused the read-only token (403)"]


def test_bad_credentials_give_one_plain_error():
    fake = FakeOpnsense()
    result = run(fake.tool(secret="wrong").handler({}))
    assert result == {"error": "192.0.2.1 refused the read-only token (401)"}
    assert len(fake.requests) == 1
    assert "wrong" not in json.dumps(result) and KEY not in json.dumps(result)



