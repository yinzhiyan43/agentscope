# -*- coding: utf-8 -*-
"""Convert API contracts or source evidence into Agent-scoped tools."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote

import httpx
import yaml
from jsonschema import SchemaError
from pydantic import BaseModel

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolBase
from agentscope.tool._business_search import PublishedBusinessTool

from ._business import (
    FORMAT,
    as_tool,
    bind_route,
    compile_source_plan,
    node,
    preview,
    resolve_path_items,
    write_definition,
)
from ._business import (
    definition_digest as _digest,
)
from ._enterprise_action import (
    ActionBlocked,
    ActionPlan,
    BoundRead,
    action_tool,
    action_view,
    verify_action,
)
from ._enterprise_auth import (
    LoginRecipe,
    compile_authentication,
    inspect_authentication,
)
from ._save_registry import SaveBusinessRegistry

if TYPE_CHECKING:
    from ..app._enterprise_login import EnterpriseLogin

MAX_INPUT = 8 * 1024 * 1024
_METHODS = {
    "get",
    "head",
    "options",
    "post",
    "put",
    "patch",
    "delete",
    "trace",
}
_ROUTE = re.compile(
    r"/(?:[\w.~-]+|\{[\w.-]+\})(?:/(?:[\w.~-]+|\{[\w.-]+\}))*/?|/"
)


class _OpenAPILoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise ValueError("OpenAPI YAML aliases are unsupported.")
        return super().compose_node(parent, index)


def _read(path: Path) -> str:
    if not path.is_file():
        raise ValueError("Expected an OpenAPI file.")
    with path.open("rb") as handle:
        raw = handle.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("The OpenAPI file exceeds 8 MiB.")
    return raw.decode("utf-8-sig")


def _document(source: str) -> dict:
    try:
        if source.lstrip().startswith(("{", "[")):
            doc = json.loads(source)
        else:
            doc = json.loads(
                json.dumps(yaml.load(source, Loader=_OpenAPILoader))
            )
    except (ValueError, TypeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("Could not parse the OpenAPI JSON or YAML.") from exc
    if (
        not isinstance(doc, dict)
        or not isinstance(doc.get("openapi"), str)
        or not doc["openapi"].startswith(("3.0.", "3.1."))
        or not isinstance(doc.get("paths"), dict)
    ):
        raise ValueError("Provide an OpenAPI 3.0 or 3.1 definition.")
    return doc


def _route_key(method: str, route: str) -> tuple[str, str]:
    return method.upper(), re.sub(r"\{[^/{}]+\}", "{}", route)


def _tool_name(method: str, route: str, operation_id: str, scope: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_]+", "_", operation_id).strip("_")
    if not label:
        label = re.sub(r"[^A-Za-z0-9_]+", "_", f"{method}_{route}").strip("_")
    return f"business_{label[:41]}_{_digest([scope, method, route])[:12]}"


def _api_artifact(
    doc: dict,
    route: str,
    method: str,
    name: str,
) -> dict:
    if not _ROUTE.fullmatch(route) or {".", ".."}.intersection(
        route.split("/")
    ):
        raise ValueError("Unsupported route template.")
    op = bind_route(doc, route, method, name)
    schema: dict = {"type": "object", "additionalProperties": False}
    properties: dict = {}
    required = []
    for location in ("path", "query"):
        params = [p for p in op["parameters"] if p["in"] == location]
        if params:
            names = [
                p["name"]
                for p in params
                if location == "path" or p.get("required")
            ]
            properties[location] = {
                "type": "object",
                "properties": {p["name"]: p["schema"] for p in params},
                "additionalProperties": False,
            }
            if names:
                required.append(location)
                properties[location]["required"] = names
    if op["bodySchema"] is not None:
        properties["body"] = op["bodySchema"]
        if op["bodyRequired"]:
            required.append("body")
    if required:
        schema["required"] = required
    schema["properties"] = properties
    return {
        "format": FORMAT,
        "operation": op,
        "plan": {
            "directOpenAPI": True,
            "endpoint": {"method": method.upper()},
        },
        "inputSchema": schema,
    }


def _operation_record(
    contract: dict,
    revision: str,
    origin: str,
    route: str,
    method: str,
    action: dict | None,
    batch: str,
    source: str | None,
) -> dict:
    """Build one runtime artifact from its trace or direct API definition."""
    raw = contract["paths"][route][method]
    raw = raw if isinstance(raw, dict) else {}
    op_id = raw.get("operationId") or ""
    entry = action["entry"] if action else ""
    name = _tool_name(
        method,
        route,
        entry.rsplit("#", 1)[-1] or str(op_id),
        f"{batch}:{entry}" if entry else revision,
    )
    pointer = route.replace("~", "~0").replace("/", "~1")
    tags = raw.get("tags")
    record = {
        "name": name,
        "label": str(raw.get("summary") or entry or op_id or route),
        "method": method.upper(),
        "route": route,
        "document_id": revision,
        "evidence": ([entry] if entry else [])
        + [f"{origin}#/paths/{pointer}/{method}"],
        "business": str(tags[0])
        if isinstance(tags, list) and tags
        else route.strip("/").split("/")[0] or "OpenAPI",
        "status": "pending",
        "reason": "verification_required"
        if method == "get"
        else "operation_confirmation_required",
    }
    try:
        artifact = _api_artifact(contract, route, method, name)
        if action:
            artifact = compile_source_plan(
                action["plan"], artifact["operation"]
            )
        record["artifact"] = artifact
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        SchemaError,
    ):
        record["reason"] = "unsupported_contract"
    if source:
        record["source_location"] = str(Path(source).expanduser().resolve())
    if (
        method not in ("get", "post")
        and record["reason"] == "operation_confirmation_required"
    ):
        record["reason"] = "unsupported_write"
    return record


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


def _brief_record(record: dict, *, include_evidence: bool) -> dict:
    row = {
        key: str(record[key])[:160]
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
            k: str(v)[:160] for k, v in record["acceptance"].items()
        }
    if include_evidence or "acceptance" in row:
        row["evidence"] = [
            str(item)[:200] for item in record.get("evidence", [])[:2]
        ]
    return row


def _chat_receipt(result: dict) -> dict:
    """Give the model counts and a bounded sample of the persisted result."""
    tools = result["tools"]
    registered = sum(tool["status"] == "registered" for tool in tools)
    sample = [
        _brief_record(tool, include_evidence=False) for tool in tools[:20]
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
        "diagnostics": [s[:160] for s in result["diagnostics"][:8]],
        "tools": sample,
        "omitted": len(tools) - len(sample),
    }


def _route_in_scope(route: str, scope: str) -> bool:
    if not scope:
        return True
    if not scope.startswith("/"):
        return False
    prefix = scope.rstrip("/")
    return route == prefix or route.startswith(prefix + "/")


def _operation_in_scope(route: str, operation: object, scope: str) -> bool:
    if _route_in_scope(route, scope):
        return True
    if not isinstance(operation, dict):
        return False
    labels = [operation.get("operationId"), operation.get("summary")]
    for tag in operation.get("tags") or []:
        if isinstance(tag, str):
            labels.extend([tag, *tag.split("·")])
    return any(
        isinstance(label, str) and label.strip().casefold() == scope.casefold()
        for label in labels
    )


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
        profile["mode"] == "bearer" and api.token and "login" not in profile
    ):
        return None
    return _digest([api.key, record["document_id"], profile])


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
    token = None if profile["mode"] == "public" or service else api.token
    if service:
        if login is None:
            raise ValueError("An active private login is required.")
        auth = login.auth(api.base_url, service, profile)
    tool = as_tool(artifact, api.base_url, token, auth=auth)
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


@dataclass(frozen=True)
class EnterpriseReadAPI:
    """A deployment-approved API and sample arguments for verified reads."""

    document: Path
    base_url: str
    samples: dict[str, dict]
    token: str | None = field(default=None, repr=False)
    success: dict | None = None

    @property
    def key(self) -> str:
        return _digest([str(self.document), self.base_url])


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
        if isinstance(tool, PublishedBusinessTool) and tool.business:
            if await tool.visible_in_catalog(state):
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


class EnterpriseBusiness:
    """Persist generated definitions separately for each user and Agent."""

    def __init__(
        self,
        directory: Path,
        *,
        local_user: str | None = None,
        save_registries: dict[str, list[SaveBusinessRegistry]] | None = None,
        source_roots: dict[str, list[Path]] | None = None,
        read_apis: dict[str, list[EnterpriseReadAPI]] | None = None,
        api_documents: dict[str, dict[str, Path]] | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.source_roots = {
            user: [root.expanduser().resolve() for root in roots]
            for user, roots in (source_roots or {}).items()
        }
        self.local_user = local_user
        self.save_registries = save_registries or {}
        for bindings in self.save_registries.values():
            if len({r.import_registrar_name for r in bindings}) != len(
                bindings
            ):
                raise ValueError(
                    "Each save target for a user needs a unique tool_prefix."
                )
        self.read_apis = read_apis or {}
        self.api_documents = api_documents or {}
        self.login: EnterpriseLogin | None = None

    def protects_user(self, user_id: str) -> bool:
        """Whether this account has a configured enterprise identity."""
        return (
            user_id == self.local_user
            or user_id in self.read_apis
            or user_id in self.save_registries
            or user_id in self.source_roots
            or user_id in self.api_documents
        )

    def chat_tools(
        self,
        user_id: str,
        agent_id: str,
        load_tools: Callable[[], Awaitable[list[ToolBase]]],
    ) -> list[ToolBase]:
        """Let the Agent use its enterprise capabilities in a normal turn."""

        async def register_enterprise_business(
            source: str,
            business_scope: str = "",
            authentication: LoginRecipe | None = None,
        ) -> dict:
            try:
                result = await self.convert(
                    user_id,
                    agent_id,
                    source,
                    business_scope=business_scope,
                    authentication=authentication,
                )
                return _chat_receipt(result)
            except (OSError, RuntimeError) as exc:
                raise ValueError(
                    "无法读取或保存该业务定义，请检查源码目录授权或接口定义。",
                ) from exc

        async def list_enterprise_skills(
            _agent_state: AgentState,
        ) -> list[dict]:
            skills = await enterprise_skill_views(
                await load_tools(), _agent_state
            )
            return [skill.model_dump() for skill in skills]

        async def inspect_enterprise_authentication(
            source: str, route: str = "", offset: int = 0
        ) -> dict:
            if not source.strip() or len(source.encode()) > MAX_INPUT:
                raise ValueError(
                    "Provide an OpenAPI definition or source path up to 8 MiB."
                )

            def inspect() -> dict:
                _, doc, _, _, _ = self._prepare(
                    user_id, source.strip(), contract_only=True
                )
                if doc is None:
                    return {"status": "source_trace_required"}
                return inspect_authentication(doc, route, offset)

            return await asyncio.to_thread(inspect)

        async def discover_enterprise_actions(
            source: str, business_scope: str = ""
        ) -> dict:
            return await self.discover(
                user_id, agent_id, source, business_scope
            )

        async def get_enterprise_progress(
            inventory_id: str = "",
            business_scope: str = "",
            status: Literal[
                "all", "discovered", "pending", "registered"
            ] = "all",
            offset: int = 0,
        ) -> dict:
            return await self.progress(
                user_id,
                agent_id,
                inventory_id=inventory_id,
                business_scope=business_scope,
                status=status,
                offset=offset,
            )

        async def register_enterprise_action(
            inventory_id: str,
            plan: ActionPlan,
            _agent_state: AgentState,
            sample_inputs: dict | None = None,
            authentication: LoginRecipe | None = None,
        ) -> dict:
            definition = ActionPlan.model_validate(plan)
            if not re.fullmatch(r"[0-9a-f]{64}", inventory_id):
                raise ValueError("Invalid inventory ID.")
            directory = self._directory(user_id, agent_id)
            filename = inventory_id + ".json"
            if not (
                (directory / "inventories" / filename).is_file()
                or (directory / filename).is_file()
            ):
                raise ValueError("No such inventory for this Agent.")
            key = _digest(
                [inventory_id, definition.business, definition.title]
            )
            draft = {
                "id": key,
                "inventory_id": inventory_id,
                "plan": definition.model_dump(),
                "sample": sample_inputs or {},
                "authentication": LoginRecipe.model_validate(
                    authentication
                ).model_dump()
                if authentication is not None
                else None,
            }
            return await self._verify_action(
                user_id, agent_id, key, _agent_state, draft
            )

        async def resume_enterprise_action(
            action_id: str, _agent_state: AgentState
        ) -> dict:
            return await self._verify_action(
                user_id, agent_id, action_id, _agent_state
            )

        async def get_enterprise_action_progress(
            action_id: str = "", offset: int = 0
        ) -> dict:
            return await asyncio.to_thread(
                self._action_progress, user_id, agent_id, action_id, offset
            )

        return [
            FunctionTool(
                register_enterprise_business,
                description=(
                    "按用户要求，从源码路径、OpenAPI 定义或已接入的 API 地址"
                    "为当前智能体注册企业业务功能。@企业业务 表示讨论的业务范围。"
                    "根据用户原话和对话上下文判断意图；咨询、解释或盘点可用功能时无需注册。"
                    "接入整个业务时先用 discover_enterprise_actions 列出候选动作，再按需注册。"
                    "若业务动作涉及多个查询，使用 register_enterprise_action 验收整条链。"
                    "source 只传本次所需的源码绝对路径、地址或定义原文。"
                    "用户指定某项业务时将其业务名或接口路径前缀传给 business_scope，"
                    "例如业务名直接用用户原话，仅在需要整个项目时留空。"
                    "先从源码追踪认证和字段来源，或调用 inspect_enterprise_authentication 查看契约。"
                    "需要登录换取凭据时，将登录 POST 路径、返回凭据的 JSON Pointer、"
                    "依赖 GET 的字段映射传入 authentication。缺少认证接口定义时请用户提供接口。"
                    "客户在私密表单填写认证参数；工具参数只传结构，不传凭据或固定业务数据。"
                    "根据字段来源组装依赖查询，普通业务调用也应优先通过关联的查询工具取得所需数据。"
                    "使用返回的 registered、pending 和原因向用户报告结果。"
                    "待处理的接口需要相应连接或验收；重复导入或编写文档不会使其注册成功。"
                ),
            ),
            FunctionTool(
                list_enterprise_skills,
                description="列出当前智能体在此会话中已注册并可见的企业业务技能。",
                input_schema={"type": "object", "properties": {}},
                is_state_injected=True,
                is_read_only=True,
                is_concurrency_safe=True,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="查看当前智能体可见的业务功能。",
                ),
            ),
            FunctionTool(
                inspect_enterprise_authentication,
                description="从指定源码或 API 契约查看认证方案及可在登录前调用的接口；"
                "传入 route 查看请求字段及响应字段路径，offset 翻页。根据这些关系组装认证依赖。"
                "若契约未说明关系，请读取对应源码或询问缺少的接口定义。",
                is_read_only=True,
            ),
            FunctionTool(
                discover_enterprise_actions,
                description="从用户提供的源码或 API 盘点指定业务的候选动作，"
                "只保存当前智能体的清单。不会登录、调用业务接口或发布技能。"
                "business_scope 可传业务名或路由前缀。按返回的依据区分源码动作和接口候选。",
            ),
            FunctionTool(
                get_enterprise_progress,
                description="从当前智能体已保存的清单和注册记录查看进度、依据和卡点。"
                "可传 inventory_id，或传 business_scope 查询该范围的最近一次清单。"
                "均省略时显示最近清单。用 status 筛选，按 next_offset 翻页。",
                is_read_only=True,
                permission=PermissionDecision(
                    behavior=PermissionBehavior.ALLOW,
                    message="查看当前智能体已保存的业务接入进度。",
                ),
            ),
            FunctionTool(
                register_enterprise_action,
                is_state_injected=True,
                description=(
                    "把同一清单中的多个 GET 串成用户能办理的一个动作，按已接入服务的验收样例逐步验证，"
                    "通过后发布为当前智能体的技能。根据源码或契约填写 plan，operation 取清单中的 name。"
                    "path/query 可以引用 input 或之前 step 的字段（JSON Pointer），也支持标量常量。"
                    "可用 inspect_enterprise_authentication 指定 route 查看接口字段和响应路径。"
                    "select 指向候选数组，match 按字段值核对唯一记录或最终业务结果。"
                    "分页列表有总条数字段时，用 total 指向该字段以校验候选集完整。"
                    "直接映射到已配置样例的业务参数会自动带入；若返回 missing_inputs，再用 sample_inputs 补充这些字段。"
                    "跨业务依赖可先盘点整个项目。写入请使用原有保存验收与确认流程。"
                ),
            ),
            FunctionTool(
                resume_enterprise_action,
                is_state_injected=True,
                description="按 action_id 从上次未通过的接口继续验证；保留仍有效的查询检查点。",
            ),
            FunctionTool(
                get_enterprise_action_progress,
                is_read_only=True,
                description="查看当前智能体组合动作的逐接口验收进度。省略 action_id 时分页列出动作和恢复标识。",
            ),
        ]

    @classmethod
    def from_environment(cls) -> "EnterpriseBusiness":
        """Load optional host-managed source and read endpoint grants."""
        config_path = os.environ.get("AGENTSCOPE_ENTERPRISE_BUSINESS_CONFIG")
        config = (
            json.loads(Path(config_path).read_text()) if config_path else {}
        )
        return cls(
            Path(config.get("directory", "~/.agentscope/enterprise-business")),
            local_user=config.get("local_user"),
            source_roots={
                user: [Path(root) for root in roots]
                for user, roots in config.get("source_roots", {}).items()
            },
            api_documents={
                user: {url: Path(path) for url, path in docs.items()}
                for user, docs in config.get("api_documents", {}).items()
            },
            read_apis={
                user: [
                    EnterpriseReadAPI(
                        Path(api["document"]),
                        api["base_url"],
                        api["samples"],
                        os.environ[api["token_env"]]
                        if api.get("token_env")
                        else None,
                        api.get("success"),
                    )
                    for api in apis
                ]
                for user, apis in config.get("read_apis", {}).items()
            },
        )

    def _directory(self, user_id: str, agent_id: str) -> Path:
        return self.directory / _digest([user_id, agent_id])

    def _source_path(self, user_id: str, value: str) -> Path:
        if len(value) > 4096:
            raise ValueError("The source path is too long.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "Provide an absolute path to the source or OpenAPI file."
            )
        path = path.resolve()
        if not any(
            path.is_relative_to(root)
            for root in self.source_roots.get(user_id, [])
        ):
            raise PermissionError(
                "This path is outside your authorized source directories."
            )
        if not path.is_dir() and not path.is_file():
            raise ValueError("The source or OpenAPI path does not exist.")
        return path

    @staticmethod
    def _embedded_document(root: Path) -> Path | None:
        """Find a bundled OpenAPI file in conventional project directories."""
        docs = root / "docs"
        folders = (root, docs, root / "openapi", docs / "openapi")
        if docs.is_dir() and docs.resolve().is_relative_to(root):
            folders += tuple(sorted(docs.glob("*/openapi")))
        for pattern in ("openapi.*", "full.openapi.*", "*.openapi.*"):
            for folder in folders:
                if not folder.is_dir() or not folder.resolve().is_relative_to(
                    root
                ):
                    continue
                for path in sorted(folder.glob(pattern)):
                    if (
                        path.suffix in (".json", ".yml", ".yaml")
                        and path.resolve().is_relative_to(root)
                        and path.is_file()
                    ):
                        return path
        return None

    def _prepare(
        self,
        user_id: str,
        value: str,
        *,
        contract_only: bool = False,
    ) -> tuple[str, dict | None, list[dict], list[str], str]:
        if value.startswith(("http://", "https://")):
            path = self.api_documents.get(user_id, {}).get(value)
            if path is None:
                raise ValueError(
                    "This API address has no configured OpenAPI document. "
                    "Paste or upload the definition."
                )
            return "api", _document(_read(path)), [], [], path.name
        if not value.startswith(("/", "~/")):
            return "api", _document(value), [], [], "OpenAPI"
        path = self._source_path(user_id, value)
        if path.is_file():
            if path.suffix not in (".json", ".yaml", ".yml"):
                # A source file can be passed directly; limit discovery to it.
                root = max(
                    (
                        r
                        for r in self.source_roots[user_id]
                        if r.is_dir() and path.is_relative_to(r)
                    ),
                    key=lambda r: len(r.parts),
                    default=None,
                )
                if root is None:
                    raise PermissionError(
                        "Source imports require an authorized directory."
                    )
                selected = str(path.relative_to(root))
            else:
                return "api", _document(_read(path)), [], [], path.name
        else:
            root, selected = path, None

        doc_path = (
            self._embedded_document(root)
            if selected is None or contract_only
            else None
        )
        doc = _document(_read(doc_path)) if doc_path else None
        if contract_only:
            return "source", doc, [], [], path.name
        diagnostics = []
        try:
            scan = node(
                "extract.mjs",
                str(root),
                "--discover",
                "",
                json.dumps(
                    {
                        "selectedFile": selected,
                        "discoveryLimit": 1800,
                        "includePlans": True,
                        "inferLocalHttpClients": True,
                    }
                ),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            scan = {"actions": [], "diagnostics": []}
            diagnostics.append("source_scan_unavailable")
        if scan.get("truncated"):
            diagnostics.append("source_scan_limited")
        if scan["diagnostics"]:
            diagnostics.append("some_source_actions_unsupported")
        label = (
            doc_path.relative_to(root).as_posix() if doc_path else path.name
        )
        return "source", doc, scan["actions"], diagnostics, label

    def _catalog(self, user_id: str) -> dict:
        """Index the authorized contracts once, including write targets."""
        paths = {a.document for a in self.read_apis.get(user_id, [])}
        paths.update(r.openapi for r in self.save_registries.get(user_id, []))
        catalog = {}
        for path in sorted(paths):
            try:
                doc = _document(_read(path))
                revision = _digest(doc)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
            entry = catalog.setdefault(
                revision,
                {
                    "document": doc,
                    "label": path.name,
                    "reads": [],
                    "saves": [],
                },
            )
            entry["reads"].extend(
                a
                for a in self.read_apis.get(user_id, [])
                if a.document == path
            )
            entry["saves"].extend(
                r
                for r in self.save_registries.get(user_id, [])
                if r.openapi == path
            )
        return catalog

    def _compile(
        self,
        user_id: str,
        value: str,
        scope: str = "",
        authentication: LoginRecipe | None = None,
    ) -> tuple:
        kind, doc, actions, diagnostics, label = self._prepare(user_id, value)
        catalog = self._catalog(user_id)
        revision = _digest(doc) if doc is not None else ""
        batch = _digest(value) if kind == "source" else revision
        groups = defaultdict(list)
        for action in actions:
            ep = action["endpoint"]
            groups[_route_key(ep["method"], ep["template"])].append(action)
        records = []
        contracts = (
            {revision: (doc, label)}
            if doc is not None
            else {
                key: (c["document"], c["label"]) for key, c in catalog.items()
            }
        )
        if kind == "source" and doc is None:
            location = Path(value).expanduser().resolve()
            for root in self.source_roots.get(user_id, []):
                if location.is_relative_to(root):
                    path = self._embedded_document(root)
                    if path is not None:
                        sibling_doc = _document(_read(path))
                        contracts.setdefault(
                            _digest(sibling_doc),
                            (sibling_doc, str(path.relative_to(root))),
                        )
        matches = defaultdict(list)
        for key, (raw, origin) in contracts.items():
            resolved = resolve_path_items(raw, diagnostics)
            for route, item in resolved["paths"].items():
                for method in _METHODS.intersection(item):
                    operation = _route_key(method, route)
                    if doc is not None or operation in groups:
                        if not _operation_in_scope(route, item[method], scope):
                            continue
                        matches[operation].append(
                            (key, resolved, origin, route, method)
                        )

        auth_profiles = {}
        for operation, options in matches.items():
            actions = groups.get(operation, [])
            if len(options) > 1 and actions:
                for action in actions:
                    action["reason"] = "ambiguous_contract"
                continue
            groups.pop(operation, None)
            helpers = {name for a in actions for name in a["calls"]}
            entries: list[dict | None] = [
                a for a in actions if a["entry"] not in helpers
            ]
            for key, contract, origin, route, method in options:
                for action in entries or [None]:
                    if len(records) >= 1500:
                        raise ValueError(
                            "Limit each import to 1,500 business operations."
                        )
                    record = _operation_record(
                        contract,
                        key,
                        origin,
                        route,
                        method,
                        action,
                        batch,
                        value if kind == "source" else None,
                    )
                    if method == "get":
                        raw = contract["paths"][route][method]
                        requirements = (
                            raw.get("security", contract.get("security"))
                            if isinstance(raw, dict)
                            else None
                        )
                        policy = (key, _digest(requirements))
                        try:
                            if policy not in auth_profiles:
                                auth_profiles[policy] = compile_authentication(
                                    contract, raw, authentication
                                )
                            record["authentication"] = auth_profiles[policy]
                        except (KeyError, TypeError, ValueError, SchemaError):
                            record["reason"] = (
                                "unsupported_authentication_contract"
                            )
                    elif (
                        authentication
                        and method == "post"
                        and route == authentication.route
                    ):
                        record["reason"] = "authentication_endpoint"
                    records.append(record)
        for candidates in groups.values():
            for action in candidates:
                entry = action["entry"]
                ep = action["endpoint"]
                method, route = ep["method"], ep["template"]
                if not _route_in_scope(
                    route, scope
                ) and scope.casefold() not in (
                    entry.casefold(),
                    entry.rsplit("#", 1)[-1].casefold(),
                ):
                    continue
                records.append(
                    {
                        "name": _tool_name(
                            method,
                            route,
                            entry.rsplit("#", 1)[-1],
                            f"{batch}:{entry}",
                        ),
                        "label": entry.rsplit("#", 1)[-1],
                        "method": method,
                        "route": route,
                        "evidence": [entry],
                        "status": "pending",
                        "reason": action.get("reason", "contract_required"),
                        "source_plan": action["plan"],
                    }
                )
        records.sort(key=lambda r: (r["route"], r["method"], r["name"]))
        if scope:
            batch = _digest([batch, scope])
            if not records:
                diagnostics.append("business_scope_not_found")
        return kind, batch, records, diagnostics, catalog

    def _save_inventory(
        self,
        user_id: str,
        agent_id: str,
        batch: str,
        kind: str,
        scope: str,
        records: list[dict],
        diagnostics: list[str],
    ) -> None:
        directory = self._directory(user_id, agent_id) / "inventories"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_definition(
            directory / f"{batch}.json",
            {
                "kind": kind,
                "business_scope": scope,
                "diagnostics": diagnostics,
                "candidates": [_candidate(r) for r in records],
                # Discovery alone is sufficient to compose an action. Keep the
                # executable read definitions private to the owning Agent.
                "reads": {
                    r["name"]: _inventory_read(r)
                    for r in records
                    if r["method"] == "GET" and "artifact" in r
                },
            },
        )

    def _action_path(
        self, user_id: str, agent_id: str, action_id: str
    ) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", action_id):
            raise ValueError("Invalid action ID.")
        return (
            self._directory(user_id, agent_id)
            / "actions"
            / f"{action_id}.json"
        )

    def _bind_action(
        self,
        user_id: str,
        agent_id: str,
        action: dict,
        plan: ActionPlan,
        catalog: dict | None = None,
    ) -> list[BoundRead]:
        directory = self._directory(user_id, agent_id)
        filename = action["inventory_id"] + ".json"
        inventory = directory / "inventories" / filename
        snapshot = (
            json.loads(inventory.read_bytes()) if inventory.is_file() else {}
        )
        records = snapshot.get("reads")
        if records is None:
            receipt = directory / filename
            if not receipt.is_file():
                raise ActionBlocked("refresh_inventory_required")
            # Only reuse an old receipt if its executable plan matches the
            # selected discovery snapshot.
            candidates = {
                c["name"]: c["revision"]
                for c in snapshot.get("candidates", [])
            }
            saved = json.loads(receipt.read_bytes())
            records = {}
            for record in saved["records"]:
                record.setdefault("document_id", saved.get("document_id"))
                if (
                    record["method"] == "GET"
                    and record.get("artifact")
                    and (
                        not snapshot
                        or candidates.get(record["name"])
                        == _record_revision(record)
                    )
                ):
                    records[record["name"]] = record
        catalog = self._catalog(user_id) if catalog is None else catalog
        reads = []
        documents = {}
        target = None
        recipe = (
            LoginRecipe.model_validate(action["authentication"])
            if action.get("authentication")
            else None
        )
        for step in plan.steps:
            record = records.get(step.operation)
            if record is None:
                raise ActionBlocked("documented_read_required", step.key)
            entry = catalog.get(record["document_id"], {})
            apis = entry.get("reads", [])
            if len(apis) != 1:
                raise ActionBlocked("read_connection_required", step.key)
            api = apis[0]
            route = record["route"]
            if route not in api.samples:
                raise ActionBlocked("verification_sample_required", step.key)
            if target is not None and target != api.key:
                raise ActionBlocked("different_api_target", step.key)
            target = api.key
            revision = record["document_id"]
            try:
                if revision not in documents:
                    documents[revision] = resolve_path_items(
                        entry["document"], []
                    )
                doc = documents[revision]
                artifact = record.get("artifact")
                if artifact is None:
                    artifact = _api_artifact(doc, route, "get", record["name"])
                    if "client_plan" in record:
                        artifact = compile_source_plan(
                            record["client_plan"], artifact["operation"]
                        )
                    record = {**record, "artifact": artifact}
            except (KeyError, TypeError, ValueError, SchemaError) as exc:
                raise ActionBlocked("unsupported_contract", step.key) from exc
            if artifact["plan"]["endpoint"]["method"] != "GET":
                raise ActionBlocked("documented_read_required", step.key)
            if src := record.get("source_location"):
                try:
                    root = self._source_path(user_id, src)
                    if root.is_file():
                        root = max(
                            (
                                r
                                for r in self.source_roots[user_id]
                                if r.is_dir() and root.is_relative_to(r)
                            ),
                            key=lambda r: len(r.parts),
                        )
                    for rel, expected in (
                        artifact["plan"].get("hashes", {}).items()
                    ):
                        location = (root / rel).resolve()
                        if (
                            not location.is_relative_to(root)
                            or hashlib.sha256(
                                location.read_bytes()
                            ).hexdigest()
                            != expected
                        ):
                            raise ActionBlocked("source_changed", step.key)
                except (PermissionError, OSError, ValueError) as exc:
                    raise ActionBlocked("source_changed", step.key) from exc
            if recipe or "authentication" not in record:
                try:
                    profile = compile_authentication(
                        doc, doc["paths"][route]["get"], recipe
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ActionBlocked(
                        "unsupported_authentication_contract", step.key
                    ) from exc
                record = {**record, "authentication": profile}
            if _login_key(record, api) and self.login is None:
                raise ActionBlocked("authentication_required", step.key)
            sample = api.samples[route]
            revision = _digest(
                [
                    _record_revision(record),
                    record["name"],
                    api.key,
                    api.success,
                    record["authentication"],
                    sample,
                    _digest(api.token),
                ]
            )

            def arguments(
                wire: dict, verifying: bool, artifact=artifact, sample=sample
            ) -> dict:
                args = _sample_inputs(artifact, wire)
                if verifying:
                    approved = _sample_inputs(artifact, sample)
                    if _digest(preview(artifact, args)) != _digest(
                        preview(artifact, approved)
                    ):
                        raise ActionBlocked("approved_sample_mismatch")
                return args

            scope = ""
            if _login_key(record, api):
                from ..app.tool_login import LoginEnded

                try:
                    scope = _digest(self.login.broker.current_scope())
                except LoginEnded:
                    pass  # Catalog inspection runs outside a chat.
            try:
                tool = _runtime(record, api, self.login)
            except ValueError as exc:
                raise ActionBlocked(
                    "invalid_read_connection", step.key
                ) from exc
            reads.append(
                BoundRead(tool, route, revision, scope, sample, arguments)
            )
        return reads

    async def _verify_action(
        self,
        user_id: str,
        agent_id: str,
        key: str,
        state: AgentState,
        draft: dict | None = None,
    ) -> dict:
        path = self._action_path(user_id, agent_id, key)

        def bind(record: dict, plan: ActionPlan) -> list[BoundRead]:
            return self._bind_action(user_id, agent_id, record, plan)

        return await verify_action(path, bind, state, draft=draft)

    def _action_progress(
        self,
        user_id: str,
        agent_id: str,
        action_id: str,
        offset: int,
        inventory_id: str = "",
        catalog: dict | None = None,
    ) -> dict:
        if offset < 0:
            raise ValueError("Expected a nonnegative offset.")
        directory = self._directory(user_id, agent_id) / "actions"
        if action_id:
            path = self._action_path(user_id, agent_id, action_id)
            if not path.is_file():
                raise ValueError("No such action for this Agent.")
            paths = [path]
        else:
            paths = sorted(
                directory.glob("*.json"),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
        records = [json.loads(path.read_bytes()) for path in paths]
        if inventory_id:
            records = [r for r in records if r["inventory_id"] == inventory_id]
        catalog = (
            self._catalog(user_id) if catalog is None and records else catalog
        )
        items = []
        for record in records[offset : offset + 20]:
            blocked = None
            if record["passed"] and record["revisions"]:
                try:
                    bound = self._bind_action(
                        user_id,
                        agent_id,
                        record,
                        ActionPlan.model_validate(record["plan"]),
                        catalog,
                    )
                    for i, read in enumerate(bound[: len(record["passed"])]):
                        if record["revisions"][i] != read.revision:
                            raise ActionBlocked(
                                "action_requires_verification",
                                record["plan"]["steps"][i]["key"],
                            )
                except ActionBlocked as exc:
                    blocked = exc
            view = action_view(record, blocked)
            if not action_id:
                del view["steps"]
            items.append(view)
        return {
            "actions": items,
            "total": len(records),
            "next_offset": offset + len(items)
            if offset + len(items) < len(records)
            else None,
        }

    async def discover(
        self,
        user_id: str,
        agent_id: str,
        source: str,
        business_scope: str = "",
    ) -> dict:
        """Compile an inventory without authenticating, probing or publishing."""
        source, scope = source.strip(), business_scope.strip()
        if not source or len(source.encode()) > MAX_INPUT:
            raise ValueError(
                "Provide an OpenAPI definition or source path up to 8 MiB."
            )

        def scan() -> str:
            kind, batch, records, diagnostics, _ = self._compile(
                user_id, source, scope
            )
            self._save_inventory(
                user_id, agent_id, batch, kind, scope, records, diagnostics
            )
            return batch

        batch = await asyncio.to_thread(scan)
        return await self.progress(user_id, agent_id, inventory_id=batch)

    async def progress(
        self,
        user_id: str,
        agent_id: str,
        *,
        inventory_id: str = "",
        business_scope: str = "",
        status: str = "all",
        offset: int = 0,
    ) -> dict:
        """Read a bounded page of the inventory and reconcile current registrations."""
        if inventory_id and not re.fullmatch(r"[0-9a-f]{64}", inventory_id):
            raise ValueError("Invalid inventory ID.")
        if offset < 0 or status not in {
            "all",
            "discovered",
            "pending",
            "registered",
        }:
            raise ValueError("Invalid progress filter.")
        return await asyncio.to_thread(
            self._progress,
            user_id,
            agent_id,
            inventory_id,
            business_scope.strip(),
            status,
            offset,
        )

    @staticmethod
    def _find_inventory(
        directory: Path,
        imports: list[Path],
        inventory_id: str,
        scope: str,
    ) -> tuple[str, dict, list[dict]] | None:
        # Older installations have import receipts but no discovery snapshots.
        snapshots = {p.stem: p for p in imports}
        snapshots.update(
            (p.stem, p) for p in (directory / "inventories").glob("*.json")
        )
        if inventory_id:
            paths = (
                [snapshots[inventory_id]] if inventory_id in snapshots else []
            )
        else:
            paths = sorted(
                snapshots.values(),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )
        best = None
        best_rank = -1
        for path in paths:
            data = json.loads(path.read_bytes())
            metadata = data.get("result", data)
            if "candidates" in data:
                candidates = data["candidates"]
            else:
                for record in data["records"]:
                    record.setdefault("document_id", data.get("document_id"))
                candidates = [_candidate(r) for r in data["records"]]
            saved_scope = metadata.get("business_scope", "")
            if not scope or scope.casefold() == saved_scope.casefold():
                return path.stem, metadata, candidates
            # Prefer a full-project inventory over an incidental tag match in
            # a narrower import. An exact scope wins even if it is older.
            rank = 0 if saved_scope else 1
            if rank <= best_rank:
                continue
            matched = [
                r
                for r in candidates
                if _operation_in_scope(
                    r["route"],
                    {"summary": r["label"], "tags": [r.get("business", "")]},
                    scope,
                )
            ]
            if matched:
                best = path.stem, metadata, matched
                best_rank = rank
        return best

    def _progress(
        self,
        user_id: str,
        agent_id: str,
        inventory_id: str,
        scope: str,
        status: str,
        offset: int,
    ) -> dict:
        directory = self._directory(user_id, agent_id)
        imports = sorted(
            directory.glob("*.json"), key=lambda p: p.stat().st_mtime_ns
        )
        inventory = self._find_inventory(
            directory, imports, inventory_id, scope
        )
        if inventory is None:
            return {
                "inventory_id": None,
                "total": 0,
                "counts": {},
                "matched": 0,
                "actions": [],
                "next_offset": None,
            }
        batch, metadata, candidates = inventory
        rows = {
            (r["name"], r["revision"]): {**r, "status": "discovered"}
            for r in candidates
        }
        names = {key[0] for key in rows}
        catalog = self._catalog(user_id)
        for receipt in imports:
            saved = json.loads(receipt.read_bytes())
            matched = []
            for r in saved["records"]:
                if r["name"] not in names:
                    continue
                r.setdefault("document_id", saved.get("document_id"))
                key = (r["name"], _record_revision(r))
                if key in rows:
                    matched.append((key, r))
            self._bindings(
                user_id,
                agent_id,
                {"records": [r for _, r in matched]},
                catalog,
                load_tools=False,
            )
            for key, record in matched:
                row = rows[key]
                if (
                    row["status"] == "registered"
                    and record["status"] != "registered"
                ):
                    continue
                row.update(
                    status=record["status"], reason=record.get("reason")
                )
                if row["reason"] == "verification_required":
                    apis = catalog.get(record["document_id"], {}).get(
                        "reads", []
                    )
                    if not apis:
                        row["reason"] = "read_connection_required"
                    elif (
                        len(apis) == 1
                        and record["route"] not in apis[0].samples
                    ):
                        row["reason"] = "verification_sample_required"
                for detail in ("acceptance", "authentication_issue"):
                    row.pop(detail, None)
                    if record.get(detail):
                        row[detail] = record[detail]

        filtered = [
            r
            for r in rows.values()
            if status == "all" or r["status"] == status
        ]
        page = [
            _brief_record(row, include_evidence=True)
            for row in filtered[offset : offset + 20]
        ]
        return {
            "inventory_id": batch,
            "kind": metadata["kind"],
            "business_scope": scope or metadata.get("business_scope", ""),
            "total": len(rows),
            "counts": dict(Counter(r["status"] for r in rows.values())),
            "diagnostics": [
                str(s)[:160] for s in metadata.get("diagnostics", [])[:8]
            ],
            "matched": len(filtered),
            "actions": page,
            "composed_actions": self._action_progress(
                user_id, agent_id, "", 0, batch, catalog
            ),
            "next_offset": offset + len(page)
            if offset + len(page) < len(filtered)
            else None,
        }

    async def convert(
        self,
        user_id: str,
        agent_id: str,
        value: str,
        *,
        business_scope: str = "",
        authentication: LoginRecipe | dict | None = None,
    ) -> dict:
        """Compile, then probe and publish the same artifact."""
        value = value.strip()
        if not value or len(value.encode()) > MAX_INPUT:
            raise ValueError(
                "Provide an OpenAPI definition or source path up to 8 MiB."
            )
        recipe = (
            LoginRecipe.model_validate(authentication)
            if authentication is not None
            else None
        )
        kind, batch, records, diagnostics, catalog = await asyncio.to_thread(
            self._compile, user_id, value, business_scope.strip(), recipe
        )
        await asyncio.to_thread(
            self._save_inventory,
            user_id,
            agent_id,
            batch,
            kind,
            business_scope.strip(),
            records,
            diagnostics,
        )
        semaphore = asyncio.Semaphore(4)

        from ..app.tool_login import LoginEnded, LoginFailed

        login_failures = {}

        async def verify(record: dict) -> None:
            reads = catalog.get(record.get("document_id"), {}).get("reads", [])
            if len(reads) > 1:
                record["reason"] = "ambiguous_contract"
                return
            if not reads or record["route"] not in reads[0].samples:
                return
            api = reads[0]
            group = _login_key(record, api)
            async with semaphore:
                if group in login_failures:
                    record.update(login_failures[group])
                    return
                try:
                    if group:
                        if self.login is None:
                            raise LoginEnded()
                        self.login.broker.current_scope()
                    sample = await asyncio.to_thread(
                        _sample_inputs,
                        record["artifact"],
                        api.samples[record["route"]],
                    )
                    await _runtime(record, api, self.login).call(**sample)
                except (LoginEnded, LoginFailed) as exc:
                    record["reason"] = (
                        "authentication_required"
                        if isinstance(exc, LoginEnded)
                        else "authentication_failed"
                    )
                    record["authentication_issue"] = str(exc)[:160]
                    login_failures[group] = {
                        k: record[k]
                        for k in ("reason", "authentication_issue")
                    }
                except (
                    httpx.HTTPError,
                    ValueError,
                    TypeError,
                    KeyError,
                    subprocess.TimeoutExpired,
                    OSError,
                ):
                    record["reason"] = "verification_failed"
                else:
                    record.update(
                        status="registered", reason=None, api_key=api.key
                    )

        await asyncio.gather(
            *(
                verify(r)
                for r in records
                if r["reason"] == "verification_required"
            )
        )
        directory = self._directory(user_id, agent_id)

        def save() -> dict:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = {
                "kind": kind,
                "inventory_id": batch,
                "business_scope": business_scope.strip(),
                "records": records,
                "diagnostics": diagnostics,
            }
            write_definition(directory / f"{batch}.json", payload)
            return self._view(user_id, agent_id, payload, catalog)

        return await asyncio.to_thread(save)

    def _bindings(
        self,
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
                    and not record.get("source_actions")
                ):
                    if (
                        record.get("authentication")
                        and _login_key(record, reads[0])
                        and self.login is None
                    ):
                        record.update(
                            status="pending", reason="authentication_required"
                        )
                        continue
                    if load_tools:
                        tools[record["name"]] = _runtime(
                            record, reads[0], self.login
                        )
                else:
                    record.update(
                        status="pending", reason="verification_required"
                    )
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
                    for tool in registry.tools(
                        user_id, agent_id, routes=routes
                    )
                )
        return tools

    def _view(
        self, user_id: str, agent_id: str, batch: dict, catalog: dict
    ) -> dict:
        self._bindings(user_id, agent_id, batch, catalog, load_tools=False)
        metadata = batch.get("result", batch)
        return {
            "inventory_id": metadata.get("inventory_id"),
            "kind": metadata["kind"],
            "diagnostics": metadata["diagnostics"],
            "tools": _public_records(batch["records"]),
        }

    async def latest(self, user_id: str, agent_id: str) -> dict | None:
        def load() -> dict | None:
            files = self._directory(user_id, agent_id).glob("*.json")
            newest = max(
                files, key=lambda p: p.stat().st_mtime_ns, default=None
            )
            if newest is None:
                return None
            payload = json.loads(newest.read_bytes())
            payload.get("result", payload).setdefault(
                "inventory_id", newest.stem
            )
            return self._view(
                user_id, agent_id, payload, self._catalog(user_id)
            )

        return await asyncio.to_thread(load)

    async def tools(self, user_id: str, agent_id: str) -> list[ToolBase]:
        if not (
            self.read_apis.get(user_id) or self.save_registries.get(user_id)
        ):
            return []

        def load() -> list[ToolBase]:
            catalog = self._catalog(user_id)
            # Assemble all imports together so a registrar sees the union of
            # pending routes even when they originated in different batches.
            records = []
            for path in sorted(
                self._directory(user_id, agent_id).glob("*.json")
            ):
                batch = json.loads(path.read_bytes())
                for record in batch["records"]:
                    record.setdefault("document_id", batch.get("document_id"))
                    records.append(record)
            tools = self._bindings(
                user_id,
                agent_id,
                {"records": records},
                catalog,
                load_tools=True,
            )

            def bind(action: dict, plan: ActionPlan) -> list[BoundRead]:
                return self._bind_action(user_id, agent_id, action, plan)

            for path in (self._directory(user_id, agent_id) / "actions").glob(
                "*.json"
            ):
                action = json.loads(path.read_bytes())
                if action["status"] != "registered":
                    continue
                plan = ActionPlan.model_validate(action["plan"])
                try:
                    reads = self._bind_action(
                        user_id, agent_id, action, plan, catalog
                    )
                except ActionBlocked:
                    continue
                if action["revisions"] == [read.revision for read in reads]:
                    tool = action_tool(action, bind, path, reads)
                    tools[tool.name] = tool
            return list(tools.values())

        return await asyncio.to_thread(load)
