"""The router's self-signed certificate for its HTTPS listener (secrets.https_enabled).

Made once, under <data>/tls/ (key file 0600), and kept across restarts. Clients
do not check a CA. They pin the certificate's SHA-256 fingerprint, which
Setup → Secrets shows (slot-secrets: FOUNDRY_TLS_FINGERPRINT).
"""

from __future__ import annotations

import datetime
import hashlib
import os
import socket
from pathlib import Path
from typing import Optional

CERT = "router.crt"
KEY = "router.key"


def paths(data: Path) -> tuple[Path, Path]:
    d = Path(data) / "tls"
    return d / CERT, d / KEY


def fingerprint(cert_path: Path) -> Optional[str]:
    """'AB:CD:…' SHA-256 of the certificate (DER), or None when there is none."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
        der = cert.public_bytes(Encoding.DER)
    except (OSError, ValueError, ImportError):
        return None
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def ensure_cert(data: Path) -> tuple[Path, Path, str]:
    """(cert path, key path, fingerprint), making the pair when missing."""
    cert_path, key_path = paths(data)
    if cert_path.exists() and key_path.exists():
        fp = fingerprint(cert_path)
        if fp:
            return cert_path, key_path, fp
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(cert_path.parent, 0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Foundry Router"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Foundry")])
    now = datetime.datetime.now(datetime.timezone.utc)
    alt = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname() or "foundry-router")]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .sign(key, hashes.SHA256()))
    key_bytes = key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption())
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, fingerprint(cert_path) or ""
