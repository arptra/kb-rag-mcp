"""Isolated, reusable localhost identities; never installs trust or contacts a CA.

This is development-only PKI. The signing key exists only during initial bundle
generation. Existing identity material is validated, never silently repaired or
rotated, so restarts cannot reset access-registry principals or credentials.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import secrets
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_FILES = {
    "ca_cert": "ca.pem",
    "server_cert": "server.pem",
    "server_key": "server.key",
    "client_cert": "client.pem",
    "client_key": "client.key",
    "client_p12": "client.p12",
    "credentials_file": "credentials.json",
}
_CREDENTIAL_FIELDS = {"schema_version", "admin_username", "admin_password", "p12_password"}
_MAX_FILE_BYTES = 131_072


@dataclass(frozen=True)
class LocalIdentity:
    ca_cert: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    client_p12: Path
    credentials_file: Path
    admin_password: str = field(repr=False)
    p12_password: str = field(repr=False)
    admin_username: str = "admin"


def _now() -> datetime:
    return datetime.now(UTC)


def _reject_symlinks(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Local identity paths must not contain symbolic links")


def _read_private_file(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Local identity files must be regular, private 0600 files")
    if metadata.st_size > _MAX_FILE_BYTES:
        raise ValueError("Local identity file exceeds the size limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Local identity file changed while opening it")
        data = handle.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Local identity file exceeds the size limit")
    return data


def _credentials(data: bytes) -> dict[str, Any]:
    value = json.loads(data)
    if (
        not isinstance(value, dict)
        or set(value) != _CREDENTIAL_FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["admin_username"] != "admin"
    ):
        raise ValueError("Invalid local identity credential schema")
    for key in ("admin_password", "p12_password"):
        secret = value[key]
        if (
            not isinstance(secret, str)
            or not 32 <= len(secret) <= 1024
            or not secret.isascii()
            or not secret.isprintable()
        ):
            raise ValueError("Invalid local identity credential format")
    if value["admin_password"] == value["p12_password"]:
        raise ValueError("Local administrator and PKCS#12 credentials must be distinct")
    return value


def _public_bytes(key: Any) -> bytes:
    return bytes(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def _check_dates(certificate: x509.Certificate) -> None:
    if not certificate.not_valid_before_utc <= _now() < certificate.not_valid_after_utc:
        raise ValueError("Local identity certificate is expired or not yet valid")


def _check_leaf(
    certificate: x509.Certificate,
    key: ec.EllipticCurvePrivateKey,
    ca: x509.Certificate,
    purpose: x509.ObjectIdentifier,
) -> None:
    _check_dates(certificate)
    certificate.verify_directly_issued_by(ca)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    if not constraints.critical or constraints.value.ca or not usage.digital_signature:
        raise ValueError("Invalid local leaf certificate constraints")
    if usage.key_cert_sign or usage.crl_sign:
        raise ValueError("Local leaf certificate must not sign certificates")
    if set(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != {
        purpose
    }:
        raise ValueError("Incorrect local leaf certificate purpose")
    authority = certificate.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    identifier = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    if authority.key_identifier != identifier.digest:
        raise ValueError("Local leaf certificate authority does not match the CA")
    if _public_bytes(certificate.public_key()) != _public_bytes(key.public_key()):
        raise ValueError("Local certificate and private key do not match")
    if certificate.not_valid_after_utc > ca.not_valid_after_utc:
        raise ValueError("Local leaf certificate outlives its CA")


def _load(directory: Path) -> LocalIdentity:
    """Validate the whole bundle before returning any sensitive credential."""
    try:
        _reject_symlinks(directory)
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise ValueError("Local identity directory must have mode 0700")
        paths = {name: directory / filename for name, filename in _FILES.items()}
        data = {name: _read_private_file(path) for name, path in paths.items()}
        credentials = _credentials(data["credentials_file"])
        ca = x509.load_pem_x509_certificate(data["ca_cert"])
        server = x509.load_pem_x509_certificate(data["server_cert"])
        client = x509.load_pem_x509_certificate(data["client_cert"])
        server_key = serialization.load_pem_private_key(data["server_key"], password=None)
        client_key = serialization.load_pem_private_key(data["client_key"], password=None)
        if not isinstance(server_key, ec.EllipticCurvePrivateKey) or not isinstance(
            client_key, ec.EllipticCurvePrivateKey
        ):
            raise ValueError("Unsupported local identity private key type")
        _check_dates(ca)
        ca.verify_directly_issued_by(ca)
        constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints)
        usage = ca.extensions.get_extension_for_class(x509.KeyUsage).value
        identifier = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        if (
            not constraints.critical
            or not constraints.value.ca
            or constraints.value.path_length != 0
            or not usage.key_cert_sign
            or not usage.crl_sign
            or identifier.digest
            != x509.SubjectKeyIdentifier.from_public_key(ca.public_key()).digest
        ):
            raise ValueError("Invalid local certificate authority constraints")
        _check_leaf(server, server_key, ca, ExtendedKeyUsageOID.SERVER_AUTH)
        _check_leaf(client, client_key, ca, ExtendedKeyUsageOID.CLIENT_AUTH)
        public_keys = {
            _public_bytes(certificate.public_key()) for certificate in (ca, server, client)
        }
        if len(public_keys) != 3:
            raise ValueError("CA, server and client must have independent private keys")
        names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if (
            len(names) != 3
            or names.get_values_for_type(x509.DNSName) != ["localhost"]
            or set(names.get_values_for_type(x509.IPAddress))
            != {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
        ):
            raise ValueError("Local server certificate must be restricted to localhost")
        p12_key, p12_cert, p12_chain = pkcs12.load_key_and_certificates(
            data["client_p12"], credentials["p12_password"].encode("ascii")
        )
        if (
            p12_key is None
            or p12_cert is None
            or len(p12_chain or []) != 1
            or _public_bytes(p12_key.public_key()) != _public_bytes(client_key.public_key())
            or p12_cert.fingerprint(hashes.SHA256()) != client.fingerprint(hashes.SHA256())
            or p12_chain[0].fingerprint(hashes.SHA256()) != ca.fingerprint(hashes.SHA256())
        ):
            raise ValueError("PKCS#12 contents do not match the local client identity")
        try:
            pkcs12.load_key_and_certificates(data["client_p12"], None)
        except ValueError:
            pass
        else:
            raise ValueError("Local PKCS#12 must be encrypted")
        return LocalIdentity(
            **paths,
            admin_username=credentials["admin_username"],
            admin_password=credentials["admin_password"],
            p12_password=credentials["p12_password"],
        )
    except Exception as error:
        # Third-party decoder errors must not echo credential or key bytes.
        raise ValueError(
            "Existing local identity is incomplete, invalid, expired, or not private; "
            "it was not changed. Use a different empty identity location to start fresh."
        ) from error


def _write_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _key_usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def _generate(directory: Path) -> None:
    now = _now()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Corporate KB Local CA {uuid.uuid4().hex[:12]}")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=31))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_file(directory / _FILES["ca_cert"], ca.public_bytes(serialization.Encoding.PEM))
    keys = {}
    certificates = {}
    for kind, common_name, purpose in (
        ("server", "localhost", ExtendedKeyUsageOID.SERVER_AUTH),
        ("client", "Local Development User", ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_key_usage(ca=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([purpose]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False,
            )
        )
        if kind == "server":
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        x509.IPAddress(ipaddress.ip_address("::1")),
                    ]
                ),
                critical=False,
            )
        certificate = builder.sign(ca_key, hashes.SHA256())
        keys[kind] = key
        certificates[kind] = certificate
        _write_file(
            directory / _FILES[f"{kind}_cert"], certificate.public_bytes(serialization.Encoding.PEM)
        )
        _write_file(
            directory / _FILES[f"{kind}_key"],
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    credentials = {
        "schema_version": 1,
        "admin_username": "admin",
        "admin_password": secrets.token_urlsafe(32),
        "p12_password": secrets.token_urlsafe(32),
    }
    # Development-only import compatibility with macOS/Windows certificate stores.
    # PKCS#12 encryption is not a security boundary: keep the whole bundle private
    # (0700/0600) and never distribute these keys. Production PKI is separate.
    encryption = (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(50_000)
        .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
        .hmac_hash(hashes.SHA1())
        .build(str(credentials["p12_password"]).encode("ascii"))
    )
    p12 = pkcs12.serialize_key_and_certificates(
        b"Corporate KB Local Development Client",
        keys["client"],
        certificates["client"],
        [ca],
        encryption,
    )
    _write_file(directory / _FILES["client_p12"], p12)
    _write_file(
        directory / _FILES["credentials_file"],
        (json.dumps(credentials, indent=2) + "\n").encode("utf-8"),
    )


@contextmanager
def _identity_lock(directory: Path) -> Iterator[None]:
    """Serialize cooperating launchers without deleting/replacing the lock inode."""
    parent = directory.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise ValueError("Local identity parent must be owned by you and not writable by others")
    path = directory.parent / f".{directory.name}.lock"
    _reject_symlinks(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
        ):
            raise ValueError("Local identity lock must be a private regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Local identity lock changed while acquiring it")
        yield
    finally:
        os.close(descriptor)


def prepare_local_identity(directory: Path) -> LocalIdentity:
    """Generate once or validate/reuse a private localhost PKI bundle.

    Existing directories must contain a complete valid bundle; in particular an
    empty pre-created directory is not overwritten. Concurrent creators return the
    one winning identity. The CA is not installed in any operating-system store.
    """
    directory = Path(directory).absolute()
    _reject_symlinks(directory)
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlinks(directory.parent)
    with _identity_lock(directory):
        if directory.exists():
            return _load(directory)
        temporary = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
        try:
            os.chmod(temporary, 0o700)
            _generate(temporary)
            _load(temporary)
            descriptor = os.open(temporary, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # The private parent and persistent advisory lock serialize launchers.
            # Never replace an existing directory, including an incomplete bundle.
            if directory.exists() or directory.is_symlink():
                return _load(directory)
            os.rename(temporary, directory)
            return _load(directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
