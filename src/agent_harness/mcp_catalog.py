"""Track live MCP contracts in one investigation, separately from source evidence."""

from __future__ import annotations

import json
from typing import Any

from agent_harness.research import lookup_key

METADATA = {"mcp_list_tools", "mcp_tool_schema"}
PREFLIGHT_FAILURES = {
    "mcp_schema_changed": "схема изменилась до вызова",
    "mcp_input_mismatch": "параметры не подходят для текущей схемы",
    "mcp_tool_missing": "инструмент больше не найден в разрешённом каталоге",
    "mcp_tool_ambiguous": "имя инструмента имеет несколько определений",
}


def schema_key(args: dict[str, Any]) -> str:
    return lookup_key("mcp_tool_schema", {k: args.get(k) for k in ("connection_id", "tool")})


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(item.get("text", ""))
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def invalidate_schema(run: dict[str, Any], args: dict[str, Any]) -> None:
    key = schema_key(args)
    ref = run.get("_mcp_metadata_refs", {}).pop(key, None)
    if ref:
        run["evidence"] = [item for item in run["evidence"]
                           if item.get("kind") != "tool_metadata"
                           or [item["index_id"], item["id"]] != ref]
    # Keep the known revision as a precondition until the new contract is read.


def repair_call(
    run: dict[str, Any], args: dict[str, Any], conflict: str,
) -> dict[str, Any] | None:
    """Read again once per contract and failure, without replaying the rejected call."""
    invalidate_schema(run, args)
    key = schema_key(args)
    revision = run.get("_mcp_schema_revisions", {}).get(key, "")
    repair = [key, conflict, revision]
    repaired = run.setdefault("_mcp_contract_repairs", [])
    if repair in repaired:
        return None
    repaired.append(repair)
    refresh = {"connection_id": args["connection_id"], "refresh": True}
    if conflict in {"mcp_tool_missing", "mcp_tool_ambiguous", "mcp_dispatch_unknown_tool"}:
        return {"name": "mcp_list_tools", "args": refresh}
    return {"name": "mcp_tool_schema", "args": {**refresh, "tool": args["tool"]}}


def manifest_needs_refresh(
    run: dict[str, Any], name: str, args: dict[str, Any], item: dict[str, Any],
) -> bool:
    revision = item.get("mcp_catalog_revision")
    if not revision or name not in {*METADATA, "mcp_call"}:
        return False
    versions = run.setdefault("_mcp_catalog_revisions", {})
    previous = versions.get(item["index_id"])
    if not previous or (name == "mcp_list_tools" and not args.get("query")):
        versions[item["index_id"]] = revision
        return False
    return bool(previous != revision)


def record_metadata(
    run: dict[str, Any], name: str, args: dict[str, Any],
    old: dict[str, Any], new: dict[str, Any],
) -> dict[str, list[str]]:
    previous, current = _payload(old), _payload(new)
    changes: dict[str, list[str]] = {}
    if name == "mcp_tool_schema":
        version, tool = current.get("revision"), current.get("name")
        if (isinstance(tool, str) and tool == args.get("tool")
                and isinstance(version, str) and len(version) == 64):
            versions = run.setdefault("_mcp_schema_revisions", {})
            key = schema_key(args)
            if versions.get(key) and versions[key] != version:
                changes["changed"] = [tool]
            versions[key] = version
        return changes
    if "tools" not in previous or "tools" not in current:
        return {}

    def listing(data: dict[str, Any]) -> dict[str, Any]:
        return {item["name"]: item.get("revision") or item.get("description")
                for item in data["tools"]
                if isinstance(item, dict) and isinstance(item.get("name"), str)}

    before, after = listing(previous), listing(current)
    if not previous.get("truncated") and not args.get("query"):
        changes["added"] = sorted(after.keys() - before.keys())
    changes["changed"] = sorted(k for k in after.keys() & before.keys() if after[k] != before[k])
    if not current.get("truncated") and not args.get("query"):
        changes["removed"] = sorted(before.keys() - after.keys())
    for tool in changes["changed"] + changes.get("removed", []):
        target = {"connection_id": args["connection_id"], "tool": tool}
        known = run.get("_mcp_schema_revisions", {}).get(schema_key(target))
        if tool not in after or known != after[tool]:
            invalidate_schema(run, target)
    if any(changes.values()) and not args.get("query"):
        # Search-filtered catalogs are views of this same live connection.
        # They may now list removed tools or omit names that have just appeared.
        stale = {(e["index_id"], e["id"]) for e in run["evidence"] if
                 e["index_id"] == new["index_id"] and e["id"] != new["id"] and
                 e.get("kind") == "tool_metadata" and
                 _payload(e).get("kind") == "tool_catalog_not_system_evidence"}
        if stale:
            run["evidence"] = [e for e in run["evidence"] if (e["index_id"], e["id"]) not in stale]
            refs = run.get("_mcp_metadata_refs", {})
            run["_mcp_metadata_refs"] = {
                k: ref for k, ref in refs.items() if tuple(ref) not in stale
            }
    if any(len(tools) > 12 for tools in changes.values()):
        new["tool_change_counts"] = {event: len(tools) for event, tools in changes.items() if tools}
    return {event: tools[:12] for event, tools in changes.items() if tools}
