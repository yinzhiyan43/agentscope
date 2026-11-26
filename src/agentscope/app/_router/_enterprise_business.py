# -*- coding: utf-8 -*-
"""Agent-owned enterprise business imports from the chat composer."""

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...business._enterprise import (
    EnterpriseBusiness,
    EnterpriseSkillView,
    enterprise_skill_views,
)
from ...business._enterprise_auth import LoginRecipe
from ...business._enterprise_config import MAX_INPUT
from ...state import AgentState
from .._types import AgentToolFactory
from ..deps import get_current_user_id, get_extra_agent_tools, get_storage
from ..storage import StorageBase
from ..tool_login import tool_login_scope

enterprise_business_router = APIRouter(
    prefix="/agent/{agent_id}/enterprise-business",
    tags=["enterprise-business"],
)


class EnterpriseBusinessInput(BaseModel):
    """One source path, API definition, or configured API address."""

    source: str = Field(min_length=1, max_length=MAX_INPUT)
    business_scope: str = ""
    authentication: LoginRecipe | None = None


class EnterpriseActionRow(BaseModel):
    """The panel summary of an enterprise action."""

    action_id: str
    title: str
    status: Literal["pending", "registered"]
    completed: int
    total: int
    can_resume: bool


class EnterpriseActionPage(BaseModel):
    """One page of visible action summaries."""

    actions: list[EnterpriseActionRow]
    next_offset: int | None


async def _owner(
    agent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> str:
    if await storage.get_agent(user_id, agent_id) is None:
        raise HTTPException(404, "Agent not found.")
    return user_id


def _converter(request: Request) -> EnterpriseBusiness:
    return request.app.state.enterprise_business


async def _action_session_state(
    storage: StorageBase, user_id: str, agent_id: str, session_id: str
) -> AgentState:
    session = await storage.get_session(user_id, agent_id, session_id)
    if session is None or session.agent_id != agent_id:
        raise HTTPException(404, "Session not found.")
    return session.state


def _can_resume(state: AgentState) -> bool:
    return not any(
        not rule.rule_content
        for rule in state.permission_context.deny_rules.get(
            "resume_enterprise_action", []
        )
    )


@enterprise_business_router.get("/actions")
async def list_enterprise_actions(
    agent_id: str,
    session_id: str | None = None,
    offset: int = Query(0, ge=0),
    user_id: str = Depends(_owner),
    storage: StorageBase = Depends(get_storage),
    converter: EnterpriseBusiness = Depends(_converter),
) -> EnterpriseActionPage:
    """List visible actions without invoking any of their business reads."""
    state = AgentState()
    if session_id is not None:
        state = await _action_session_state(
            storage, user_id, agent_id, session_id
        )
    page = await asyncio.to_thread(
        converter._action_progress, user_id, agent_id, "", offset, state=state
    )
    can_resume = session_id is not None and _can_resume(state)
    for action in page["actions"]:
        action["can_resume"] = can_resume and action["status"] == "pending"
    return EnterpriseActionPage.model_validate(page)


@enterprise_business_router.post("/actions/{action_id}/resume")
async def resume_enterprise_action(
    agent_id: str,
    action_id: str,
    session_id: str,
    user_id: str = Depends(_owner),
    storage: StorageBase = Depends(get_storage),
    converter: EnterpriseBusiness = Depends(_converter),
) -> EnterpriseActionRow:
    """Continue validation in the same private login scope as chat."""
    state = await _action_session_state(storage, user_id, agent_id, session_id)
    try:
        page = await asyncio.to_thread(
            converter._action_progress,
            user_id,
            agent_id,
            action_id,
            0,
            state=state,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Action not found.") from exc
    if not page["actions"]:
        raise HTTPException(404, "Action not found.")
    action = page["actions"][0]
    if action["status"] == "pending":
        if not _can_resume(state):
            raise HTTPException(403, "Action resumption is denied.")
        with tool_login_scope(user_id, agent_id, session_id):
            action = await converter._verify_action(
                user_id, agent_id, action_id, state
            )
    action["can_resume"] = action["status"] == "pending"
    return EnterpriseActionRow.model_validate(action)


@enterprise_business_router.get("")
async def latest_enterprise_business(
    agent_id: str,
    user_id: str = Depends(_owner),
    converter: EnterpriseBusiness = Depends(_converter),
) -> dict | None:
    """Read the result of the latest enterprise business import."""
    return await converter.latest(user_id, agent_id)


@enterprise_business_router.post("")
async def convert_enterprise_business(
    agent_id: str,
    body: EnterpriseBusinessInput,
    user_id: str = Depends(_owner),
    converter: EnterpriseBusiness = Depends(_converter),
) -> dict:
    """Convert one input for the selected Agent."""
    try:
        return await converter.convert(
            user_id,
            agent_id,
            body.source,
            business_scope=body.business_scope,
            authentication=body.authentication,
        )
    except PermissionError as exc:
        # A filesystem PermissionError may contain private host paths.
        raise HTTPException(403, "Source path access is unavailable.") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            422, "Could not read or store the business definition."
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@enterprise_business_router.get("/skills")
async def list_enterprise_skills(
    agent_id: str,
    session_id: str | None = None,
    user_id: str = Depends(_owner),
    storage: StorageBase = Depends(get_storage),
    converter: EnterpriseBusiness = Depends(_converter),
    factory: AgentToolFactory | None = Depends(get_extra_agent_tools),
) -> list[EnterpriseSkillView]:
    """List active business functions across imports without invoking them."""
    state = AgentState()
    if session_id is not None:
        session = await storage.get_session(user_id, agent_id, session_id)
        if session is None:
            raise HTTPException(404, "Session not found.")
        state = session.state
    # A host factory may grant tools only in a specific session. Use the
    # same assembled factory as chat only after checking that session.
    if session_id is not None and factory is not None:
        tools = await factory(user_id, agent_id, session_id)
    else:
        tools = await converter.tools(user_id, agent_id)

    return await enterprise_skill_views(tools, state)
