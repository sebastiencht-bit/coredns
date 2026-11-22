"""The pinned-certificate TLS path, against a real HTTPS server that requires a client certificate."""

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from incus_mcp.app import self_check
from incus_mcp.config import Config
from incus_mcp.incus import IncusClient, IncusError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")


def make_cert(directory, name, cn):
    key, crt = directory / f"{name}.key", directory / f"{name}.crt"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-384", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-days", "2", "-subj", f"/CN={cn}"],
        check=True, capture_output=True,
    )
    return crt, key


class Handler(BaseHTTPRequestHandler):
    restricted = True  # like a certificate added with --restricted --projects timon

    def do_GET(self):  # noqa: N802
        if self.restricted and "project=timon" not in self.path:
            status, data = 403, {"type": "error", "error": "Certificate is restricted", "error_code": 403}
        else:
            status, data = 200, {"type": "sync", "status_code": 200, "metadata": [{"name": "c1", "status": "Running"}]}
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def incus_like(tmp_path):
    # Like Incus: a self-signed server certificate naming the host, not 127.0.0.1.
    server_crt, server_key = make_cert(tmp_path, "server", "incus-host")
    client_crt, client_key = make_cert(tmp_path, "client", "incus-mcp")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(server_crt, server_key)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(client_crt)  # the "trust store": only our client certificate
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{httpd.server_address[1]}", server_crt, client_crt, client_key, tmp_path
    httpd.shutdown()


def cfg_for(url, server_crt, client_crt, client_key):
    return Config(incus_url=url, server_cert=str(server_crt), client_cert=str(client_crt), client_key=str(client_key), audit_log=None)


async def test_pinned_server_and_client_certificate(incus_like):
    url, server_crt, client_crt, client_key, _ = incus_like
    client = IncusClient(cfg_for(url, server_crt, client_crt, client_key))
    try:
        assert await client.call("GET", "/1.0/instances") == [{"name": "c1", "status": "Running"}]
    finally:
        await client.aclose()


async def test_other_server_certificate_is_refused(incus_like):
    url, _server_crt, client_crt, client_key, tmp = incus_like
    impostor, _ = make_cert(tmp, "impostor", "incus-host")
    client = IncusClient(cfg_for(url, impostor, client_crt, client_key))
    try:
        with pytest.raises(IncusError, match="cannot reach Incus"):
            await client.call("GET", "/1.0/instances")
    finally:
        await client.aclose()


async def test_unknown_client_certificate_is_refused(incus_like):
    url, server_crt, _client_crt, _client_key, tmp = incus_like
    other_crt, other_key = make_cert(tmp, "other", "someone")
    client = IncusClient(cfg_for(url, server_crt, other_crt, other_key))
    try:
        with pytest.raises(IncusError, match="cannot reach Incus"):
            await client.call("GET", "/1.0/instances")
    finally:
        await client.aclose()


async def test_self_check_passes_only_when_restricted(incus_like, capsys):
    url, server_crt, client_crt, client_key, _ = incus_like
    cfg = cfg_for(url, server_crt, client_crt, client_key)
    assert await self_check(cfg) == 0
    assert "FAIL" not in capsys.readouterr().out
    Handler.restricted = False
    try:
        assert await self_check(cfg) == 1
        assert "is READABLE" in capsys.readouterr().out
    finally:
        Handler.restricted = True
