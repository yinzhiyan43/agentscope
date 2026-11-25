# -*- coding: utf-8 -*-
"""Expose enterprise onboarding and recovery in the normal chat toolkit."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.state import AgentState
from agentscope.tool import FunctionTool, ToolBase

from ._business import definition_digest as _digest
from ._enterprise_action import ActionPlan
from ._enterprise_auth import LoginRecipe
from ._enterprise_catalog import _chat_receipt, enterprise_skill_views

if TYPE_CHECKING:
    from ._enterprise import EnterpriseBusiness


def chat_tools(
    business: "EnterpriseBusiness",
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
            result = await business.convert(
                user_id,
                agent_id,
                source,
                business_scope=business_scope,
                authentication=authentication,
            )
            return _chat_receipt(result, business.limits)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                "无法读取或保存该业务定义，请检查源码目录授权或接口定义。",
            ) from exc

    async def list_enterprise_skills(
        _agent_state: AgentState,
    ) -> list[dict]:
        skills = await enterprise_skill_views(await load_tools(), _agent_state)
        return [skill.model_dump() for skill in skills]

    async def inspect_enterprise_authentication(
        source: str, route: str = "", offset: int = 0
    ) -> dict:
        return await asyncio.to_thread(
            business.compiler.inspect_authentication,
            user_id,
            source.strip(),
            route,
            offset,
        )

    async def discover_enterprise_actions(
        source: str, business_scope: str = ""
    ) -> dict:
        return await business.discover(
            user_id, agent_id, source, business_scope
        )

    async def get_enterprise_progress(
        inventory_id: str = "",
        business_scope: str = "",
        status: Literal["all", "discovered", "pending", "registered"] = "all",
        offset: int = 0,
    ) -> dict:
        return await business.progress(
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
        directory = business._directory(user_id, agent_id)
        filename = inventory_id + ".json"
        if not (
            (directory / "inventories" / filename).is_file()
            or (directory / filename).is_file()
        ):
            raise ValueError("No such inventory for this Agent.")
        key = _digest([inventory_id, definition.business, definition.title])
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
        return await business._verify_action(
            user_id, agent_id, key, _agent_state, draft
        )

    async def resume_enterprise_action(
        action_id: str, _agent_state: AgentState
    ) -> dict:
        return await business._verify_action(
            user_id, agent_id, action_id, _agent_state
        )

    async def get_enterprise_action_progress(
        action_id: str = "", offset: int = 0
    ) -> dict:
        return await asyncio.to_thread(
            business._action_progress, user_id, agent_id, action_id, offset
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
