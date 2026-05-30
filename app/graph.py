import base64
import email as email_stdlib
import email.header
import logging
import time

import httpx
import msal

import app.config as config

logger = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/users/{from_addr}/sendMail"

_msal_app: msal.ConfidentialClientApplication | None = None


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            config.ENTRA_CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{config.ENTRA_TENANT_ID}",
            client_credential=config.ENTRA_CLIENT_SECRET,
        )
    return _msal_app


def _acquire_token() -> str:
    result = _get_msal_app().acquire_token_for_client(scopes=GRAPH_SCOPE)
    if "access_token" not in result:
        raise RuntimeError(
            f"Failed to acquire Graph API token: {result.get('error_description', result)}"
        )
    return result["access_token"]


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value or "")
    decoded = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            try:
                decoded.append(fragment.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                # charset like "unknown-8bit" is not a valid Python codec — fall back
                decoded.append(fragment.decode("utf-8", errors="replace"))
        else:
            decoded.append(fragment)
    return "".join(decoded)


def _build_payload(raw_eml: bytes, to_addresses: list[str]) -> dict:
    """Parse a raw MIME message and build a Graph API sendMail payload."""
    msg = email_stdlib.message_from_bytes(raw_eml)
    subject = _decode_header(msg.get("Subject", "(no subject)"))

    body_content = ""
    body_type = "Text"
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if part.get_filename() or "attachment" in disposition:
                raw = part.get_payload(decode=True) or b""
                attachments.append({
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": _decode_header(part.get_filename() or "attachment"),
                    "contentType": content_type,
                    "contentBytes": base64.b64encode(raw).decode(),
                })
            elif content_type == "text/html" and not body_content:
                raw = part.get_payload(decode=True) or b""
                body_content = raw.decode("utf-8", errors="replace")
                body_type = "HTML"
            elif content_type == "text/plain" and not body_content:
                raw = part.get_payload(decode=True) or b""
                body_content = raw.decode("utf-8", errors="replace")
    else:
        raw = msg.get_payload(decode=True) or b""
        body_content = raw.decode("utf-8", errors="replace")
        body_type = "HTML" if msg.get_content_type() == "text/html" else "Text"

    payload: dict = {
        "message": {
            "subject": subject,
            "body": {"contentType": body_type, "content": body_content},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to_addresses],
        },
        "saveToSentItems": False,
    }
    if attachments:
        payload["message"]["attachments"] = attachments
    return payload


def send_mail(from_address: str, to_addresses: list[str], raw_eml: bytes) -> None:
    """Forward a raw MIME message via the Graph API sendMail endpoint.

    Retries once on HTTP 429 / 503 with the server's Retry-After hint.
    """
    token = _acquire_token()
    payload = _build_payload(raw_eml, to_addresses)
    url = GRAPH_SEND_URL.format(from_addr=from_address)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for attempt in range(2):
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload, headers=headers)

        if response.status_code == 202:
            return

        if response.status_code in (429, 503) and attempt == 0:
            wait = min(int(response.headers.get("Retry-After", "5")), 30)
            logger.warning(f"Graph API throttled (HTTP {response.status_code}), retrying in {wait}s")
            time.sleep(wait)
            continue

        response.raise_for_status()


def send_test_mail(from_address: str, to_address: str) -> None:
    """Send a simple test email to verify Graph API connectivity."""
    raw_eml = (
        f"From: {from_address}\r\n"
        f"To: {to_address}\r\n"
        f"Subject: SMTP Relay – Test Email\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"This is a test email sent from your SMTP relay to verify the configuration."
    ).encode()
    send_mail(from_address, [to_address], raw_eml)
