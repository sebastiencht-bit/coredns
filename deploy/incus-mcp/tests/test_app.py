"""End to end: a real uvicorn server on 127.0.0.1, the official MCP client, the fake Incus behind."""

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from incus_mcp.app import build_app
from incus_mcp.config import Config, _networks
from incus_mcp.incus import IncusClient

from .fake_incus import FakeIncus

TOKEN = "k" * 48


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve(cfg: Config, fake: FakeIncus):
    app = build_app(cfg, TOKEN, IncusClient(cfg, transport=fake.transport()))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cfg.listen_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{cfg.listen_port}"
    finally:
        server.should_exit = True
        thread.join(5)


@pytest.fixture
def fake():
    f = FakeIncus()
    f.add("web1")
    return f


@pytest.fixture
def cfg(tmp_path):
    return Config(
        listen_host="127.0.0.1",
        listen_port=free_port(),
        allowed_networks=_networks("127.0.0.0/8"),
        audit_log=str(tmp_path / "audit.log"),
    )


def client(url: str, token: str = TOKEN) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return Client(streamable_http_client(f"{url}/mcp", http_client=http))


def text_of(result) -> dict:
    return json.loads(result.content[0].text)


def test_health_needs_lan_only(cfg, fake):
    with serve(cfg, fake) as url:
        r = httpx.get(f"{url}/healthz")
        assert r.status_code == 200 and r.json()["project"] == "timon"


def test_token_is_required(cfg, fake):
    with serve(cfg, fake) as url:
        assert httpx.post(f"{url}/mcp", json={}).status_code == 401
        bad = httpx.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer " + "x" * 48})
        assert bad.status_code == 401 and "bearer" in bad.headers["www-authenticate"].lower()


def test_outside_networks_are_refused(cfg, fake):
    lan_only = Config(**{**cfg.__dict__, "allowed_networks": _networks("192.168.1.0/24")})
    with serve(lan_only, fake) as url:
        r = httpx.post(f"{url}/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 403
        assert httpx.get(f"{url}/healthz").status_code == 403


async def test_mcp_tools_end_to_end(cfg, fake, tmp_path):
    with serve(cfg, fake) as url:
        async with client(url) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert len(tools) == 15
            assert tools["instance_list"].annotations.read_only_hint is True
            assert tools["instance_delete"].annotations.destructive_hint is True
            assert tools["instance_create"].description.startswith("[changes an instance]")
            assert "image" in tools["instance_create"].input_schema["properties"]

            listing = text_of(await c.call_tool("instance_list", {}))
            assert [i["name"] for i in listing["instances"]] == ["web1"]

            created = await c.call_tool("instance_create", {"name": "mcp-test", "image": "images:debian/13"})
            assert not created.is_error and text_of(created)["status"] == "Running"

            refused = await c.call_tool("instance_delete", {"name": "mcp-test"})
            assert refused.is_error and "force" in refused.content[0].text

            done = await c.call_tool("instance_delete", {"name": "mcp-test", "force": True})
            assert text_of(done)["deleted"] is True

            missing = await c.call_tool("instance_get", {"name": "ghost"})
            assert missing.is_error and "not found" in missing.content[0].text.lower()

            hidden = await c.call_tool("instance_exec", {"name": "web1", "command": ["env"], "env": {"SECRET": "s3cr3t"}})
            assert text_of(hidden)["exit_code"] == 0

    lines = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines()]
    assert [entry["tool"] for entry in lines] == [
        "instance_list", "instance_create", "instance_delete", "instance_delete", "instance_get", "instance_exec",
    ]
    assert lines[0]["client"] == "127.0.0.1"
    assert [entry["outcome"] for entry in lines] == ["ok", "ok", "error", "ok", "error", "ok"]
    assert "s3cr3t" not in (tmp_path / "audit.log").read_text()
    assert all(r.url.params.get_list("project") == ["timon"] for r in fake.requests)


async def test_wrong_token_fails_for_mcp_client(cfg, fake):
    with serve(cfg, fake) as url:
        with pytest.raises(Exception):
            async with client(url, token="w" * 48) as c:
                await c.list_tools()
