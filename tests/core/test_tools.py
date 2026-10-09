from fakes import FakeOllama, run, audit  # noqa: F401 (a fixture)

from jarvis.tools import TIER_HIGH_RISK, TIER_READ_ONLY, Tool, ToolRegistry, make_local_status


def make(name, tier, handler):
    return Tool(name, "d", {"type": "object", "properties": {}}, tier, handler)


async def ok(_):
    return {"value": 1}


async def boom(_):
    raise RuntimeError("broken")


def kinds(audit):
    return [line.split('"kind":"')[1].split('"')[0] for line in audit.path.read_text().splitlines()]


def test_read_only_tool_runs_and_is_audited(audit):
    reg = ToolRegistry(audit)
    reg.register(make("status", TIER_READ_ONLY, ok))
    assert run(reg.dispatch("s", "status", {})) == {"value": 1}
    assert kinds(audit) == ["tool_call"]


def test_higher_tier_is_hidden_and_refused(audit):
    reg = ToolRegistry(audit)
    reg.register(make("status", TIER_READ_ONLY, ok))
    reg.register(make("firewall_block", TIER_HIGH_RISK, ok))
    assert [s["function"]["name"] for s in reg.schemas()] == ["status"]
    result = run(reg.dispatch("s", "firewall_block", {}))
    assert "error" in result and "approval" in result["error"]
    assert kinds(audit) == ["tool_denied"]


def test_unknown_tool_is_refused(audit):
    result = run(ToolRegistry(audit).dispatch("s", "rm_rf", {}))
    assert result == {"error": "unknown tool: rm_rf"}
    assert kinds(audit) == ["tool_denied"]


def test_failing_tool_returns_error(audit):
    reg = ToolRegistry(audit)
    reg.register(make("bad", TIER_READ_ONLY, boom))
    assert run(reg.dispatch("s", "bad", "not a dict")) == {"error": "RuntimeError: broken"}
    assert '"ok":false' in audit.path.read_text()


def test_local_status_reads_ollama(audit):
    fake = FakeOllama(ps={"models": [{"name": "qwen3:8b", "size": 6_300_000_000, "size_vram": 6_300_000_000}]})

    async def go():
        async with fake.client() as client:
            return await make_local_status("http://ollama", client).handler({})

    result = run(go())
    assert result["ollama"] == {"up": True, "loaded_models": [{"name": "qwen3:8b", "size_gb": 6.3, "on_gpu_percent": 100}]}
    assert result["uptime_hours"] >= 0 and "gpu" in result


def test_local_status_survives_ollama_down(audit):
    fake = FakeOllama(down=True)

    async def go():
        async with fake.client() as client:
            return await make_local_status("http://ollama", client).handler({})

    assert run(go())["ollama"] == {"up": False, "error": "ConnectError"}
