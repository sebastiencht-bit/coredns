"""Minimal async client for the Incus REST API, locked to a single project."""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config

# Incus operation status codes.
OP_RUNNING = {100, 101, 103, 105}  # created, started, running, pending
OP_SUCCESS = 200


class IncusError(Exception):
    """An error reported by Incus (or the transport), safe to show to the model."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        return f"{self.message} (code {self.code})" if self.code else self.message


class OperationTimeout(IncusError):
    def __init__(self, operation_id: str):
        super().__init__(f"operation {operation_id} is still running", 103)
        self.operation_id = operation_id


def seg(value: str) -> str:
    """Quote one URL path segment (names are validated beforehand; this is defence in depth)."""
    return quote(value, safe="")


def tls_context(cfg: Config) -> ssl.SSLContext:
    """Trust exactly the pinned Incus server certificate and present our restricted client certificate."""
    ctx = ssl.create_default_context(cafile=cfg.server_cert)
    # Incus certificates are self-signed and name the host, not 127.0.0.1: the pin replaces hostname checks.
    ctx.check_hostname = False
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.load_cert_chain(cfg.client_cert, cfg.client_key)
    return ctx


class IncusClient:
    def __init__(self, cfg: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.project = cfg.project
        kwargs: dict[str, Any] = {"base_url": cfg.incus_url, "timeout": httpx.Timeout(30.0, read=None)}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = tls_context(cfg)
        self._http = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, params: dict[str, Any] | None, body: Any) -> httpx.Response:
        path = path.split("?", 1)[0]  # Incus may return paths with a query; the project is set below
        if not path.startswith("/1.0"):
            raise IncusError(f"refusing to call {path!r}: not an Incus API path")
        query = {k: v for k, v in (params or {}).items() if k != "project"}
        query["project"] = self.project  # always, whatever the caller asked for
        try:
            return await self._http.request(method, path, params=query, json=body)
        # With TLS 1.3 a refused client certificate is reported after the handshake, while reading the
        # answer: httpx then lets the raw ssl.SSLError (an OSError) through instead of wrapping it.
        except (httpx.HTTPError, OSError) as exc:
            raise IncusError(f"cannot reach Incus: {exc.__class__.__name__}: {exc}") from exc

    @staticmethod
    def _error_from(response: httpx.Response) -> IncusError:
        try:
            data = response.json()
            return IncusError(data.get("error") or response.reason_phrase, data.get("error_code") or response.status_code)
        except ValueError:
            return IncusError(response.text.strip()[:500] or response.reason_phrase, response.status_code)

    async def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        wait: bool = True,
        timeout: int = 600,
    ) -> Any:
        """Call the API; for background operations, wait for them and return the operation metadata."""
        response = await self._send(method, path, params, body)
        if response.status_code >= 400:
            raise self._error_from(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise IncusError(f"unexpected answer from Incus for {method} {path}") from exc
        if data.get("type") == "error":
            raise IncusError(data.get("error", "unknown error"), data.get("error_code"))
        if data.get("type") == "async":
            operation = data.get("operation") or (data.get("metadata") or {}).get("id", "")
            op_id = str(operation).rstrip("/").rsplit("/", 1)[-1]
            return await self.wait(op_id, timeout) if wait else data.get("metadata")
        return data.get("metadata")

    async def raw(self, path: str, params: dict[str, Any] | None = None) -> str:
        response = await self._send("GET", path, params, None)
        if response.status_code >= 400:
            raise self._error_from(response)
        return response.text

    async def wait(self, operation_id: str, timeout: int) -> dict[str, Any]:
        meta = await self.call("GET", f"/1.0/operations/{seg(operation_id)}/wait", params={"timeout": timeout})
        status = meta.get("status_code")
        if status in OP_RUNNING:
            raise OperationTimeout(operation_id)
        if status != OP_SUCCESS:
            raise IncusError(meta.get("err") or meta.get("status") or "operation failed", status)
        return meta
