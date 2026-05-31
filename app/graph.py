import base64
import email as email_stdlib
import email.header
import logging
import threading
import time
from dataclasses import dataclass, field

import httpx
import msal

import app.config as config

logger = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0/users/{from_addr}"
IMMUTABLE_ID_PREFER = 'IdType="ImmutableId"'

SMALL_ATTACHMENT_MAX = 3 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 4 * 1024 * 1024
LARGE_ATTACHMENT_MAX = 150 * 1024 * 1024

SENT_ITEMS_DELETE_DELAY = 5
SENT_ITEMS_DELETE_MAX_ATTEMPTS = 6
SENT_ITEMS_DELETE_RETRY_INTERVAL = 3
GRAPH_JSON_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 300.0

_msal_app: msal.ConfidentialClientApplication | None = None


@dataclass
class AttachmentPart:
    name: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass
class ParsedMessage:
    subject: str
    body_content: str
    body_type: str
    attachments: list[AttachmentPart] = field(default_factory=list)


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
                decoded.append(fragment.decode("utf-8", errors="replace"))
        else:
            decoded.append(fragment)
    return "".join(decoded)


def _user_url(from_address: str, path: str) -> str:
    return GRAPH_BASE.format(from_addr=from_address) + path


def _parse_message(raw_eml: bytes) -> ParsedMessage:
    """Parse a raw MIME message into body fields and raw attachment bytes."""
    msg = email_stdlib.message_from_bytes(raw_eml)
    subject = _decode_header(msg.get("Subject", "(no subject)"))

    body_content = ""
    body_type = "Text"
    attachments: list[AttachmentPart] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            if part.get_filename() or "attachment" in disposition:
                raw = part.get_payload(decode=True) or b""
                attachments.append(AttachmentPart(
                    name=_decode_header(part.get_filename() or "attachment"),
                    content_type=content_type,
                    data=raw,
                ))
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

    return ParsedMessage(
        subject=subject,
        body_content=body_content,
        body_type=body_type,
        attachments=attachments,
    )


def _graph_request(
    client: httpx.Client,
    method: str,
    url: str,
    token: str,
    *,
    immutable_id: bool = False,
    **kwargs,
) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    if immutable_id:
        headers["Prefer"] = IMMUTABLE_ID_PREFER
    for attempt in range(2):
        response = client.request(method, url, headers=headers, **kwargs)
        if response.status_code in (429, 503) and attempt == 0:
            wait = min(int(response.headers.get("Retry-After", "5")), 30)
            logger.warning(
                "Graph API throttled (HTTP %s), retrying in %ss", response.status_code, wait
            )
            time.sleep(wait)
            continue
        return response
    return response


def _create_draft(
    client: httpx.Client,
    token: str,
    from_address: str,
    parsed: ParsedMessage,
    to_addresses: list[str],
) -> str:
    url = _user_url(from_address, "/messages")
    body = {
        "subject": parsed.subject,
        "body": {"contentType": parsed.body_type, "content": parsed.body_content},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to_addresses],
    }
    response = _graph_request(
        client, "POST", url, token, immutable_id=True, json=body
    )
    response.raise_for_status()
    return response.json()["id"]


def _attach_small(
    client: httpx.Client,
    token: str,
    from_address: str,
    message_id: str,
    part: AttachmentPart,
) -> None:
    url = _user_url(from_address, f"/messages/{message_id}/attachments")
    body = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": part.name,
        "contentType": part.content_type,
        "contentBytes": base64.b64encode(part.data).decode(),
    }
    response = _graph_request(
        client, "POST", url, token, immutable_id=True, json=body
    )
    response.raise_for_status()


def _attach_large(
    client: httpx.Client,
    token: str,
    from_address: str,
    message_id: str,
    part: AttachmentPart,
    active_upload_url: list[str | None],
) -> None:
    url = _user_url(from_address, f"/messages/{message_id}/attachments/createUploadSession")
    item: dict = {
        "attachmentType": "file",
        "name": part.name,
        "size": part.size,
    }
    if part.content_type:
        item["contentType"] = part.content_type
    response = _graph_request(
        client, "POST", url, token, immutable_id=True, json={"AttachmentItem": item}
    )
    response.raise_for_status()
    upload_url = response.json()["uploadUrl"]
    active_upload_url[0] = upload_url

    total = part.size
    offset = 0
    with httpx.Client(timeout=UPLOAD_TIMEOUT) as upload_client:
        while offset < total:
            end = min(offset + UPLOAD_CHUNK_SIZE, total) - 1
            chunk = part.data[offset : end + 1]
            put_resp = upload_client.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total}",
                },
            )
            if put_resp.status_code not in (200, 201):
                put_resp.raise_for_status()
            if put_resp.status_code == 201:
                active_upload_url[0] = None
                return
            offset = end + 1
    active_upload_url[0] = None


def _send_draft(
    client: httpx.Client,
    token: str,
    from_address: str,
    message_id: str,
) -> None:
    url = _user_url(from_address, f"/messages/{message_id}/send")
    response = _graph_request(client, "POST", url, token, immutable_id=True)
    if response.status_code != 202:
        response.raise_for_status()


def _delete_message(
    client: httpx.Client,
    token: str,
    from_address: str,
    message_id: str,
    *,
    not_found_ok: bool = False,
) -> bool:
    """Delete a message. Returns True if deleted or already absent."""
    url = _user_url(from_address, f"/messages/{message_id}")
    response = _graph_request(client, "DELETE", url, token, immutable_id=True)
    if response.status_code == 204:
        return True
    if response.status_code == 404:
        return not_found_ok
    response.raise_for_status()
    return False


def _remove_sent_items_copy(
    client: httpx.Client,
    token: str,
    from_address: str,
    message_id: str,
) -> None:
    """Delete the Sent Items copy after send.

    Uses immutable message IDs so the ID remains valid after the draft is sent.
    Retries because Graph may not expose the Sent Items copy immediately.
    """
    time.sleep(SENT_ITEMS_DELETE_DELAY)
    url = _user_url(from_address, f"/messages/{message_id}")
    for attempt in range(SENT_ITEMS_DELETE_MAX_ATTEMPTS):
        if attempt > 0:
            time.sleep(SENT_ITEMS_DELETE_RETRY_INTERVAL)
        response = _graph_request(client, "DELETE", url, token, immutable_id=True)
        if response.status_code == 204:
            logger.debug("Removed Sent Items copy for message %s", message_id)
            return
        if response.status_code == 404:
            continue
        response.raise_for_status()
    logger.warning(
        "Could not remove Sent Items copy for message %s after %d attempts",
        message_id,
        SENT_ITEMS_DELETE_MAX_ATTEMPTS,
    )


def _remove_sent_items_copy_background(from_address: str, message_id: str) -> None:
    try:
        token = _acquire_token()
        with httpx.Client(timeout=GRAPH_JSON_TIMEOUT) as client:
            _remove_sent_items_copy(client, token, from_address, message_id)
    except Exception as exc:
        logger.warning(
            "Background Sent Items cleanup failed for message %s: %s", message_id, exc
        )


def _schedule_sent_items_removal(from_address: str, message_id: str) -> None:
    threading.Thread(
        target=_remove_sent_items_copy_background,
        args=(from_address, message_id),
        daemon=True,
        name="sent-items-cleanup",
    ).start()


def _cancel_upload_session(client: httpx.Client, upload_url: str) -> None:
    response = client.delete(upload_url)
    if response.status_code not in (204, 404):
        response.raise_for_status()


def _cleanup_failed_send(
    token: str,
    from_address: str,
    message_id: str | None,
    upload_url: str | None,
) -> None:
    if message_id is None and upload_url is None:
        return
    try:
        with httpx.Client(timeout=GRAPH_JSON_TIMEOUT) as client:
            if upload_url:
                _cancel_upload_session(client, upload_url)
            if message_id:
                _delete_message(client, token, from_address, message_id, not_found_ok=True)
    except Exception as exc:
        logger.warning("Failed to clean up draft %s: %s", message_id, exc)


def send_mail(
    from_address: str,
    to_addresses: list[str],
    raw_eml: bytes,
    *,
    save_to_sent_items: bool = False,
) -> None:
    """Forward a raw MIME message via Graph draft + attach + send.

    Attachments under 3 MB are added directly; larger files use an upload session.
    Retries once on HTTP 429 / 503 for Graph JSON calls.
    """
    parsed = _parse_message(raw_eml)
    for part in parsed.attachments:
        if part.size > LARGE_ATTACHMENT_MAX:
            raise ValueError(
                f"Attachment '{part.name}' exceeds the maximum size of "
                f"{LARGE_ATTACHMENT_MAX // (1024 * 1024)} MB"
            )

    token = _acquire_token()
    message_id: str | None = None
    active_upload_url: list[str | None] = [None]

    try:
        with httpx.Client(timeout=GRAPH_JSON_TIMEOUT) as client:
            message_id = _create_draft(client, token, from_address, parsed, to_addresses)
            for part in parsed.attachments:
                if part.size < SMALL_ATTACHMENT_MAX:
                    _attach_small(client, token, from_address, message_id, part)
                else:
                    _attach_large(
                        client, token, from_address, message_id, part, active_upload_url
                    )
            _send_draft(client, token, from_address, message_id)

        if not save_to_sent_items and message_id:
            _schedule_sent_items_removal(from_address, message_id)
    except Exception:
        _cleanup_failed_send(token, from_address, message_id, active_upload_url[0])
        raise


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
