"""Minimal private test-client export; never includes server/admin credentials."""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from pathlib import Path

from corporate_kb.access.dev_pki import LocalIdentity


def export_client_bundle(identity: LocalIdentity, directory: Path, server_url: str) -> Path:
    """Write one generated ZIP for secure transfer to the user's browser computer."""
    destination = directory / "client-import.zip"
    if destination.is_symlink() or (
        destination.exists() and not stat.S_ISREG(destination.stat().st_mode)
    ):
        raise ValueError("Client export must be a regular file, not a symlink")
    instructions = f"""Corporate KB test client — PRIVATE, one test identity only

Open {server_url}/connect after importing the certificates ON THE BROWSER COMPUTER.

ca.crt: public test CA. Trust it only for this experiment.
client.p12: encrypted personal certificate WITH its private key.
Get the P12 password separately from the server operator. It is NOT in this ZIP.

Windows: import ca.crt into Current User / Trusted Root Certification Authorities,
and client.p12 into Current User / Personal.
macOS: Keychain Access / login; import ca.crt, enable SSL trust, then import client.p12.
Linux/Firefox: Settings / Privacy & Security / Certificates / View Certificates;
import ca.crt under Authorities (identify websites), and client.p12 under Your Certificates.
Browser menus and trust stores vary. Restart the browser after import.

Select your personal certificate and click the MCP configuration button on /connect.
The MCP client must also trust ca.crt. This ZIP is NOT an MCP configuration or token.
Use {server_url}/admin for the regular dashboard. Admin access is separate.

Transfer this ZIP privately (for example via SSH/SCP); share its password separately.
Do not distribute this identity to multiple users. Do not commit this ZIP or tokens.
Never copy server.key, credentials.json or the entire server state directory.
The server keeps a test copy of this client key; production users need their own PKI.
After the experiment, remove this CA and identity from the client's certificate store.
"""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".client-import-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in (
                    ("ca.crt", identity.ca_cert.read_bytes()),
                    ("client.p12", identity.client_p12.read_bytes()),
                    ("README.txt", instructions.encode("utf-8")),
                ):
                    entry = zipfile.ZipInfo(name)
                    entry.create_system = 3
                    entry.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(entry, data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
