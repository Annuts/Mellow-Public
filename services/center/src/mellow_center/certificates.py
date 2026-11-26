"""Read-only public TLS identity, independent of the CLI and ASGI application."""
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization


def fingerprint(directory):
    cert = x509.load_pem_x509_certificate((directory / 'tls.crt').read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def tls_status(directory) -> dict[str, Any]:
    try:
        cert = x509.load_pem_x509_certificate((directory / 'tls.crt').read_bytes())
    except FileNotFoundError:
        return {}
    expiry = cert.not_valid_after_utc
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return dict(tls_spki_sha256=hashlib.sha256(spki).hexdigest(),
                tls_sha256=hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
                tls_expires_at=expiry.isoformat(), tls_expiring=expiry < datetime.now(timezone.utc) + timedelta(days=30))
