from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID

from corporate_kb.access.dev_pki import (
    LocalIdentity,
    normalize_server_name,
    prepare_local_identity,
)


def _snapshot(directory: Path) -> dict[str, tuple[str, int]]:
    return {
        path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        for path in directory.iterdir()
        if path.is_file() and not path.is_symlink()
    }


def _public(key) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


@pytest.fixture
def identity(tmp_path: Path) -> LocalIdentity:
    return prepare_local_identity(tmp_path / "identity")


def test_generates_private_local_identity_and_reuses_exact_files(identity: LocalIdentity) -> None:
    directory = identity.ca_cert.parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert len(list(directory.iterdir())) == 7
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())
    assert identity.admin_username == "admin"
    assert len(identity.admin_password) >= 32
    assert len(identity.p12_password) >= 32
    assert identity.admin_password != identity.p12_password
    assert identity.admin_password not in repr(identity)
    assert identity.p12_password not in repr(identity)
    data = json.loads(identity.credentials_file.read_bytes())
    assert data == {
        "schema_version": 1,
        "admin_username": "admin",
        "admin_password": identity.admin_password,
        "p12_password": identity.p12_password,
    }
    snapshot = _snapshot(directory)
    assert prepare_local_identity(directory) == identity
    assert _snapshot(directory) == snapshot
    assert not (directory / "ca.key").exists()


def test_certificate_chain_purposes_and_localhost_sans(identity: LocalIdentity) -> None:
    ca = x509.load_pem_x509_certificate(identity.ca_cert.read_bytes())
    server = x509.load_pem_x509_certificate(identity.server_cert.read_bytes())
    client = x509.load_pem_x509_certificate(identity.client_cert.read_bytes())
    ca.verify_directly_issued_by(ca)
    constraints = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical
    assert constraints.value.ca
    assert constraints.value.path_length == 0
    usage = ca.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign and usage.crl_sign
    ca_identifier = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    assert ca_identifier.digest == x509.SubjectKeyIdentifier.from_public_key(ca.public_key()).digest
    for certificate, key_path, purpose in (
        (server, identity.server_key, ExtendedKeyUsageOID.SERVER_AUTH),
        (client, identity.client_key, ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        certificate.verify_directly_issued_by(ca)
        assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        assert set(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {
            purpose
        }
        assert (
            certificate.extensions.get_extension_for_class(
                x509.AuthorityKeyIdentifier
            ).value.key_identifier
            == ca_identifier.digest
        )
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert _public(key.public_key()) == _public(certificate.public_key())
        assert (
            timedelta(days=29)
            < certificate.not_valid_after_utc - datetime.now(UTC)
            <= timedelta(days=30)
        )
    assert len({_public(cert.public_key()) for cert in (ca, server, client)}) == 3
    sans = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["localhost"]
    assert set(sans.get_values_for_type(x509.IPAddress)) == {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    }
    assert len(sans) == 3


def test_pkcs12_contains_encrypted_matching_client_key_cert_and_ca(identity: LocalIdentity) -> None:
    data = identity.client_p12.read_bytes()
    with pytest.raises(ValueError):
        pkcs12.load_key_and_certificates(data, None)
    with pytest.raises(ValueError):
        pkcs12.load_key_and_certificates(data, b"incorrect-test-password")
    key, certificate, authorities = pkcs12.load_key_and_certificates(
        data, identity.p12_password.encode()
    )
    client = x509.load_pem_x509_certificate(identity.client_cert.read_bytes())
    ca = x509.load_pem_x509_certificate(identity.ca_cert.read_bytes())
    assert key is not None and certificate is not None
    assert _public(key.public_key()) == _public(client.public_key())
    assert certificate.fingerprint(hashes.SHA256()) == client.fingerprint(hashes.SHA256())
    assert len(authorities) == 1
    assert authorities[0].fingerprint(hashes.SHA256()) == ca.fingerprint(hashes.SHA256())


def test_independent_directories_get_unique_ca_keys_and_credentials(tmp_path: Path) -> None:
    first = prepare_local_identity(tmp_path / "first")
    second = prepare_local_identity(tmp_path / "second")
    assert first.ca_cert.read_bytes() != second.ca_cert.read_bytes()
    assert first.client_key.read_bytes() != second.client_key.read_bytes()
    assert first.admin_password != second.admin_password
    assert first.p12_password != second.p12_password


def test_empty_existing_directory_is_not_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "identity"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("damage", ["missing", "bad_cert", "bad_key", "bad_p12", "bad_json"])
def test_partial_and_corrupt_identity_is_not_repaired(identity: LocalIdentity, damage: str) -> None:
    if damage == "missing":
        identity.client_key.unlink()
    elif damage == "bad_cert":
        identity.ca_cert.write_bytes(b"not-a-certificate")
    elif damage == "bad_key":
        identity.server_key.write_bytes(b"not-a-key")
    elif damage == "bad_p12":
        identity.client_p12.write_bytes(b"not-a-pkcs12")
    else:
        identity.credentials_file.write_bytes(b"{")
    snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(identity.ca_cert.parent)
    assert _snapshot(identity.ca_cert.parent) == snapshot


@pytest.mark.parametrize("damage", ["schema", "username", "short_password", "p12_password"])
def test_invalid_credentials_are_not_reset(identity: LocalIdentity, damage: str) -> None:
    data = json.loads(identity.credentials_file.read_bytes())
    if damage == "schema":
        data["schema_version"] = 999
    elif damage == "username":
        data["admin_username"] = "unexpected"
    elif damage == "short_password":
        data["admin_password"] = "short"
    else:
        data["p12_password"] = "wrong-p12-password-with-enough-characters"
    identity.credentials_file.write_text(json.dumps(data))
    snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(identity.ca_cert.parent)
    assert _snapshot(identity.ca_cert.parent) == snapshot


def test_expired_identity_is_not_rotated(
    identity: LocalIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    future = datetime.now(UTC) + timedelta(days=40)
    monkeypatch.setattr("corporate_kb.access.dev_pki._now", lambda: future)
    snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(identity.ca_cert.parent)
    assert _snapshot(identity.ca_cert.parent) == snapshot


@pytest.mark.parametrize("part", ["server_key", "ca_cert", "client_p12"])
def test_mismatched_identity_material_is_rejected(
    identity: LocalIdentity, tmp_path: Path, part: str
) -> None:
    other = prepare_local_identity(tmp_path / "other")
    getattr(identity, part).write_bytes(getattr(other, part).read_bytes())
    snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(identity.ca_cert.parent)
    assert _snapshot(identity.ca_cert.parent) == snapshot


def test_swapped_leaf_purposes_rejected(identity: LocalIdentity) -> None:
    identity.server_cert.write_bytes(identity.client_cert.read_bytes())
    identity.server_key.write_bytes(identity.client_key.read_bytes())
    snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError, match="not changed"):
        prepare_local_identity(identity.ca_cert.parent)
    assert _snapshot(identity.ca_cert.parent) == snapshot


@pytest.mark.parametrize("part", ["directory", "client_key", "parent", "lock"])
def test_symlink_paths_are_never_followed(
    identity: LocalIdentity, tmp_path: Path, part: str
) -> None:
    original_snapshot = _snapshot(identity.ca_cert.parent)
    if part == "directory":
        path = tmp_path / "linked-identity"
        path.symlink_to(identity.ca_cert.parent, target_is_directory=True)
    elif part == "parent":
        parent = tmp_path / "linked-parent"
        parent.symlink_to(identity.ca_cert.parent, target_is_directory=True)
        path = parent / "nested"
    elif part == "lock":
        path = tmp_path / "another"
        (tmp_path / ".another.lock").symlink_to(identity.client_key)
    else:
        outside = tmp_path / "external-client.key"
        outside.write_bytes(identity.client_key.read_bytes())
        identity.client_key.unlink()
        identity.client_key.symlink_to(outside)
        path = identity.ca_cert.parent
        original_snapshot = _snapshot(identity.ca_cert.parent)
    with pytest.raises(ValueError):
        prepare_local_identity(path)
    assert _snapshot(identity.ca_cert.parent) == original_snapshot


@pytest.mark.parametrize("part", ["directory", "key", "parent"])
def test_unsafe_permissions_rejected_without_chmod(identity: LocalIdentity, part: str) -> None:
    target = (
        identity.ca_cert.parent
        if part == "directory"
        else identity.ca_cert.parent.parent
        if part == "parent"
        else identity.client_key
    )
    changed_mode = 0o777 if part == "parent" else 0o755 if part == "directory" else 0o644
    os.chmod(target, changed_mode)
    try:
        with pytest.raises(ValueError):
            prepare_local_identity(identity.ca_cert.parent)
        assert stat.S_IMODE(target.stat().st_mode) == changed_mode
    finally:
        os.chmod(target, 0o600 if part == "key" else 0o700)


def test_concurrent_creators_reuse_one_complete_bundle(tmp_path: Path) -> None:
    destination = tmp_path / "concurrent"
    with ThreadPoolExecutor(max_workers=6) as pool:
        identities = list(pool.map(lambda _: prepare_local_identity(destination), range(12)))
    assert all(item == identities[0] for item in identities)
    assert len(list(destination.iterdir())) == 7
    assert {item.name for item in tmp_path.iterdir()} == {"concurrent", ".concurrent.lock"}


def test_failed_generation_does_not_publish_or_leave_partial_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_generation(directory: Path, *, server_names: tuple[str, ...] = ()) -> None:
        (directory / "partial").write_text("non-secret test fixture")
        raise ValueError("simulated failure")

    monkeypatch.setattr("corporate_kb.access.dev_pki._generate", fail_generation)
    with pytest.raises(ValueError, match="simulated"):
        prepare_local_identity(tmp_path / "identity")
    assert not (tmp_path / "identity").exists()
    assert {item.name for item in tmp_path.iterdir()} == {".identity.lock"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Debian-BOX.Example.COM", "debian-box.example.com"),
        ("debian-box", "debian-box"),
        ("example.com.", "example.com"),
        ("example\u3002com\u3002", "example.com"),
        ("Пример.РФ", "xn--e1afmkfd.xn--p1ai"),
        ("XN--E1AFMKFD.XN--P1AI", "xn--e1afmkfd.xn--p1ai"),
        ("192.0.2.10", "192.0.2.10"),
        ("192.0.2.10.", "192.0.2.10"),
        ("2001:0DB8:0000:0000:0000:0000:0000:0010", "2001:db8::10"),
        ("::1", "::1"),
        ("LOCALHOST.", "localhost"),
    ],
)
def test_normalize_server_names(value: str, expected: str) -> None:
    assert normalize_server_name(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "localhost ",
        "\tlocalhost",
        "local host",
        "local\nhost",
        "local\x00host",
        "local\u200bhost",
        "https://example.com",
        "http://localhost",
        "example.com:8443",
        "example.com/path",
        "example.com\\path",
        "user@example.com",
        "example.com?query",
        "example.com#fragment",
        "*.example.com",
        "[::1]",
        "[::1]:8443",
        "fe80::1%eth0",
        "example.com%20",
        "_service.example.com",
        "-example.com",
        "example-.com",
        "example..com",
        ".example.com",
        "example.com..",
        "xn--.example",
        "0.0.0.0",
        "::",
        "0:0:0:0:0:0:0:0",
        "224.0.0.1",
        "239.255.255.255",
        "ff02::1",
        "::ffff:224.0.0.1",
        "::ffff:0.0.0.0",
        "127.0.0.999",
        "127.1",
        "2130706433",
        "127.000.000.001",
        "2001:db8:::1",
        "::1.",
        "a" * 64 + ".example",
        ".".join(["a" * 63] * 4),
    ],
)
def test_invalid_server_names_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_server_name(value)


def test_remote_names_add_dns_and_ip_sans_without_changing_client_identity(tmp_path: Path) -> None:
    identity = prepare_local_identity(
        tmp_path / "identity",
        server_names=("Debian.Example.COM.", "192.0.2.10", "2001:0DB8::10", "Пример.РФ"),
    )
    server = x509.load_pem_x509_certificate(identity.server_cert.read_bytes())
    sans = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(sans.get_values_for_type(x509.DNSName)) == {
        "localhost",
        "debian.example.com",
        "xn--e1afmkfd.xn--p1ai",
    }
    assert set(sans.get_values_for_type(x509.IPAddress)) == {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
        ipaddress.ip_address("192.0.2.10"),
        ipaddress.ip_address("2001:db8::10"),
    }
    assert len(sans) == 7
    client = x509.load_pem_x509_certificate(identity.client_cert.read_bytes())
    with pytest.raises(x509.ExtensionNotFound):
        client.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert set(client.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {
        ExtendedKeyUsageOID.CLIENT_AUTH
    }


def test_remote_name_reuse_normalizes_deduplicates_and_ignores_order(tmp_path: Path) -> None:
    destination = tmp_path / "identity"
    identity = prepare_local_identity(
        destination,
        server_names=("Example.COM", "192.0.2.10", "2001:db8::10"),
    )
    before = _snapshot(destination)
    reused = prepare_local_identity(
        destination,
        server_names=(
            "LOCALHOST.",
            "127.0.0.1",
            "0:0:0:0:0:0:0:1",
            "EXAMPLE.com.",
            "example.com",
            "2001:0DB8::10",
            "192.0.2.10",
        ),
    )
    assert reused == identity
    assert _snapshot(destination) == before


@pytest.mark.parametrize("requested", [(), ("other.example.com",), ("example.com", "192.0.2.10")])
def test_remote_name_mismatch_never_rotates_existing_bundle(
    tmp_path: Path, requested: tuple[str, ...]
) -> None:
    destination = tmp_path / "identity"
    original = prepare_local_identity(destination, server_names=("example.com",))
    before = _snapshot(destination)
    with pytest.raises(ValueError, match=r"server names.*not changed"):
        prepare_local_identity(destination, server_names=requested)
    assert _snapshot(destination) == before
    assert prepare_local_identity(destination, server_names=("EXAMPLE.COM.",)) == original


def test_default_bundle_cannot_silently_gain_remote_hosts(identity: LocalIdentity) -> None:
    directory = identity.ca_cert.parent
    before = _snapshot(directory)
    with pytest.raises(ValueError, match=r"server names.*not changed"):
        prepare_local_identity(directory, server_names=("debian.example.com",))
    assert _snapshot(directory) == before
    assert prepare_local_identity(directory) == identity


def test_invalid_host_validation_precedes_directory_or_lock_creation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        prepare_local_identity(tmp_path / "new-parent" / "identity", server_names=("0.0.0.0",))
    assert not (tmp_path / "new-parent").exists()
    with pytest.raises(ValueError, match="tuple"):
        prepare_local_identity(tmp_path / "identity", server_names="example.com")
    assert not list(tmp_path.iterdir())


def test_native_windows_fails_with_wsl_guidance_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("corporate_kb.access.dev_pki.sys.platform", "win32")
    with pytest.raises(ValueError, match="Windows use WSL"):
        prepare_local_identity(tmp_path / "identity")
    assert not list(tmp_path.iterdir())
