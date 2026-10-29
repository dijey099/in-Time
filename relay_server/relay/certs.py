"""Self-signed HTTPS certificate for the LAN (browsers only allow camera access
over HTTPS). Regenerated automatically when the relay's LAN IP changes."""
import datetime
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def lan_ips():
    ips = set()
    try:  # IP of the interface used for the default route (no packet is sent)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("0.0.0.0")
    ordered = sorted(ips, key=lambda ip: (ip.startswith("127."), ip))
    return ordered or ["127.0.0.1"]


def _cert_ips(cert_path: Path):
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        not_after = cert.not_valid_after_utc
        if not_after < datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30):
            return set()
        return {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    except Exception:
        return set()


def ensure_certificate(certs_dir: Path):
    cert_path, key_path = certs_dir / "relay.crt", certs_dir / "relay.key"
    ips = set(lan_ips()) | {"127.0.0.1"}
    if cert_path.exists() and key_path.exists() and ips <= _cert_ips(cert_path):
        return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "in-Time relay")])
    host = socket.gethostname()
    san = [x509.DNSName("localhost"), x509.DNSName(host)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path
