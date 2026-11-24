"""Track live MCP contracts in one investigation, separately from source evidence."""

from __future__ import annotations

import json
import re
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


def quick_schema_reads(
    item: dict[str, Any], connection_id: str, available_calls: int, attempted: set[str],
    question: str,
) -> list[dict[str, Any]]:
    """Offer likely schemas first and keep discovery available for the remaining manifest."""
    catalog = _payload(item)
    entries = catalog.get("tools")
    if catalog.get("kind") != "tool_catalog_not_system_evidence" or not isinstance(entries, list):
        return []
    calls = []
    def terms(text: str) -> set[str]:
        return set(re.findall(r"[^\W_]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).casefold()))

    query = terms(question)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(name := entry.get("name"), str):
            continue
        args = {"connection_id": connection_id, "tool": name}
        if schema_key(args) not in attempted:
            score = 2 * len(terms(name) & query) + len(terms(str(entry.get("description"))) & query)
            calls.append((score, {"name": "mcp_tool_schema", "args": args}))
    calls.sort(key=lambda pair: pair[0], reverse=True)
    return [call for _, call in calls[:max(0, min(4, available_calls))]]


def all_mcp_tools_inspected(
    run: dict[str, Any], wrapper: dict[str, Any], targets: dict[str, dict[str, str]],
) -> bool:
    connections = wrapper["function"]["parameters"]["properties"]["connection_id"].get("enum", [])
    inspected = {(t["connection_id"], t["tool"]) for t in targets.values()}
    refs = run.get("_mcp_metadata_refs", {})
    retained = {(item["index_id"], item["id"]): item for item in run["evidence"]}
    for connection in connections:
        key = lookup_key("mcp_list_tools", {"connection_id": connection})
        ref = refs.get(key)
        catalog = _payload(retained.get(tuple(ref), {})) if ref else {}
        if not isinstance(catalog.get("tools"), list) or catalog.get("truncated"):
            return False
        if any((connection, t.get("name")) not in inspected
               for t in catalog["tools"] if isinstance(t, dict)):
            return False
    return bool(connections)


def native_read_tools(
    run: dict[str, Any], tool_catalog: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Offer live, inspected contracts directly to a tool-calling chat model."""
    names = {t["function"]["name"] for t in tool_catalog}
    schemas: list[dict[str, Any]] = []
    targets: dict[str, dict[str, str]] = {}
    if "mcp_call" not in names:
        return schemas, targets
    for item in run["evidence"]:
        if item.get("kind") != "tool_metadata" or item["index_id"] not in run["index_ids"]:
            continue
        metadata = _payload(item)
        name, schema = metadata.get("name"), metadata.get("inputSchema")
        if ((run["mode"] != "quick" and metadata.get("read_only") is not True)
                or metadata.get("kind") != "tool_schema_not_system_evidence"
                or not isinstance(name, str)
                or not isinstance(schema, dict) or metadata.get("truncated")):
            continue
        target = {"connection_id": item["index_id"].removeprefix("connection:"), "tool": name}
        # Only the trusted receipt for this run can introduce a callable schema.
        if run.get("_mcp_metadata_refs", {}).get(schema_key(target)) != [
            item["index_id"], item["id"],
        ]:
            continue
        alias = re.sub("[^a-zA-Z0-9_-]", "_", name)[:64]
        if not alias or alias in names or alias in targets:
            alias = f"read_mcp_{len(targets) + 1}"
            while alias in names or alias in targets:
                alias += "_"
        targets[alias] = target
        schemas.append({"type": "function", "function": {
            "name": alias, "description": str(metadata.get("description", ""))[:1000],
            "parameters": schema,
        }})
    return schemas, targets


def text_read_call(content: str, targets: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Some chat adapters serialize a function decision into message.content."""
    body = content.strip()
    if body.startswith("```json\n") and body.endswith("```"):
        body = body[8:-3].strip()
    try:
        action = json.loads(body)
    except ValueError:
        return []
    if not isinstance(action, dict) or set(action) not in (
        {"name", "parameters"}, {"name", "arguments"},
    ):
        return []
    name, args = action["name"], action.get("parameters", action.get("arguments"))
    if not isinstance(name, str) or not isinstance(args, dict):
        return []
    # Accept whitespace/case variants only for an unambiguous inspected read.
    def normalized(value: str) -> str:
        return re.sub(r"[\s_-]+", "", value.casefold())

    matches = [alias for alias in targets if normalized(alias) == normalized(name)]
    return [{"name": matches[0], "args": args}] if len(matches) == 1 else []


_RECORD_ID = re.compile(
    r"(?<![\w-])(?:[A-Z][A-Z0-9]{1,14}-\d{1,16}|"
    r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})(?![\w-])", re.I | re.ASCII,
)


def question_record_id(question: str) -> str | None:
    ids = {key.casefold() for key in _RECORD_ID.findall(question)}
    return ids.pop() if len(ids) == 1 else None


def question_record_lookup(question: str, arguments: Any) -> bool:
    """Bind a read to one exact record ID, without persisting its arguments."""
    requested = question_record_id(question)
    if not requested or not isinstance(arguments, dict):
        return False
    fields = [value.casefold() for value in arguments.values() if isinstance(value, str)]
    observed = {key.casefold() for key in _RECORD_ID.findall(" ".join(fields))}
    return observed == {requested} and requested in fields


def read_chat(run: dict[str, Any]) -> bool:
    return run["mode"] == "quick" or bool(run.get("_mcp_record_lookup"))


def linked_read(
    run: dict[str, Any], source: dict[str, Any], tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve one explicit foreign key using a unique inspected read on the same server."""
    origin = source.get("tool_origin", {})
    if origin.get("tool") != "mcp_call" or source.get("tool_outcome"):
        return None
    data = _payload(source)
    if not data:
        # Plain-text MCP results commonly use a single key=value record.
        match = re.fullmatch(r"\s*([\w]+)\s*=\s*([\w.-]{1,180})\s*", source["text"])
        if not match:
            return None
        data = {match[1]: match[2]}
    schemas, targets = native_read_tools(run, tools)
    matching = []
    for schema in schemas:
        definition = schema["function"]
        target = targets[definition["name"]]
        if (origin.get("remote_tool") == target["tool"] or
                "connection:" + target["connection_id"] != source["index_id"]):
            continue
        required = definition["parameters"].get("required", [])
        if not isinstance(required, list) or len(required) != 1 or not isinstance(required[0], str):
            continue
        field = required[0]
        value = data.get(field)
        if (not re.search(r"(?:_(?:id|key|ref)|Id|Key|Ref)$", field)
                or type(value) not in (str, int) or not re.fullmatch(r"[\w.-]{1,180}", str(value))):
            continue
        args = {**target, "arguments": {field: value}}
        if (lookup_key("mcp_call", args) not in run.get("_issued_calls", [])
                and schema_key(target) not in run.get("_uncertain_mcp_tools", [])):
            matching.append({"name": "mcp_call", "args": args})
    return matching[0] if len(matching) == 1 else None


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
