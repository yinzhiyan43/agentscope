# -*- coding: utf-8 -*-
"""Prepare enterprise read tools and reconcile the Agent's publications."""

from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

from pydantic import BaseModel

from agentscope.state import AgentState
from agentscope.tool import ToolBase
from agentscope.tool._business_search import PublishedBusinessTool

from ._business import as_tool, preview
from ._business import definition_digest as _digest
from ._enterprise_config import EnterpriseLimits, EnterpriseReadAPI, LoginAdapterError

if TYPE_CHECKING:
    from ..app._enterprise_login import EnterpriseLogin


def _public_records(records: list[dict]) -> list[dict]:
    keys = (
        "name",
        "label",
        "method",
        "route",
        "evidence",
        "status",
        "reason",
        "acceptance",
        "authentication_issue",
        "diagnostic",
    )
    return [{k: record[k] for k in keys if k in record} for record in records]


def _record_revision(record: dict) -> str:
    plan = record.get("artifact", {}).get("plan") or record.get("source_plan")
    return _digest(
        [record.get("document_id"), record.get("source_location"), plan]
    )


def _candidate(record: dict) -> dict:
    """Keep enough evidence to correlate an inventory with an actual import."""
    plan = record.get("artifact", {}).get("plan") or record.get("source_plan")
    candidate = {
        key: record[key]
        for key in (
            "name",
            "label",
            "method",
            "route",
            "business",
            "evidence",
            "reason",
            "diagnostic",
        )
        if key in record
    }
    candidate["revision"] = _record_revision(record)
    traced = (plan and not plan.get("directOpenAPI")) or len(
        record.get("evidence", [])
    ) > 1
    candidate["basis"] = "source" if traced else "api"
    return candidate


def _inventory_read(record: dict) -> dict:
    """Keep the traced source plan; rebind wire schemas from the approved contract."""
    read = {
        k: record[k]
        for k in (
            "name",
            "label",
            "business",
            "route",
            "document_id",
            "source_location",
            "authentication",
            "reason",
        )
        if k in record
    }
    plan = record["artifact"]["plan"]
    if not plan.get("directOpenAPI"):
        read["client_plan"] = plan
    return read


def _brief_record(
    record: dict, limits: EnterpriseLimits, *, include_evidence: bool
) -> dict:
    row = {
        key: str(record[key])[: limits.summary_chars]
        for key in (
            "name",
            "label",
            "route",
            "method",
            "basis",
            "status",
            "reason",
            "authentication_issue",
        )
        if record.get(key)
    }
    if record.get("acceptance"):
        row["acceptance"] = {
            k: str(v)[: limits.summary_chars]
            for k, v in record["acceptance"].items()
        }
    if record.get("diagnostic"):
        row["diagnostic"] = record["diagnostic"]
    if include_evidence or "acceptance" in row:
        row["evidence"] = [
            str(item)[: limits.evidence_chars]
            for item in record.get("evidence", [])[
                : limits.evidence_per_record
            ]
        ]
    return row


def _chat_receipt(result: dict, limits: EnterpriseLimits) -> dict:
    """Give the model counts and a bounded sample of the persisted result."""
    tools = result["tools"]
    registered = sum(tool["status"] == "registered" for tool in tools)
    sample = [
        _brief_record(tool, limits, include_evidence=False)
        for tool in tools[: limits.page_size]
    ]
    return {
        "inventory_id": result["inventory_id"],
        "kind": result["kind"],
        "total": len(tools),
        "registered": registered,
        "pending": len(tools) - registered,
        "pending_reasons": dict(
            Counter(t["reason"] for t in tools if t.get("reason"))
        ),
        "diagnostics": [
            s[: limits.summary_chars]
            for s in result["diagnostics"][: limits.diagnostic_count]
        ],
        "tools": sample,
        "omitted": len(tools) - len(sample),
    }


def _sample_inputs(artifact: dict, sample: dict) -> dict:
    """Map a wire sample onto a traced action without changing the probe."""
    plan = artifact["plan"]
    if (
        plan.get("directOpenAPI")
        or not sample
        or set(sample) - {"path", "query", "body"}
    ):
        return sample
    expected_path = sample.get("path", {})
    expected_query = sample.get("query", {})
    if not isinstance(expected_path, dict) or not isinstance(
        expected_query, dict
    ):
        raise ValueError("Expected path and query parameter maps")
    fields = artifact["inputSchema"]["properties"]
    by_name = {
        k: v
        for k, v in {**expected_path, **expected_query}.items()
        if k in fields
    }
    request = plan["functions"][plan["entry"]]["steps"][-1]["return"]
    for name, expression in zip(
        artifact["operation"]["pathNames"], request.get("bindings", [])
    ):
        parameter = expression.get("variable")
        if parameter in fields and name in expected_path:
            by_name[parameter] = expected_path[name]
    query_expression = request.get("query", {})
    if query_expression.get("variable") in fields:
        by_name[query_expression["variable"]] = expected_query
    for key, expression in query_expression.get("object", {}).items():
        if expression.get("variable") in fields and key in expected_query:
            by_name[expression["variable"]] = expected_query[key]
    template = artifact["operation"]["route"]
    for candidate in (sample, by_name):
        try:
            wire = preview(artifact, candidate)
        except (ValueError, TypeError, KeyError):
            continue
        if expected_path:
            segments = dict(
                zip(template.split("/"), wire["path"].split("/"), strict=True)
            )
            if any(
                unquote(segments.get("{" + name + "}", "")) != str(value)
                for name, value in expected_path.items()
            ):
                continue
        if "body" in sample and wire.get("body") != sample["body"]:
            continue
        if all(
            k in wire.get("query", {}) and wire["query"][k] == v
            for k, v in expected_query.items()
        ):
            return candidate
    raise ValueError(
        "Verification inputs do not reproduce the approved API sample"
    )


def _login_key(record: dict, api: "EnterpriseReadAPI") -> str | None:
    profile = record.get("authentication", {"mode": "legacy"})
    if profile["mode"] in ("public", "legacy") or (
        profile["mode"] in ("bearer", "header")
        and not api.login_adapter
        and not profile.get("client")
        and api.token
        and "login" not in profile
    ):
        return None
    parts = [api.key, record["document_id"], profile]
    return _digest(parts)


def _runtime(
    record: dict, api: "EnterpriseReadAPI", login: "EnterpriseLogin | None"
) -> PublishedBusinessTool:
    artifact = {
        **record["artifact"],
        "profile": {"success": api.success, "singlePathSegments": True},
    }
    profile = record.get("authentication", {"mode": "legacy"})
    service = _login_key(record, api)
    auth = None
    static_headers = None
    token = None if profile["mode"] == "public" or service else api.token
    if profile["mode"] == "header" and token is not None:
        if (
            not token.isascii()
            or not token.isprintable()
            or token.strip() != token
        ):
            raise ValueError("The configured API Key has an invalid format.")
        static_headers = {profile["header"]: token}
        token = None
    if service:
        if login is None:
            raise ValueError("An active private login is required.")
        auth = login.auth(
            api.base_url, service, profile, adapter=api.login_adapter
        )
    tool = as_tool(
        artifact, api.base_url, token, auth=auth, static_headers=static_headers
    )
    return PublishedBusinessTool(
        tool.call,
        business=record["business"],
        display_name=record["label"],
        action="query",
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
        is_read_only=True,
    )


class EnterpriseSkillView(BaseModel):
    """One published business operation visible to the current Agent."""

    name: str
    display_name: str
    business: str
    action: Literal["query", "save", "readback"]
    description: str


async def enterprise_skill_views(
    tools: list[ToolBase], state: AgentState
) -> list[EnterpriseSkillView]:
    """Use the same live publications in chat and the skill panel."""
    skills = {}
    for tool in tools:
        if (
            isinstance(tool, PublishedBusinessTool)
            and tool.business
            and await tool.visible_in_catalog(state)
        ):
            skills[tool.name] = EnterpriseSkillView(
                name=tool.name,
                display_name=tool.display_name,
                business=tool.business,
                action=tool.action,
                description=tool.description,
            )
    return sorted(
        skills.values(), key=lambda s: (s.display_name.casefold(), s.name)
    )


def bindings(
    login: "EnterpriseLogin | None",
    user_id: str,
    agent_id: str,
    batch: dict,
    catalog: dict,
    *,
    load_tools: bool,
) -> dict:
    """Resolve current read grants and save publications."""
    tools = {}
    save_groups = defaultdict(list)
    for record in batch["records"]:
        revision = record.get("document_id", batch.get("document_id"))
        bindings = catalog.get(revision, {})
        if record.get("reason") == "authentication_endpoint":
            continue
        if record["method"] == "POST" and record.get("artifact"):
            registries = bindings.get("saves", [])
            if source := record.get("source_location"):
                path = Path(source)
                registries = [
                    r
                    for r in registries
                    if path.is_relative_to(r.source)
                    or r.source.is_relative_to(path)
                ]
            if len(registries) == 1 and registries[0].target_label:
                save_groups[registries[0]].append(record)
            else:
                record.update(
                    status="pending", reason="save_connection_required"
                )
        elif record["status"] == "registered":
            reads = bindings.get("reads", [])
            if (
                len(reads) == 1
                and reads[0].key == record.get("api_key")
                and record["route"] in reads[0].samples
            ):
                if (
                    record.get("authentication")
                    and _login_key(record, reads[0])
                    and login is None
                ):
                    record.update(
                        status="pending", reason="authentication_required"
                    )
                    continue
                if load_tools:
                    try:
                        tools[record["name"]] = _runtime(
                            record, reads[0], login
                        )
                    except LoginAdapterError as exc:
                        record.update(status="pending", reason=str(exc))
                    except ValueError:
                        record.update(
                            status="pending", reason="invalid_read_connection"
                        )
            else:
                record.update(status="pending", reason="verification_required")
    for registry, records in save_groups.items():
        routes = {r["route"] for r in records}
        published = registry.accepted_routes(user_id, agent_id)
        for record in records:
            if record["route"] in published:
                record.update(status="registered", reason=None)
            else:
                record.update(
                    status="pending",
                    reason="save_acceptance_required",
                    acceptance={
                        "registrar": registry.import_registrar_name,
                        "target": registry.target_label,
                    },
                )
        if load_tools:
            tools.update(
                (tool.name, tool)
                for tool in registry.tools(user_id, agent_id, routes=routes)
            )
    return tools
