"""An in-memory stand-in for the Incus REST API, enough for the tools (served through httpx.MockTransport)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx


class FakeIncus:
    def __init__(self, project: str = "timon"):
        self.project = project
        self.instances: dict[str, dict[str, Any]] = {}
        self.snapshots: dict[str, list[str]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.files: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.exec_result = {"return": 0, "stdout": "hello\n", "stderr": ""}
        self.exec_runs_forever = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def sync(metadata: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"type": "sync", "status": "Success", "status_code": 200, "metadata": metadata})

    @staticmethod
    def error(code: int, message: str) -> httpx.Response:
        return httpx.Response(code, json={"type": "error", "error": message, "error_code": code})

    def async_op(self, metadata: dict[str, Any] | None = None, status_code: int = 200, err: str = "") -> httpx.Response:
        op_id = str(uuid.uuid4())
        self.operations[op_id] = {
            "id": op_id,
            "status": "Success" if status_code == 200 else "Running" if status_code == 103 else "Failure",
            "status_code": status_code,
            "err": err,
            "metadata": metadata or {},
        }
        return httpx.Response(202, json={"type": "async", "operation": f"/1.0/operations/{op_id}", "metadata": {"id": op_id}})

    def add(self, name: str, status: str = "Running", type_: str = "container") -> None:
        self.instances[name] = {
            "name": name,
            "status": status,
            "type": type_,
            "config": {"image.description": "Debian trixie", "user.owner": "test", "volatile.eth0.hwaddr": "x"},
            "expanded_config": {"limits.cpu": "2"},
            "devices": {},
            "created_at": "2026-11-22T10:00:00Z",
        }
        self.snapshots[name] = []

    def state(self, name: str) -> dict[str, Any]:
        running = self.instances[name]["status"] == "Running"
        return {
            "status": self.instances[name]["status"],
            "processes": 12 if running else 0,
            "memory": {"usage": 1024},
            "cpu": {"usage": 99},
            "network": {
                "lo": {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "local"}]},
                "eth0": {"addresses": [{"family": "inet", "address": "10.0.0.5", "scope": "global"}]},
            }
            if running
            else {},
        }

    # ------------------------------------------------------------------ router
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.params.get_list("project") != [self.project]:
            return self.error(403, "Certificate is restricted")
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        parts = path.strip("/").split("/")

        if path.startswith("/1.0/operations/"):
            op = self.operations.get(parts[2])
            if op is None:
                return self.error(404, "Operation not found")
            return self.sync(op)
        if path == "/1.0/images":
            return self.sync([{"fingerprint": "a" * 64, "aliases": [{"name": "debian/13"}], "properties": {"description": "Debian 13"}, "type": "container", "architecture": "x86_64", "size": 120_000_000}])
        if path == "/1.0/instances" and method == "GET":
            return self.sync([{**i, "state": self.state(n)} for n, i in self.instances.items()])
        if path == "/1.0/instances" and method == "POST":
            if body["name"] in self.instances:
                return self.async_op(status_code=400, err="Instance already exists")
            self.add(body["name"], status="Stopped", type_=body["type"])
            self.instances[body["name"]]["config"].update(body.get("config") or {})
            self.instances[body["name"]]["source"] = body["source"]
            return self.async_op()
        if len(parts) >= 3 and parts[1] == "instances":
            name = parts[2]
            if name not in self.instances:
                return self.error(404, "Instance not found")
            inst = self.instances[name]
            rest = parts[3:]
            if not rest:
                if method == "GET":
                    return self.sync(inst)
                if method == "DELETE":
                    if inst["status"] != "Stopped":
                        return self.async_op(status_code=400, err="Instance is running")
                    del self.instances[name]
                    return self.async_op()
                if method == "PATCH":
                    inst["config"].update(body["config"])
                    return self.sync({})
                if method == "PUT" and "restore" in body:
                    if body["restore"] not in self.snapshots[name]:
                        return self.async_op(status_code=400, err="Snapshot not found")
                    return self.async_op()
            if rest == ["state"]:
                if method == "GET":
                    return self.sync(self.state(name))
                inst["status"] = {"start": "Running", "restart": "Running", "stop": "Stopped"}[body["action"]]
                return self.async_op()
            if rest == ["snapshots"]:
                if method == "GET":
                    return self.sync([{"name": s, "created_at": "now", "stateful": False} for s in self.snapshots[name]])
                self.snapshots[name].append(body["name"])
                return self.async_op()
            if rest == ["console"]:
                return httpx.Response(200, text="\n".join(f"boot line {i}" for i in range(1, 501)))
            if rest == ["exec"]:
                if self.exec_runs_forever:
                    return self.async_op(status_code=103)
                out = f"/1.0/instances/{name}/logs/exec_{uuid.uuid4().hex}.stdout"
                err = f"/1.0/instances/{name}/logs/exec_{uuid.uuid4().hex}.stderr"
                self.files[out], self.files[err] = self.exec_result["stdout"], self.exec_result["stderr"]
                self.last_exec = body
                return self.async_op({"return": self.exec_result["return"], "output": {"1": f"{out}?project={self.project}", "2": err}})
            if rest[0] == "logs":
                if method == "DELETE":
                    self.files.pop(path, None)
                    return self.sync({})
                return httpx.Response(200, text=self.files.get(path, ""))
        return self.error(404, f"not found: {method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
