"""Tool implementations: plain async methods, independent of MCP so they can be tested directly."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .incus import IncusClient, IncusError, OperationTimeout, seg

INSTANCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]$|^[A-Za-z]$")
SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
IMAGE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{12,64}$")
MEMORY = re.compile(r"^[1-9][0-9]{0,5}(MB|MiB|GB|GiB)$")
SETTABLE_KEYS = re.compile(r"^(user\.[A-Za-z0-9._-]{1,128}|limits\.(cpu|cpu\.allowance|memory|memory\.swap|processes))$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ToolInputError(ValueError):
    """Invalid arguments: reported to the model so it can fix its call."""


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ToolInputError(f"invalid {what}: {value!r}")
    return value


def truncate(text: str, limit: int) -> str:
    """Keep the head and the tail of a long output, with a marker in between."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[… {len(text) - 2 * half} characters cut …]\n{text[-half:]}"


def _addresses(state: dict[str, Any] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"ipv4": [], "ipv6": []}
    for ifname, net in ((state or {}).get("network") or {}).items():
        if ifname == "lo":
            continue
        for addr in net.get("addresses") or []:
            if addr.get("scope") != "global":
                continue
            out["ipv4" if addr.get("family") == "inet" else "ipv6"].append(addr.get("address"))
    return out


def _user_config(config: dict[str, Any]) -> dict[str, str]:
    return {k: v for k, v in (config or {}).items() if k.startswith("user.")}


def _summary(inst: dict[str, Any]) -> dict[str, Any]:
    config = inst.get("config") or {}
    expanded = inst.get("expanded_config") or config
    return {
        "name": inst.get("name"),
        "status": inst.get("status"),
        "type": inst.get("type"),
        "image": config.get("image.description") or config.get("image.os"),
        "cpu": expanded.get("limits.cpu"),
        "memory": expanded.get("limits.memory"),
        **_addresses(inst.get("state")),
        "created_at": inst.get("created_at"),
        "user": _user_config(config),
    }


class IncusTools:
    def __init__(self, cfg: Config, incus: IncusClient):
        self.cfg = cfg
        self.incus = incus

    def _instance(self, name: str) -> str:
        return f"/1.0/instances/{seg(_check(INSTANCE_NAME, name, 'instance name'))}"

    # ------------------------------------------------------------------ read
    async def instance_list(self) -> dict[str, Any]:
        items = await self.incus.call("GET", "/1.0/instances", params={"recursion": 2}) or []
        return {"project": self.cfg.project, "instances": [_summary(i) for i in items]}

    async def instance_get(self, name: str) -> dict[str, Any]:
        inst = await self.incus.call("GET", self._instance(name))
        state = await self.incus.call("GET", f"{self._instance(name)}/state")
        config = {k: v for k, v in (inst.get("config") or {}).items() if not k.startswith("volatile.")}
        memory = (state or {}).get("memory") or {}
        return {
            **_summary({**inst, "state": state}),
            "architecture": inst.get("architecture"),
            "description": inst.get("description"),
            "ephemeral": inst.get("ephemeral"),
            "config": config,
            "devices": inst.get("expanded_devices") or inst.get("devices") or {},
            "processes": (state or {}).get("processes"),
            "memory_usage_bytes": memory.get("usage"),
            "cpu_usage_ns": ((state or {}).get("cpu") or {}).get("usage"),
        }

    async def instance_logs(self, name: str, lines: int = 200) -> dict[str, Any]:
        lines = max(1, min(int(lines), 2000))
        text = await self.incus.raw(f"{self._instance(name)}/console", params={"type": "log"})
        tail = "\n".join(text.splitlines()[-lines:])
        return {"name": name, "console_log": truncate(tail, self.cfg.output_limit)}

    async def image_list(self) -> dict[str, Any]:
        images = await self.incus.call("GET", "/1.0/images", params={"recursion": 1}) or []
        return {
            "cached_images": [
                {
                    "fingerprint": (img.get("fingerprint") or "")[:12],
                    "aliases": [a.get("name") for a in img.get("aliases") or []],
                    "description": (img.get("properties") or {}).get("description"),
                    "type": img.get("type"),
                    "architecture": img.get("architecture"),
                    "size_mb": round((img.get("size") or 0) / 1_000_000, 1),
                }
                for img in images
            ],
            "remote": (
                f"Any image of {self.cfg.image_server} can be used as 'images:<alias>', "
                "for example images:debian/13, images:ubuntu/24.04, images:alpine/3.22, "
                "images:debian/13/cloud (with cloud-init)."
            ),
        }

    async def snapshot_list(self, name: str) -> dict[str, Any]:
        snaps = await self.incus.call("GET", f"{self._instance(name)}/snapshots", params={"recursion": 1}) or []
        return {
            "name": name,
            "snapshots": [
                {"name": s.get("name"), "created_at": s.get("created_at"), "stateful": s.get("stateful")} for s in snaps
            ],
        }

    async def operation_get(self, operation_id: str) -> dict[str, Any]:
        try:
            op_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise ToolInputError(f"invalid operation id: {operation_id!r}") from exc
        op = await self.incus.call("GET", f"/1.0/operations/{op_id}")
        return {k: op.get(k) for k in ("id", "description", "status", "status_code", "err", "created_at", "updated_at")}

    # ------------------------------------------------------------------ write
    def _source(self, image: str) -> dict[str, Any]:
        if image.startswith("images:"):
            alias = _check(IMAGE_ALIAS, image.removeprefix("images:"), "image alias")
            return {"type": "image", "alias": alias, "server": self.cfg.image_server, "protocol": "simplestreams"}
        if FINGERPRINT.fullmatch(image):
            return {"type": "image", "fingerprint": image}
        return {"type": "image", "alias": _check(IMAGE_ALIAS, image, "image alias")}

    async def instance_create(
        self,
        name: str,
        image: str,
        vm: bool = False,
        cpu: int | None = None,
        memory: str | None = None,
        cloud_init_user_data: str | None = None,
        description: str = "",
        start: bool = True,
    ) -> dict[str, Any]:
        _check(INSTANCE_NAME, name, "instance name")
        config: dict[str, str] = {}
        if cpu is not None:
            if not 1 <= int(cpu) <= 64:
                raise ToolInputError("cpu must be between 1 and 64")
            config["limits.cpu"] = str(int(cpu))
        if memory is not None:
            config["limits.memory"] = _check(MEMORY, memory, "memory (use for example 1GiB or 512MiB)")
        if cloud_init_user_data:
            if len(cloud_init_user_data) > 64 * 1024:
                raise ToolInputError("cloud_init_user_data is larger than 64 KiB")
            config["cloud-init.user-data"] = cloud_init_user_data
        body = {
            "name": name,
            "type": "virtual-machine" if vm else "container",
            "source": self._source(image),
            "config": config,
            "description": description[:255],
        }
        await self.incus.call("POST", "/1.0/instances", body=body, timeout=1800)
        if start:
            await self._state(name, "start")
        return await self.instance_get(name)

    async def _state(self, name: str, action: str, force: bool = False, timeout: int = 60) -> None:
        body = {"action": action, "timeout": timeout, "force": force}
        await self.incus.call("PUT", f"{self._instance(name)}/state", body=body, timeout=timeout + 30)

    async def instance_start(self, name: str) -> dict[str, Any]:
        await self._state(name, "start")
        return {"name": name, "status": "Running"}

    async def instance_stop(self, name: str, force: bool = False) -> dict[str, Any]:
        await self._state(name, "stop", force=force)
        return {"name": name, "status": "Stopped"}

    async def instance_restart(self, name: str, force: bool = False) -> dict[str, Any]:
        await self._state(name, "restart", force=force)
        return {"name": name, "status": "Running"}

    async def instance_set_config(self, name: str, config: dict[str, str | None]) -> dict[str, Any]:
        if not config:
            raise ToolInputError("config is empty")
        patch: dict[str, str] = {}
        for key, value in config.items():
            if not SETTABLE_KEYS.fullmatch(key):
                raise ToolInputError(f"key {key!r} cannot be changed here: only user.* and limits.* keys")
            if key == "limits.memory" and value:
                _check(MEMORY, value, "limits.memory")
            patch[key] = "" if value is None else str(value)
        await self.incus.call("PATCH", self._instance(name), body={"config": patch})
        return {"name": name, "changed": sorted(patch)}

    async def snapshot_create(self, name: str, snapshot: str | None = None) -> dict[str, Any]:
        snap = snapshot or datetime.now(UTC).strftime("snap-%Y%m%d-%H%M%S")
        _check(SNAPSHOT_NAME, snap, "snapshot name")
        await self.incus.call("POST", f"{self._instance(name)}/snapshots", body={"name": snap, "stateful": False})
        return {"name": name, "snapshot": snap}

    # ------------------------------------------------------------------ sensitive
    async def snapshot_restore(self, name: str, snapshot: str) -> dict[str, Any]:
        _check(SNAPSHOT_NAME, snapshot, "snapshot name")
        await self.incus.call("PUT", self._instance(name), body={"restore": snapshot}, timeout=1800)
        return {"name": name, "restored": snapshot}

    async def instance_delete(self, name: str, force: bool = False) -> dict[str, Any]:
        inst = await self.incus.call("GET", self._instance(name))
        if (inst.get("status") or "").lower() != "stopped":
            if not force:
                raise ToolInputError(f"{name} is {inst.get('status')}: stop it first, or pass force=true")
            await self._state(name, "stop", force=True)
        await self.incus.call("DELETE", self._instance(name), timeout=600)
        return {"name": name, "deleted": True}

    async def instance_exec(
        self,
        name: str,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            raise ToolInputError("command must be a non-empty list of strings, e.g. ['df', '-h']")
        timeout = max(1, min(int(timeout), self.cfg.exec_timeout))
        for key in env or {}:
            _check(ENV_NAME, key, "environment variable name")
        body: dict[str, Any] = {
            "command": command,
            "environment": env or {},
            "wait-for-websocket": False,
            "interactive": False,
            "record-output": True,
        }
        if cwd:
            if not cwd.startswith("/"):
                raise ToolInputError("cwd must be an absolute path")
            body["cwd"] = cwd
        try:
            meta = await self.incus.call("POST", f"{self._instance(name)}/exec", body=body, timeout=timeout)
        except OperationTimeout as exc:
            return {"name": name, "status": "timeout", "timeout_s": timeout, "operation": exc.operation_id}
        result = meta.get("metadata") or {}
        outputs: dict[str, str] = {}
        for fd, label in (("1", "stdout"), ("2", "stderr")):
            path = (result.get("output") or {}).get(fd)
            if not path:
                outputs[label] = ""
                continue
            if not path.split("?", 1)[0].startswith(f"{self._instance(name)}/logs/"):
                raise IncusError(f"unexpected output location from Incus: {path!r}")
            try:
                outputs[label] = truncate(await self.incus.raw(path), self.cfg.output_limit)
            finally:
                try:
                    await self.incus.call("DELETE", path)
                except IncusError:
                    pass  # the log is cleaned up with the instance anyway
        return {"name": name, "exit_code": result.get("return"), **outputs}
