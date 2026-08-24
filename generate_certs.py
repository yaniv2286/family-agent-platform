#!/usr/bin/env python3
"""Generate a self-signed certificate for local HTTPS testing."""
from pathlib import Path
import datetime
import ipaddress
import socket

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"


def generate_self_signed_cert():
    if Path(CERT_FILE).exists() and Path(KEY_FILE).exists():
        print(f"{CERT_FILE} and {KEY_FILE} already exist. Delete them to regenerate.")
        return

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "IL"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Local"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, "Local"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Family Agent Platform"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )

    # Subject Alternative Names for the browser/security warning.
    san_hosts = [x509.DNSName("localhost"), x509.DNSName("127.0.0.1")]
    try:
        hostname = socket.gethostname()
        san_hosts.append(x509.DNSName(hostname))
        addr = socket.getaddrinfo(hostname, None, family=socket.AF_INET)[0][4][0]
        san_hosts.append(x509.IPAddress(ipaddress.IPv4Address(addr)))
    except Exception:
        pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_hosts), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(KEY_FILE, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    print(f"Generated {CERT_FILE} and {KEY_FILE}")


if __name__ == "__main__":
    generate_self_signed_cert()
