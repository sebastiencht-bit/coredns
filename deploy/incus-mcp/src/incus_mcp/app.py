"""HTTP application: the MCP server behind a LAN filter and a bearer token, plus the entry point."""

from __future__ import annotations

import contextvars
import functools
import hmac
import ipaddress
import json
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __version__
from .config import Config, ConfigError
from .incus import IncusClient, IncusError
from .tools import IncusTools, ToolInputError

log = logging.getLogger("incus_mcp")
CLIENT_IP: contextvars.ContextVar[str] = contextvars.ContextVar("client_ip", default="-")

INSTRUCTIONS = """\
Drives the Incus instances (system containers and virtual machines) of ONE project, "{project}".
Nothing outside that project is visible or reachable, whatever you ask.
Rules: look before you act (instance_list / instance_get); prefer instance_stop to instance_delete;
take a snapshot (snapshot_create) before risky changes; report Incus errors as they are.
Images: use "images:<distribution>/<release>", e.g. images:debian/13 (add /cloud for cloud-init).
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
DANGER = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

REDACT = {"cloud_init_user_data", "env"}


# ---------------------------------------------------------------------- audit
class Audit:
    """One JSON line per tool call, appended (the file is reopened each time, so logrotate needs no signal)."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()

    @staticmethod
    def _clean(args: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in args.items():
            if key in REDACT and value:
                out[key] = f"<redacted, {len(json.dumps(value))} chars>"
            else:
                text = json.dumps(value, default=str)
                out[key] = value if len(text) <= 500 else text[:500] + "…"
        return out

    def record(self, tool: str, args: dict[str, Any], outcome: str, detail: str = "", ms: int = 0) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "client": CLIENT_IP.get(),
            "tool": tool,
            "args": self._clean(args),
            "outcome": outcome,
            "ms": ms,
        }
        if detail:
            entry["detail"] = detail[:500]
        line = json.dumps(entry, ensure_ascii=False, default=str)
        if not self.path:
            log.info("audit %s", line)
            return
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:  # never fail a tool call because of the audit file, but say it loudly
            log.error("cannot write the audit log %s: %s; entry: %s", self.path, exc, line)


# ---------------------------------------------------------------------- MCP server
def build_mcp(cfg: Config, incus: IncusClient, audit: Audit) -> MCPServer:
    tools = IncusTools(cfg, incus)
    server = MCPServer(
        "incus",
        title=f"Incus ({cfg.project})",
        version=__version__,
        instructions=INSTRUCTIONS.format(project=cfg.project),
    )

    def register(func: Callable[..., Awaitable[dict[str, Any]]], annotations: ToolAnnotations, kind: str) -> None:
        name = func.__name__

        @functools.wraps(func)
        async def wrapper(**kwargs: Any) -> dict[str, Any]:
            started = time.monotonic()
            ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
            try:
                result = await getattr(tools, name)(**kwargs)
            except (ToolInputError, IncusError) as exc:
                audit.record(name, kwargs, "error", str(exc), ms())
                raise ToolError(str(exc)) from exc
            except Exception as exc:  # unexpected: log it, return a generic message
                log.exception("tool %s failed", name)
                audit.record(name, kwargs, "crash", repr(exc), ms())
                raise ToolError(f"internal error in {name}; see the incus-mcp service log") from exc
            audit.record(name, kwargs, "ok", "", ms())
            return result

        doc = (func.__doc__ or "").strip()
        server.tool(name=name, description=f"[{kind}] {doc}", annotations=annotations)(wrapper)

    # The docstrings below are what the model reads.
    async def instance_list() -> dict[str, Any]:
        """List every instance of the project with status, type, image, CPU/memory limits and IP addresses."""

    async def instance_get(name: str) -> dict[str, Any]:
        """Details of one instance: configuration (without volatile keys), devices, IPs, processes, memory and CPU use."""

    async def instance_logs(name: str, lines: int = 200) -> dict[str, Any]:
        """Last lines (default 200, max 2000) of the instance console log: boot messages, crashes."""

    async def image_list() -> dict[str, Any]:
        """Images already cached on the server, and how to name remote images (images:debian/13…)."""

    async def snapshot_list(name: str) -> dict[str, Any]:
        """Snapshots of an instance, oldest first."""

    async def operation_get(operation_id: str) -> dict[str, Any]:
        """State of a background Incus operation, e.g. an instance_exec that timed out."""

    async def instance_create(
        name: str,
        image: str,
        vm: bool = False,
        cpu: int | None = None,
        memory: str | None = None,
        cloud_init_user_data: str | None = None,
        description: str = "",
        start: bool = True,
    ) -> dict[str, Any]:
        """Create (and by default start) a system container, or a VM with vm=true.
        image: "images:debian/13", "images:ubuntu/24.04", "images:debian/13/cloud" for cloud-init, or a cached alias.
        cpu: 1-64 vCPU; memory: e.g. "2GiB"; cloud_init_user_data: a #cloud-config document (needs a /cloud image).
        Name: letters, digits and dashes, starting with a letter."""

    async def instance_start(name: str) -> dict[str, Any]:
        """Start a stopped instance."""

    async def instance_stop(name: str, force: bool = False) -> dict[str, Any]:
        """Stop an instance cleanly (force=true kills it if it does not stop within 60 s)."""

    async def instance_restart(name: str, force: bool = False) -> dict[str, Any]:
        """Restart an instance."""

    async def instance_set_config(name: str, config: dict[str, str | None]) -> dict[str, Any]:
        """Set or unset (value null) configuration keys. Only user.* keys and limits.cpu, limits.cpu.allowance,
        limits.memory, limits.memory.swap, limits.processes are accepted."""

    async def snapshot_create(name: str, snapshot: str | None = None) -> dict[str, Any]:
        """Take a snapshot of an instance (default name snap-YYYYMMDD-HHMMSS). Do this before risky changes."""

    async def snapshot_restore(name: str, snapshot: str) -> dict[str, Any]:
        """Roll the instance back to a snapshot. Everything written since that snapshot is LOST."""

    async def instance_delete(name: str, force: bool = False) -> dict[str, Any]:
        """Delete an instance and its snapshots, permanently. A running instance needs force=true (it is stopped first)."""

    async def instance_exec(
        name: str,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Run a command as root inside a running instance and return exit code, stdout and stderr (long outputs
        are cut in the middle). command is an argv list, e.g. ["sh", "-c", "df -h /"]. No terminal, no stdin."""

    for func in (instance_list, instance_get, instance_logs, image_list, snapshot_list, operation_get):
        register(func, READ, "read-only")
    for func in (instance_create, instance_start, instance_stop, instance_restart, instance_set_config, snapshot_create):
        register(func, WRITE, "changes an instance")
    for func in (snapshot_restore, instance_delete, instance_exec):
        register(func, DANGER, "destructive or arbitrary")
    return server


# ---------------------------------------------------------------------- HTTP guard
def _client_ip(scope: dict[str, Any]) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    client = scope.get("client")
    if not client:
        return None
    try:
        ip = ipaddress.ip_address(client[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


class Guard:
    """ASGI middleware: LAN-only, then a constant-time bearer check. /healthz needs the LAN only."""

    def __init__(self, app: Any, cfg: Config, token: str):
        self.app = app
        self.cfg = cfg
        self.expected = f"Bearer {token}".encode()

    async def _reply(self, send: Any, status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]] = ()) -> None:
        payload = json.dumps(body).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode()), *extra]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        ip = _client_ip(scope)
        if ip is None or not any(ip in net for net in self.cfg.allowed_networks):
            log.warning("refused %s: outside the allowed networks", ip)
            await self._reply(send, 403, {"error": "forbidden"})
            return
        if scope.get("path") == "/healthz":
            await self._reply(send, 200, {"status": "ok", "project": self.cfg.project, "version": __version__})
            return
        auth = dict(scope.get("headers") or []).get(b"authorization", b"")
        if not hmac.compare_digest(auth, self.expected):
            log.warning("refused %s: missing or wrong bearer token", ip)
            await self._reply(send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b'Bearer realm="incus-mcp"')])
            return
        token = CLIENT_IP.set(str(ip))
        try:
            await self.app(scope, receive, send)
        finally:
            CLIENT_IP.reset(token)


def build_app(cfg: Config, token: str, incus: IncusClient | None = None) -> Guard:
    incus = incus or IncusClient(cfg)
    server = build_mcp(cfg, incus, Audit(cfg.audit_log))
    starlette = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Host-header checks are meant for servers on localhost; this one is protected by the LAN filter and token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=cfg.listen_host,
    )
    return Guard(starlette, cfg, token)


async def self_check(cfg: Config) -> int:
    """Prove the certificate works for our project AND is refused everywhere else. Exit code 0 only if both hold."""
    import httpx

    from .incus import tls_context

    client = IncusClient(cfg)
    try:
        instances = await client.call("GET", "/1.0/instances")
        print(f"OK    project {cfg.project!r} reachable: {len(instances or [])} instance(s)")
    except IncusError as exc:
        print(f"FAIL  project {cfg.project!r} not reachable: {exc}")
        return 1
    finally:
        await client.aclose()
    leaks = []
    probes = (
        ("project 'default'", "/1.0/instances", {"project": "default", "recursion": "1"}),
        ("no project (the server default)", "/1.0/instances", {"recursion": "1"}),
        ("project 'default' itself", "/1.0/projects/default", {}),
    )
    async with httpx.AsyncClient(base_url=cfg.incus_url, verify=tls_context(cfg), timeout=15) as http:
        for label, path, params in probes:
            try:
                r = await http.get(path, params=params)
            except (httpx.HTTPError, OSError) as exc:
                print(f"FAIL  cannot probe {label}: {exc}")
                return 1
            readable = r.status_code < 400
            if readable and path == "/1.0/instances":
                # Incus 7 answers list calls on a forbidden project with 200 and a FILTERED list, not 403:
                # only instances of another project prove a leak.
                try:
                    items = r.json().get("metadata") or []
                except ValueError:
                    items = []
                readable = any(not isinstance(i, dict) or i.get("project") != cfg.project for i in items)
            if readable:
                leaks.append(label)
                print(f"FAIL  {label} is READABLE: the certificate is not restricted")
            else:
                print(f"OK    {label} refused ({r.status_code}{', filtered' if r.status_code < 400 else ''})")
    return 1 if leaks else 0


def main() -> None:
    import asyncio

    import uvicorn

    try:
        cfg = Config.from_env()
        if sys.argv[1:] == ["check"]:
            sys.exit(asyncio.run(self_check(cfg)))
        if sys.argv[1:]:
            print("usage: incus-mcp [check]", file=sys.stderr)
            sys.exit(2)
        token = cfg.read_token()
    except ConfigError as exc:
        print(f"incus-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(level=cfg.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    if cfg.tls_cert is None:
        log.warning("TLS is off: the bearer token travels in clear. Set INCUS_MCP_TLS_CERT/KEY outside tests.")
    uvicorn.run(
        build_app(cfg, token),
        host=cfg.listen_host,
        port=cfg.listen_port,
        ssl_certfile=cfg.tls_cert,
        ssl_keyfile=cfg.tls_key,
        log_level=cfg.log_level,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
