"""Tests for MAX_CONNECTIONS enforcement (#46)."""

import asyncio
import functools
import ssl
from unittest.mock import patch

import pytest

from app.proxy_handler import (
    _connection_semaphore,
    _service_unavailable,
    handle_client,
)
from app.tls import create_server_ssl_context, ensure_certificates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bypass_ssrf():
    with patch("app.proxy_handler._is_private_ip", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Reset the global semaphore between tests."""
    import app.proxy_handler as ph
    ph._connection_semaphore = None
    ph._active_connections = 0
    yield
    ph._connection_semaphore = None
    ph._active_connections = 0


def _client_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _start_server(settings):
    ensure_certificates(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)
    ssl_ctx = create_server_ssl_context(settings.TLS_CERT_PATH, settings.TLS_KEY_PATH)
    handler = functools.partial(handle_client, settings=settings)
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_ctx)
    port = server.sockets[0].getsockname()[1]
    return server, port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConnectionLimit:
    def test_503_response_format(self):
        resp = _service_unavailable()
        assert b"503 Service Unavailable" in resp
        assert b"connection limit reached" in resp

    @pytest.mark.asyncio
    async def test_connections_within_limit_accepted(self, settings):
        """Connections under MAX_CONNECTIONS are accepted normally."""
        settings.MAX_CONNECTIONS = 5
        server, port = await _start_server(settings)
        try:
            ctx = _client_ssl_context()
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
            writer.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # Should get a response (likely 502 since example.com isn't reachable,
            # but NOT 503)
            assert not data.startswith(b"HTTP/1.1 503")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_excess_connections_get_503(self, settings):
        """When MAX_CONNECTIONS is exceeded, new connections get 503."""
        settings.MAX_CONNECTIONS = 2

        # Start a target that holds connections open
        hold = asyncio.Event()

        async def slow_target(reader, writer):
            await hold.wait()
            writer.close()

        target = await asyncio.start_server(slow_target, "127.0.0.1", 0)
        target_port = target.sockets[0].getsockname()[1]

        server, port = await _start_server(settings)
        try:
            ctx = _client_ssl_context()

            # Fill up the connection limit with CONNECT requests to the slow target
            writers = []
            for _ in range(2):
                r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
                w.write(
                    f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode()
                )
                await w.drain()
                writers.append(w)

            # Small delay for server to process
            await asyncio.sleep(0.3)

            # Third connection should get 503
            r3, w3 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
            w3.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await w3.drain()

            data = await asyncio.wait_for(r3.read(4096), timeout=5.0)
            assert b"503 Service Unavailable" in data

            w3.close()
            try:
                await w3.wait_closed()
            except Exception:
                pass

            # Clean up held connections
            hold.set()
            for w in writers:
                w.close()
                try:
                    await w.wait_closed()
                except Exception:
                    pass
        finally:
            server.close()
            await server.wait_closed()
            target.close()
            await target.wait_closed()

    @pytest.mark.asyncio
    async def test_connections_freed_after_completion(self, settings):
        """After a connection completes, the slot is freed for new ones."""
        settings.MAX_CONNECTIONS = 1
        server, port = await _start_server(settings)
        try:
            ctx = _client_ssl_context()

            # First connection — completes immediately (bad target)
            r1, w1 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
            w1.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await w1.drain()
            await asyncio.wait_for(r1.read(4096), timeout=5.0)
            w1.close()
            try:
                await w1.wait_closed()
            except Exception:
                pass

            # Small delay for semaphore release
            await asyncio.sleep(0.2)

            # Second connection should succeed (slot freed)
            r2, w2 = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
            w2.write(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
            await w2.drain()
            data = await asyncio.wait_for(r2.read(4096), timeout=5.0)
            assert not data.startswith(b"HTTP/1.1 503")
            w2.close()
            try:
                await w2.wait_closed()
            except Exception:
                pass
        finally:
            server.close()
            await server.wait_closed()
