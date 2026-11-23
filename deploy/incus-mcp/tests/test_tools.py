import pytest

from incus_mcp.config import Config, ConfigError
from incus_mcp.incus import IncusClient, IncusError
from incus_mcp.tools import IncusTools, ToolInputError, truncate

from .fake_incus import FakeIncus


@pytest.fixture
def fake():
    f = FakeIncus()
    f.add("web1")
    f.add("db1", status="Stopped")
    return f


@pytest.fixture
def cfg():
    return Config(audit_log=None, output_limit=2048, exec_timeout=30)


@pytest.fixture
def tools(cfg, fake):
    return IncusTools(cfg, IncusClient(cfg, transport=fake.transport()))


async def test_every_request_is_pinned_to_the_project(tools, fake):
    await tools.instance_list()
    await tools.instance_get("web1")
    # Even a caller that tries to switch project is overridden.
    await tools.incus.call("GET", "/1.0/instances", params={"project": "default"})
    assert fake.requests and all(r.url.params.get_list("project") == ["timon"] for r in fake.requests)


async def test_other_project_is_refused_by_incus():
    other = FakeIncus(project="default")  # simulates the restricted certificate on another project
    cfg = Config(audit_log=None)
    t = IncusTools(cfg, IncusClient(cfg, transport=other.transport()))
    with pytest.raises(IncusError, match="restricted"):
        await t.instance_list()


async def test_list_and_get_summarise(tools):
    listing = await tools.instance_list()
    web = next(i for i in listing["instances"] if i["name"] == "web1")
    assert web["ipv4"] == ["10.0.0.5"] and web["cpu"] == "2" and web["user"] == {"user.owner": "test"}
    detail = await tools.instance_get("web1")
    assert "volatile.eth0.hwaddr" not in detail["config"]
    assert detail["processes"] == 12


async def test_create_with_remote_image_and_limits(tools, fake):
    result = await tools.instance_create("app1", "images:debian/13/cloud", cpu=2, memory="2GiB", cloud_init_user_data="#cloud-config\n")
    assert result["status"] == "Running"
    inst = fake.instances["app1"]
    assert inst["source"] == {"type": "image", "alias": "debian/13/cloud", "server": "https://images.linuxcontainers.org", "protocol": "simplestreams"}
    assert inst["config"]["limits.memory"] == "2GiB" and inst["config"]["limits.cpu"] == "2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "1bad", "image": "images:debian/13"},
        {"name": "ok", "image": "images:../../etc"},
        {"name": "ok", "image": "images:debian/13", "memory": "lots"},
        {"name": "ok", "image": "images:debian/13", "cpu": 0},
        {"name": "a/b", "image": "images:debian/13"},
    ],
)
async def test_create_rejects_bad_input(tools, fake, kwargs):
    with pytest.raises(ToolInputError):
        await tools.instance_create(**kwargs)
    assert not any(r.method == "POST" for r in fake.requests)


async def test_incus_errors_surface(tools):
    with pytest.raises(IncusError, match="already exists"):
        await tools.instance_create("web1", "images:debian/13")
    with pytest.raises(IncusError, match="not found"):
        await tools.instance_get("ghost")


async def test_stop_start_restart(tools, fake):
    await tools.instance_stop("web1")
    assert fake.instances["web1"]["status"] == "Stopped"
    await tools.instance_start("web1")
    await tools.instance_restart("web1")
    assert fake.instances["web1"]["status"] == "Running"


async def test_delete_requires_force_when_running(tools, fake):
    with pytest.raises(ToolInputError, match="force"):
        await tools.instance_delete("web1")
    assert "web1" in fake.instances
    await tools.instance_delete("web1", force=True)
    assert "web1" not in fake.instances
    await tools.instance_delete("db1")
    assert "db1" not in fake.instances


async def test_set_config_only_allows_user_and_limits(tools, fake):
    await tools.instance_set_config("web1", {"user.team": "infra", "limits.memory": "1GiB", "user.owner": None})
    assert fake.instances["web1"]["config"]["user.team"] == "infra"
    assert fake.instances["web1"]["config"]["user.owner"] == ""
    for key in ("security.privileged", "raw.lxc", "limits.memory.hugepages", "user.bad key"):
        with pytest.raises(ToolInputError):
            await tools.instance_set_config("web1", {key: "true"})


async def test_snapshots(tools, fake):
    created = await tools.snapshot_create("web1")
    assert created["snapshot"].startswith("snap-")
    await tools.snapshot_create("web1", "before-upgrade")
    assert [s["name"] for s in (await tools.snapshot_list("web1"))["snapshots"]] == [created["snapshot"], "before-upgrade"]
    await tools.snapshot_restore("web1", "before-upgrade")
    with pytest.raises(IncusError, match="Snapshot not found"):
        await tools.snapshot_restore("web1", "nope")


async def test_exec_returns_output_and_cleans_logs(tools, fake):
    fake.exec_result = {"return": 3, "stdout": "x" * 10_000, "stderr": "warning\n"}
    result = await tools.instance_exec("web1", ["sh", "-c", "echo hi"], cwd="/root", env={"LANG": "C"})
    assert result["exit_code"] == 3 and result["stderr"] == "warning\n"
    assert len(result["stdout"]) < 2200 and "characters cut" in result["stdout"]
    assert fake.files == {}  # both output logs were deleted
    assert fake.last_exec["record-output"] is True and fake.last_exec["interactive"] is False


async def test_exec_timeout_reports_operation(tools, fake):
    fake.exec_runs_forever = True
    result = await tools.instance_exec("web1", ["sleep", "999"], timeout=999)
    assert result["status"] == "timeout" and result["timeout_s"] == 30  # capped by INCUS_MCP_EXEC_TIMEOUT
    op = await tools.operation_get(result["operation"])
    assert op["status_code"] == 103


@pytest.mark.parametrize("bad", [[], "ls", [1, 2]])
async def test_exec_rejects_bad_commands(tools, bad):
    with pytest.raises(ToolInputError):
        await tools.instance_exec("web1", bad)


async def test_logs_tail(tools):
    logs = await tools.instance_logs("web1", lines=3)
    assert logs["console_log"].splitlines() == ["boot line 498", "boot line 499", "boot line 500"]


async def test_images(tools):
    images = await tools.image_list()
    assert images["cached_images"][0]["aliases"] == ["debian/13"]
    assert "images:debian/13" in images["remote"]


def test_truncate_keeps_head_and_tail():
    text = "A" * 100 + "B" * 100
    cut = truncate(text, 50)
    assert cut.startswith("A" * 25) and cut.endswith("B" * 25)
    assert truncate("short", 50) == "short"


def test_config_from_env(tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 40)
    cfg = Config.from_env({"INCUS_MCP_TOKEN_FILE": str(token), "INCUS_MCP_ALLOWED_NETWORKS": "192.168.1.0/24, 10.0.0.0/8", "INCUS_MCP_AUDIT_LOG": "off"})
    assert len(cfg.allowed_networks) == 2 and cfg.audit_log is None
    assert cfg.read_token() == "t" * 40
    with pytest.raises(ConfigError, match="default"):
        Config.from_env({"INCUS_MCP_PROJECT": "default"})
    token.write_text("short")
    with pytest.raises(ConfigError, match="shorter"):
        cfg.read_token()
