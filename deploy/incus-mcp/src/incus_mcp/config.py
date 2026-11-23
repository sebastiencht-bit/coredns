"""Configuration, read from INCUS_MCP_* environment variables (see incus-mcp.env.example)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MIN_TOKEN_LENGTH = 32


class ConfigError(Exception):
    pass


def _networks(value: str) -> tuple[Network, ...]:
    try:
        return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError(f"INCUS_MCP_ALLOWED_NETWORKS: {exc}") from exc


@dataclass(frozen=True)
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8444
    tls_cert: str | None = None
    tls_key: str | None = None
    token_file: str = "/etc/incus-mcp/token"
    allowed_networks: tuple[Network, ...] = field(default_factory=lambda: _networks("192.168.1.0/24"))
    incus_url: str = "https://127.0.0.1:8443"
    client_cert: str = "/etc/incus-mcp/client.crt"
    client_key: str = "/etc/incus-mcp/client.key"
    server_cert: str = "/etc/incus-mcp/server.crt"
    project: str = "timon"
    image_server: str = "https://images.linuxcontainers.org"
    audit_log: str | None = "/var/log/incus-mcp/audit.log"
    exec_timeout: int = 120
    output_limit: int = 64 * 1024
    log_level: str = "info"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)

        def get(name: str, default: str | None) -> str | None:
            value = env.get(f"INCUS_MCP_{name}")
            return default if value is None or value == "" else value

        def get_int(name: str, default: int) -> int:
            raw = get(name, str(default))
            try:
                return int(raw)  # type: ignore[arg-type]
            except ValueError as exc:
                raise ConfigError(f"INCUS_MCP_{name} must be an integer, got {raw!r}") from exc

        d = cls()
        audit = get("AUDIT_LOG", d.audit_log)
        cfg = cls(
            listen_host=get("LISTEN_HOST", d.listen_host),  # type: ignore[arg-type]
            listen_port=get_int("LISTEN_PORT", d.listen_port),
            tls_cert=get("TLS_CERT", None),
            tls_key=get("TLS_KEY", None),
            token_file=get("TOKEN_FILE", d.token_file),  # type: ignore[arg-type]
            allowed_networks=_networks(get("ALLOWED_NETWORKS", "192.168.1.0/24")),  # type: ignore[arg-type]
            incus_url=get("INCUS_URL", d.incus_url).rstrip("/"),  # type: ignore[union-attr]
            client_cert=get("CLIENT_CERT", d.client_cert),  # type: ignore[arg-type]
            client_key=get("CLIENT_KEY", d.client_key),  # type: ignore[arg-type]
            server_cert=get("SERVER_CERT", d.server_cert),  # type: ignore[arg-type]
            project=get("PROJECT", d.project),  # type: ignore[arg-type]
            image_server=get("IMAGE_SERVER", d.image_server).rstrip("/"),  # type: ignore[union-attr]
            audit_log=None if audit in ("-", "off", "none") else audit,
            exec_timeout=get_int("EXEC_TIMEOUT", d.exec_timeout),
            output_limit=get_int("OUTPUT_LIMIT", d.output_limit),
            log_level=get("LOG_LEVEL", d.log_level),  # type: ignore[arg-type]
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.allowed_networks:
            raise ConfigError("INCUS_MCP_ALLOWED_NETWORKS is empty: no client could connect")
        if not self.project or self.project == "default":
            raise ConfigError("INCUS_MCP_PROJECT must name a dedicated, restricted project, not 'default'")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ConfigError("set both INCUS_MCP_TLS_CERT and INCUS_MCP_TLS_KEY, or neither")
        if not 1 <= self.exec_timeout <= 3600:
            raise ConfigError("INCUS_MCP_EXEC_TIMEOUT must be between 1 and 3600 seconds")
        if self.output_limit < 1024:
            raise ConfigError("INCUS_MCP_OUTPUT_LIMIT must be at least 1024 bytes")

    def read_token(self) -> str:
        path = Path(self.token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read the token file {path}: {exc.strerror}") from exc
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"the token in {path} is shorter than {MIN_TOKEN_LENGTH} characters")
        return token
