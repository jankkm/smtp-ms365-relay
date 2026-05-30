import logging
import socket
import time

from aiosmtpd.controller import Controller

import app.cert as cert_module
from app.smtp_handler import RelayAuthenticator, RelayHandler

logger = logging.getLogger(__name__)

_controllers: list[Controller] = []


def start_smtp_server() -> None:
    """Create and start SMTP controllers for ports 465 (SMTPS) and 587 (STARTTLS)."""
    _start_controllers()


def _start_controllers() -> None:
    global _controllers

    handler = RelayHandler()
    authenticator = RelayAuthenticator()
    ssl_ctx = cert_module.create_ssl_context()

    # Port 465: SSL from the first byte (SMTPS)
    ctrl_465 = Controller(
        handler,
        hostname="0.0.0.0",
        port=465,
        ssl_context=ssl_ctx,
        authenticator=authenticator,
        auth_required=True,
    )

    # Port 587: plain connection, STARTTLS required before AUTH
    ctrl_587 = Controller(
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
    ctrl_25 = Controller(
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
