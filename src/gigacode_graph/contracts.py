"""Protocol contract normalization shared by static matching and LLM verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class NormalizedContract:
    protocol: str
    method: str
    address: str

    @property
    def key(self) -> tuple[str, str, str]:
        return self.protocol, self.method, self.address


def normalize_contract(protocol: str, operation: str) -> NormalizedContract:
    normalized_protocol = protocol.strip().upper() or "UNKNOWN"
    value = " ".join(operation.strip().split())
    if normalized_protocol == "KAFKA":
        topic = value.removeprefix("KAFKA ").strip().strip("\"'")
        return NormalizedContract("KAFKA", "EVENT", topic)
    if normalized_protocol != "HTTP":
        return NormalizedContract(normalized_protocol, "ANY", value.lower())
    match = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY)\s+(.+)$", value, re.I)
    method = match.group(1).upper() if match else "ANY"
    raw_path = match.group(2) if match else value
    if "://" in raw_path:
        parsed = urlparse(raw_path)
        raw_path = parsed.path or "/"
    raw_path = raw_path.split("?", 1)[0].split("#", 1)[0]
    raw_path = re.sub(r"\$\{[^}]+}", "{}", raw_path)
    raw_path = re.sub(r"\{[^/{}]+}", "{}", raw_path)
    raw_path = re.sub(r"(?<=/)\:[A-Za-z_]\w*", "{}", raw_path)
    raw_path = re.sub(r"/+", "/", f"/{raw_path.lstrip('/')}")
    if len(raw_path) > 1:
        raw_path = raw_path.rstrip("/")
    return NormalizedContract("HTTP", method, raw_path.lower())


def contracts_compatible(
    outbound_protocol: str,
    outbound_operation: str,
    inbound_protocol: str,
    inbound_operation: str,
) -> bool:
    outbound = normalize_contract(outbound_protocol, outbound_operation)
    inbound = normalize_contract(inbound_protocol, inbound_operation)
    if outbound.protocol != inbound.protocol or outbound.address != inbound.address:
        return False
    return (
        outbound.protocol != "HTTP"
        or outbound.method == inbound.method
        or "ANY" in {outbound.method, inbound.method}
    )
