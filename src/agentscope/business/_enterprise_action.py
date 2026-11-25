# -*- coding: utf-8 -*-
"""Compose approved reads into a business action with resumable verification."""

import asyncio
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentscope.permission import PermissionBehavior, PermissionEngine
from agentscope.state import AgentState
from agentscope.tool._business_search import PublishedBusinessTool

from ._business import definition_digest, validate, write_definition
from ._enterprise_config import PUBLICATION_POLL_SECONDS
from ._enterprise_diagnostics import report_failure
from ._save_registry import _publication_lock

Scalar = str | int | float | bool
Kind = Literal["string", "integer", "number", "boolean"]


class FieldRef(BaseModel):
    """A JSON Pointer into the business input or an earlier step's selected row."""

    model_config = ConfigDict(extra="forbid")
    source: str
    pointer: str = Field(pattern=r"^(?:/|$)")


class ActionStep(BaseModel):
    """A GET candidate from the inventory, with its path and query bindings."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    operation: str = Field(
        description="Exact operation tool name from the inventory."
    )
    path: dict[str, FieldRef | Scalar] = Field(default_factory=dict)
    query: dict[str, FieldRef | Scalar] = Field(default_factory=dict)
    select: str | None = Field(
        default=None,
        pattern=r"^(?:/|$)",
        description="Pointer to a list. Exactly one matching row must remain.",
    )
    total: str | None = Field(
        default=None,
        pattern=r"^(?:/|$)",
        description="For a paginated selection, pointer to the total record count in the response.",
    )
    match: dict[str, FieldRef | Scalar] = Field(
        default_factory=dict,
        description="Expected fields by JSON Pointer on the row or final response.",
    )

    def refs(self) -> list[FieldRef]:
        return [
            ref
            for bindings in (self.path, self.query, self.match)
            for ref in bindings.values()
            if isinstance(ref, FieldRef)
        ]


class ActionPlan(BaseModel):
    """The Agent derives one business action from the source or API contract."""

    model_config = ConfigDict(extra="forbid")
    business: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=160)
    inputs: dict[str, Kind]
    steps: list[ActionStep] = Field(min_length=2)

    @model_validator(mode="after")
    def check_dependencies(self) -> "ActionPlan":
        if "_agent_state" in self.inputs:
            raise ValueError(
                "The Agent state is supplied by the chat runtime."
            )
        input_pointers = {
            "/" + name.replace("~", "~0").replace("/", "~1")
            for name in self.inputs
        }
        known = {"input"}
        used = set()
        for step in self.steps:
            if step.key in known:
                raise ValueError(
                    "Step names must be unique; input is reserved."
                )
            if step.total is not None and step.select is None:
                raise ValueError(
                    "A pagination total requires a list selection."
                )
            for ptr in step.match:
                if ptr and not ptr.startswith("/"):
                    raise ValueError("Expected a JSON Pointer for each check.")
            for ref in step.refs():
                if ref.source not in known:
                    raise ValueError(
                        "A field must come from an earlier step or input."
                    )
                if ref.source == "input" and ref.pointer not in input_pointers:
                    raise ValueError("Unknown business input.")
                used.add(ref.source)
            known.add(step.key)
        if known - {"input", self.steps[-1].key} - used:
            raise ValueError(
                "Each intermediate step must feed the business action."
            )
        return self

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {k: {"type": v} for k, v in self.inputs.items()},
            "required": list(self.inputs),
            "additionalProperties": False,
        }

    def projections(self) -> dict[str, set[str]]:
        needed = {step.key: set() for step in self.steps}
        for step in self.steps:
            for ref in step.refs():
                if ref.source != "input":
                    needed[ref.source].add(ref.pointer)
        return needed


class ActionBlocked(ValueError):
    """A short, safe reason suitable for a persisted checkpoint."""

    def __init__(self, reason: str, step: str | None = None) -> None:
        super().__init__(reason)
        self.step = step
        self.code = reason
        self.diagnostic: dict | None = None

    def with_cause(self, stage: str, error: Exception) -> "ActionBlocked":
        self.diagnostic = report_failure(
            self.code, stage, error, step=self.step
        )
        return self


def _field(value: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        # Arrays are resolved explicitly by select and uniqueness checks.
        if not isinstance(value, dict) or key not in value:
            raise ActionBlocked("field_missing")
        value = value[key]
    return value


def _resolve(bindings: dict, inputs: dict, outputs: dict) -> dict:
    def value(ref):
        if not isinstance(ref, FieldRef):
            return ref
        if ref.source == "input":
            return _field(inputs, ref.pointer)
        return outputs[ref.source][ref.pointer]

    return {key: value(ref) for key, ref in bindings.items()}


def _wire(step: ActionStep, inputs: dict, outputs: dict) -> dict:
    return {
        k: _resolve(v, inputs, outputs)
        for k, v in (("path", step.path), ("query", step.query))
        if v
    }


@dataclass
class BoundRead:
    tool: PublishedBusinessTool
    route: str
    revision: str
    probe_scope: str
    sample: dict
    arguments: Callable[[dict, bool], dict]


def _verification_inputs(
    plan: ActionPlan, reads: list[BoundRead], supplied: dict
) -> dict:
    inputs = supplied.copy()
    for step, read in zip(plan.steps, reads, strict=True):
        for location, bindings in (("path", step.path), ("query", step.query)):
            approved = read.sample.get(location, {})
            if not isinstance(approved, dict):
                continue
            for parameter, ref in bindings.items():
                if (
                    parameter not in approved
                    or not isinstance(ref, FieldRef)
                    or ref.source != "input"
                ):
                    continue
                name = ref.pointer[1:].replace("~1", "/").replace("~0", "~")
                if name not in inputs:
                    value = approved[parameter]
                    try:
                        validate(
                            {"type": plan.inputs[name]},
                            value,
                            "Approved business input",
                        )
                    except ValueError:
                        continue
                    inputs[name] = value
    return inputs


async def _execute(
    step: ActionStep,
    bound: BoundRead,
    inputs: dict,
    outputs: dict,
    projections: set[str],
    state: AgentState,
    *,
    verifying: bool,
) -> tuple[Any, dict]:
    wire = _wire(step, inputs, outputs)
    arguments = await asyncio.to_thread(bound.arguments, wire, verifying)
    permission = await PermissionEngine(
        state.permission_context
    ).check_permission(bound.tool, arguments)
    if permission.behavior != PermissionBehavior.ALLOW:
        raise ActionBlocked("step_permission_required")
    chunk = await bound.tool.call(**arguments)
    result = json.loads(chunk.content[0].text)["result"]
    checks = _resolve(step.match, inputs, outputs)

    def matches(row):
        for key, expected in checks.items():
            actual = _field(row, key)
            if (
                isinstance(actual, bool) != isinstance(expected, bool)
                or actual != expected
            ):
                return False
        return True

    if step.select is not None:
        rows = _field(result, step.select)
        if not isinstance(rows, list):
            raise ActionBlocked("expected_candidate_list")
        if step.total is not None:
            total = _field(result, step.total)
            if type(total) is not int or total < 0:
                raise ActionBlocked("invalid_result_count")
            if total != len(rows):
                raise ActionBlocked("incomplete_candidate_list")
        rows = [row for row in rows if matches(row)]
        if not rows:
            raise ActionBlocked("no_matching_record")
        if len(rows) > 1:
            raise ActionBlocked("ambiguous_records")
        result = rows[0]
    elif not matches(result):
        raise ActionBlocked("business_result_mismatch")
    projected = {ptr: _field(result, ptr) for ptr in projections}
    if any(
        type(value) not in (str, int, float, bool)
        for value in projected.values()
    ):
        raise ActionBlocked("expected_scalar_dependency")
    return result, projected


def action_view(record: dict, blocked: ActionBlocked | None = None) -> dict:
    completed = len(record["passed"])
    steps = record["plan"]["steps"]
    blocked_step = blocked.step if blocked else record.get("blocked_step")
    if (blocked or record.get("reason")) and not blocked_step:
        blocked_step = steps[0]["key"]
    if blocked_step:
        index = next(
            i for i, step in enumerate(steps) if step["key"] == blocked_step
        )
        completed = min(completed, index)
    view = {
        "action_id": record["id"],
        "business": record["plan"]["business"],
        "title": record["plan"]["title"],
        "status": "pending" if blocked else record["status"],
        "reason": str(blocked) if blocked else record.get("reason"),
        "completed": completed,
        "total": len(steps),
        "blocked_step": blocked_step,
        "next_step": steps[completed]["key"]
        if completed < len(steps)
        else None,
        "steps": [
            {
                "key": s["key"],
                "operation": s["operation"],
                "method": "GET"
                if s["key"] in record.get("routes", {})
                else None,
                "route": record.get("routes", {}).get(s["key"]),
                "status": "passed"
                if i < completed
                else "blocked"
                if s["key"] == blocked_step
                else "pending",
            }
            for i, s in enumerate(steps)
        ],
    }
    if record.get("missing_inputs"):
        view["missing_inputs"] = record["missing_inputs"]
    diagnostic = blocked.diagnostic if blocked else record.get("diagnostic")
    if diagnostic:
        view["diagnostic"] = diagnostic
    return view


Binder = Callable[[dict, ActionPlan], list[BoundRead]]


async def verify_action(
    path: Path,
    bind: Binder,
    state: AgentState,
    *,
    draft: dict | None = None,
    poll_seconds: float = PUBLICATION_POLL_SECONDS,
) -> dict:
    """Atomic writes track each successful GET. The lock is released on process exit."""
    from ..app.tool_login import LoginEnded, LoginFailed

    if draft is None and not path.is_file():
        raise ValueError("No such action for this Agent.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with _publication_lock(path, poll_seconds=poll_seconds):
        previous = json.loads(path.read_bytes()) if path.is_file() else None
        if previous is None and draft is None:
            raise ValueError("No such action for this Agent.")
        record = draft if draft is not None else previous
        plan = ActionPlan.model_validate(record["plan"])
        supplied = record["sample"]
        validate(
            {**plan.input_schema(), "required": []},
            supplied,
            "Verification input",
        )
        past = previous["passed"] if previous else []
        needed = plan.projections()
        record.pop("missing_inputs", None)
        record.pop("diagnostic", None)
        if previous is None:
            record.update(
                status="pending", reason=None, passed=[], revisions=[]
            )
            write_definition(path, record)
        try:
            reads = await asyncio.to_thread(bind, record, plan)
        except ActionBlocked as exc:
            # Keep resumable evidence during a temporary loss of a binding.
            record.update(
                status="pending",
                reason=str(exc),
                passed=past
                if previous
                and previous["plan"] == record["plan"]
                and previous["sample"] == supplied
                else [],
                blocked_step=exc.step,
                diagnostic=exc.diagnostic,
                revisions=[],
            )
            write_definition(path, record)
            return action_view(record)
        revisions = [read.revision for read in reads]
        record["routes"] = {
            step.key: read.route
            for step, read in zip(plan.steps, reads, strict=True)
        }
        inputs = _verification_inputs(plan, reads, supplied)
        missing = sorted(plan.inputs.keys() - inputs.keys())
        if missing:
            record.update(
                status="pending",
                reason="verification_inputs_required",
                missing_inputs=missing,
                passed=[],
                revisions=revisions,
                blocked_step=None,
            )
            write_definition(path, record)
            return action_view(record)
        # Reuse only the common validated prefix of this plan, sample and identity.
        fingerprints = [
            definition_digest(
                [
                    read.revision,
                    read.probe_scope,
                    step.model_dump(),
                    inputs,
                    plan.inputs,
                    sorted(needed[step.key]),
                ]
            )
            for step, read in zip(plan.steps, reads, strict=True)
        ]
        passed = []
        outputs = {}
        for i, (fingerprint, checkpoint) in enumerate(zip(fingerprints, past)):
            if checkpoint["fingerprint"] != fingerprint:
                break
            # A resumed validation must still be allowed to use each prerequisite.
            args = await asyncio.to_thread(
                reads[i].arguments, _wire(plan.steps[i], inputs, outputs), True
            )
            decision = await PermissionEngine(
                state.permission_context
            ).check_permission(reads[i].tool, args)
            if decision.behavior != PermissionBehavior.ALLOW:
                break
            passed.append(checkpoint)
            outputs[plan.steps[i].key] = checkpoint["values"]
        if (
            len(passed) == len(reads)
            and previous
            and previous["status"] == "registered"
            and previous["plan"] == record["plan"]
        ):
            return action_view(previous)
        record.update(
            passed=passed,
            status="pending",
            reason=None,
            blocked_step=None,
            revisions=revisions,
        )
        write_definition(path, record)
        for i in range(len(passed), len(reads)):
            step = plan.steps[i]
            try:
                _, values = await _execute(
                    step,
                    reads[i],
                    inputs,
                    outputs,
                    needed[step.key],
                    state,
                    verifying=True,
                )
            except (
                LoginEnded,
                LoginFailed,
                ValueError,
                httpx.HTTPError,
                OSError,
                subprocess.TimeoutExpired,
            ) as exc:
                if isinstance(exc, ActionBlocked):
                    code = exc.code
                elif isinstance(exc, (LoginEnded, LoginFailed)):
                    code = "authentication_required"
                elif isinstance(exc, httpx.HTTPStatusError):
                    code = f"upstream_http_{exc.response.status_code}"
                else:
                    code = "verification_failed"
                record.update(
                    reason=code,
                    blocked_step=step.key,
                    diagnostic=report_failure(
                        code, "action_probe", exc, step=step.key
                    ),
                )
                write_definition(path, record)
                return action_view(record)
            outputs[step.key] = values
            passed.append({"fingerprint": fingerprints[i], "values": values})
            write_definition(path, record)
        # A connection or a source file could change while a probe was in flight.
        try:
            current = await asyncio.to_thread(bind, record, plan)
            for step, before, after in zip(
                plan.steps, reads, current, strict=True
            ):
                if before.revision != after.revision:
                    raise ActionBlocked(
                        "action_requires_verification", step.key
                    )
        except ActionBlocked as exc:
            record.update(
                reason=exc.code,
                blocked_step=exc.step or plan.steps[0].key,
                diagnostic=exc.diagnostic,
            )
            write_definition(path, record)
            return action_view(record)
        record["status"] = "registered"
        write_definition(path, record)
        return action_view(record)


def action_tool(
    record: dict, bind: Binder, path: Path, reads: list[BoundRead]
) -> PublishedBusinessTool:
    plan = ActionPlan.model_validate(record["plan"])

    async def run(_agent_state: AgentState, **inputs) -> dict:
        validate(plan.input_schema(), inputs, "Business input")
        # Reload both the publication and the host grants for long-lived tool handles.
        current = json.loads(path.read_bytes())
        if (
            current["status"] != "registered"
            or current["plan"] != record["plan"]
            or current["revisions"] != record["revisions"]
        ):
            raise ActionBlocked("action_requires_verification")
        reads = await asyncio.to_thread(bind, current, plan)
        if current["revisions"] != [r.revision for r in reads]:
            raise ActionBlocked("action_requires_verification")
        outputs = {}
        needed = plan.projections()
        for step, read in zip(plan.steps, reads, strict=True):
            result, outputs[step.key] = await _execute(
                step,
                read,
                inputs,
                outputs,
                needed[step.key],
                _agent_state,
                verifying=False,
            )
        return {"result": result}

    class ActionTool(PublishedBusinessTool):
        async def visible_in_catalog(self, state: AgentState) -> bool:
            if not await super().visible_in_catalog(state):
                return False
            for read in reads:
                if not await read.tool.visible_in_catalog(state):
                    return False
            return True

    return ActionTool(
        run,
        name="business_action_" + record["id"][:32],
        display_name=plan.title,
        description=plan.title,
        business=plan.business,
        action="query",
        input_schema=plan.input_schema(),
        is_state_injected=True,
        is_read_only=True,
    )
