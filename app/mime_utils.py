from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import collapse_rfc2231_value


def parse_message(raw_eml: bytes):
    """Parse raw MIME bytes with modern email policy."""
    return BytesParser(policy=policy.default).parsebytes(raw_eml)


def decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 / encoded-word headers to unicode text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        parts = decode_header(value)
        decoded: list[str] = []
        for fragment, charset in parts:
            if isinstance(fragment, bytes):
                try:
                    decoded.append(fragment.decode(charset or "utf-8", errors="replace"))
                except (LookupError, TypeError):
                    decoded.append(fragment.decode("utf-8", errors="replace"))
            else:
                decoded.append(fragment)
        return "".join(decoded)


def extract_subject(raw_eml: bytes) -> str:
    msg = parse_message(raw_eml)
    subject = msg.get("Subject")
    if subject is None:
        return ""
    return decode_mime_header(str(subject))


def decode_filename(value: str | None) -> str:
    if not value:
        return "attachment"
    return decode_mime_header(collapse_rfc2231_value(value))

