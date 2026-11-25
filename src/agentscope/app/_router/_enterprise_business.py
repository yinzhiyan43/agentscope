# -*- coding: utf-8 -*-
"""Agent-owned enterprise business imports from the chat composer."""

from fastapi import APIRouter, Depends, HTTPException, Request
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

enterprise_business_router = APIRouter(
    prefix="/agent/{agent_id}/enterprise-business",
    tags=["enterprise-business"],
)


class EnterpriseBusinessInput(BaseModel):
    """One source path, API definition, or configured API address."""

    source: str = Field(min_length=1, max_length=MAX_INPUT)
    business_scope: str = ""
    authentication: LoginRecipe | None = None


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
