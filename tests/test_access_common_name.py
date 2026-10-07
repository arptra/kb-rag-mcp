"""CN parsing is shared by new TLS enrollment and migration of old subject metadata."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from corporate_kb.access.models import (
    common_name_from_certificate,
    common_name_from_subject,
    normalize_common_name,
)
from corporate_kb.access.tls import identity_from_der


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("CN=alice,O=Company", "alice"),
        ("O=Company,CN=Alice", "Alice"),
        (r"CN=Doe\, John,O=Company", "Doe, John"),
        (r"CN=\ Alice\ ,O=Company", "Alice"),
        ("CN=Алексей,O=Компания", "Алексей"),
        ("CN=Jose\u0301", "José"),
        ("O=Company", None),
        ("CN=alice,CN=bob", None),
        ("CN=alice+CN=bob", None),
        ("CN=", None),
        ("CN=\u200bhidden", None),
        ("CN=bad\nname", None),
        ("CN=" + "a" * 257, None),
        ("not a subject", None),
    ],
)
def test_common_name_parsing(subject, expected):
    assert common_name_from_subject(subject) == expected


@pytest.mark.parametrize("value", ["", " ", "alice\n", "\x00alice", "a\u200db", "\ud800"])
def test_common_name_rejects_empty_controls_and_invalid_unicode(value):
    with pytest.raises(ValueError):
        normalize_common_name(value)


def test_normalization_does_not_conflate_different_case():
    assert normalize_common_name("Alice") != normalize_common_name("alice")


@pytest.mark.parametrize("name", ["alice", "я" * 33, "x" * 65])
@pytest.mark.filterwarnings("ignore:Attribute's length must be:UserWarning")
def test_real_certificate_cn_is_not_reparsed_from_subject_text(name):
    key = ec.generate_private_key(ec.SECP256R1())
    # Test legacy certificates which exceed the constructor's 64-byte CN limit.
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name, _validate=False)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    identity = identity_from_der(certificate.public_bytes(serialization.Encoding.DER))
    assert identity.common_name == name
    # Stored display metadata cannot override the actual certificate's CN.
    assert common_name_from_certificate(identity.certificate_pem, "CN=someone-else") == name


def test_historical_text_only_record_uses_subject_fallback():
    assert common_name_from_certificate("legacy-placeholder", "CN=alice") == "alice"
