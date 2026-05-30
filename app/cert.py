import logging
import ssl
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import app.config as config

logger = logging.getLogger(__name__)

CERT_FILE = config.CERTS_DIR / "cert.pem"
KEY_FILE = config.CERTS_DIR / "key.pem"
RENEWAL_THRESHOLD_DAYS = 90
CERT_VALIDITY_DAYS = 365


def _custom_cert_configured() -> bool:
    return bool(config.SMTP_CERT_FILE and config.SMTP_KEY_FILE)


def ensure_certificate() -> None:
    """Generate a new self-signed certificate if none exists or it expires soon.
    Skipped entirely when SMTP_CERT_FILE and SMTP_KEY_FILE are set."""
    if _custom_cert_configured():
        cert_path = config.SMTP_CERT_FILE
        key_path = config.SMTP_KEY_FILE
        if not Path(cert_path).exists() or not Path(key_path).exists():
            raise FileNotFoundError(
                f"Custom TLS cert/key not found: {cert_path}, {key_path}"
            )
        logger.info("Using custom TLS certificate: %s", cert_path)
        return

    if CERT_FILE.exists() and KEY_FILE.exists():
        try:
            cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
            expiry = cert.not_valid_after_utc
            days_left = (expiry - datetime.now(timezone.utc)).days
            if days_left > RENEWAL_THRESHOLD_DAYS:
                logger.info(f"TLS certificate valid for {days_left} more days — no renewal needed.")
                return
            logger.info(f"TLS certificate expires in {days_left} days — renewing.")
        except Exception as exc:
            logger.warning(f"Could not read existing certificate ({exc}) — regenerating.")

    _generate_certificate()


def _generate_certificate() -> None:
    hostname = config.SMTP_HOSTNAME
    logger.info(f"Generating self-signed TLS certificate for CN={hostname}")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    logger.info("TLS certificate written to disk.")


def create_ssl_context() -> ssl.SSLContext:
    cert = config.SMTP_CERT_FILE or CERT_FILE
    key = config.SMTP_KEY_FILE or KEY_FILE
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx
