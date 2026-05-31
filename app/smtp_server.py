import asyncio
import logging
import socket
import time

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import MISSING, SMTP

import app.cert as cert_module
from app.smtp_handler import RelayAuthenticator, RelayHandler

logger = logging.getLogger(__name__)

_controllers: list[Controller] = []

# Raised on the StreamReader only while receiving a legacy client message body.
_LEGACY_BODY_LIMIT = 4 << 20


class RelaySMTP(SMTP):
    """Strict by default; legacy DATA mode is per-credential after AUTH."""

    async def smtp_DATA(self, arg: str) -> None:
        if await self.check_helo_needed() or await self.check_auth_needed("DATA"):
            return
        auth = (self.session.auth_data if self.session else None) or {}
        if auth.get("legacy_data"):
            await self._smtp_DATA_legacy(arg)
        else:
            await SMTP.smtp_DATA(self, arg)

    async def _smtp_DATA_legacy(self, arg: str) -> None:
        """Accept LF-only line endings and lines longer than the SMTP 1000-byte limit."""
        assert self.envelope is not None
        if not self.envelope.rcpt_tos:
            await self.push("503 Error: need RCPT command")
            return
        if arg:
            await self.push("501 Syntax: DATA")
            return
        await self.push("354 Start mail input; end with <CRLF>.<CRLF>")

        reader = self._reader
        old_limit = reader._limit
        reader._limit = _LEGACY_BODY_LIMIT
        try:
            lines, nbytes = [], 0
            while True:
                try:
                    raw = await reader.readuntil(b"\n")
                except asyncio.CancelledError:
                    self._writer.close()
                    raise
                nbytes += len(raw)
                if self.data_size_limit and nbytes > self.data_size_limit:
                    await self.push("552 Error: Too much mail data")
                    self._set_post_data_state()
                    return
                line = raw.rstrip(b"\r\n")
                if line == b".":
                    break
                if line.startswith(b"."):
                    line = line[1:]
                lines.append(line)
        finally:
            reader._limit = old_limit

        body = b"\r\n".join(lines) + (b"\r\n" if lines else b"")
        self.envelope.original_content = self.envelope.content = body
        status = await self._call_handler_hook("DATA")
        self._set_post_data_state()
        await self.push("250 OK" if status is MISSING else status)


class RelayController(Controller):
    def factory(self):
        return RelaySMTP(self.handler, **self.SMTP_kwargs)


def start_smtp_server() -> None:
    """Create and start SMTP controllers for ports 465 (SMTPS) and 587 (STARTTLS)."""
    _start_controllers()


def _start_controllers() -> None:
    global _controllers

    handler = RelayHandler()
    authenticator = RelayAuthenticator()
    ssl_ctx = cert_module.create_ssl_context()

    # Port 465: SSL from the first byte (SMTPS)
    ctrl_465 = RelayController(
        handler,
        hostname="0.0.0.0",
        port=465,
        ssl_context=ssl_ctx,
        authenticator=authenticator,
        auth_required=True,
    )

    # Port 587: plain connection, STARTTLS required before AUTH
    ctrl_587 = RelayController(
        handler,
        hostname="0.0.0.0",
        port=587,
        tls_context=ssl_ctx,
        require_starttls=True,
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=True,
    )

    # Port 25: legacy plain SMTP, STARTTLS available but not required,
    # AUTH allowed without TLS for compatibility with older clients
    ctrl_25 = RelayController(
        handler,
        hostname="0.0.0.0",
        port=25,
        tls_context=ssl_ctx,
        require_starttls=False,
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=False,
    )

    ctrl_465.start()
    ctrl_587.start()
    ctrl_25.start()
    _controllers = [ctrl_465, ctrl_587, ctrl_25]
    logger.info("SMTP servers listening on ports 25 (legacy), 465 (SMTPS) and 587 (STARTTLS)")


def reload_ssl_context() -> None:
    """Stop all controllers and restart them with a freshly loaded SSL context.
    Called by the certificate renewal job after a new certificate is written."""
    global _controllers
    logger.info("Reloading SMTP SSL context…")
    for ctrl in _controllers:
        try:
            ctrl.stop()
        except Exception as exc:
            logger.error("Error stopping SMTP controller on port %s: %s", ctrl.port, exc)
    _controllers = []
    time.sleep(1)
    _start_controllers()
    logger.info("SMTP SSL context reloaded.")


def is_running() -> bool:
    """Check whether the SMTP server is accepting connections on port 587."""
    try:
        with socket.create_connection(("127.0.0.1", 587), timeout=2):
            return True
    except OSError:
        return False
