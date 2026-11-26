# -*- coding: utf-8 -*-
"""Orchestrate enterprise onboarding and persistence for each account and Agent."""

import asyncio
import hashlib
import json
import re
import subprocess
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

import httpx
from jsonschema import SchemaError

from agentscope.state import AgentState
from agentscope.tool import ToolBase

from ._business import (
    compile_source_plan,
    preview,
    write_definition,
)
from ._business import definition_digest as _digest
from ._enterprise_action import (
    ActionBlocked,
    ActionPlan,
    BoundRead,
    action_tool,
    action_view,
    verify_action,
)
from ._enterprise_auth import LoginRecipe, compile_authentication
from ._enterprise_catalog import (
    EnterpriseSkillView,
    _brief_record,
    _candidate,
    _inventory_read,
    _login_key,
    _public_records,
    _record_revision,
    _runtime,
    _sample_inputs,
    bindings,
    enterprise_skill_views,
)
from ._enterprise_config import (
    EnterpriseLimits,
    EnterpriseReadAPI,
    load_enterprise_config,
)
from ._enterprise_convert import (
    EnterpriseCompiler,
    _api_artifact,
    _operation_in_scope,
)
from ._enterprise_diagnostics import report_failure
from ._enterprise_tools import chat_tools
from ._file_cache import FileCache, file_stamp, json_paths
from ._save_registry import SaveBusinessRegistry

if TYPE_CHECKING:
    from ..app._enterprise_login import EnterpriseLogin

__all__ = [
    "EnterpriseBusiness",
    "EnterpriseReadAPI",
    "EnterpriseSkillView",
    "enterprise_skill_views",
]


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
        limits: EnterpriseLimits | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.source_roots = {
            user: [root.expanduser().resolve() for root in roots]
            for user, roots in (source_roots or {}).items()
        }
        self.local_user = local_user
        self.save_registries = save_registries or {}
        for registries in self.save_registries.values():
            if len({r.import_registrar_name for r in registries}) != len(
                registries
            ):
                raise ValueError(
                    "Each save target for a user needs a unique tool_prefix."
                )
        self.read_apis = read_apis or {}
        self.api_documents = api_documents or {}
        self.limits = limits if limits is not None else EnterpriseLimits()
        self.compiler = EnterpriseCompiler(
            self.limits,
            self.source_roots,
            self.api_documents,
            self.read_apis,
            self.save_registries,
        )
        self.login: EnterpriseLogin | None = None
        self._receipts = FileCache(
            lambda path: json.loads(path.read_bytes()),
            max_bytes=64 * 1024 * 1024,
        )
        self._source_digests = FileCache(
            lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self._binding_lock = RLock()
        self._read_catalogs: OrderedDict = OrderedDict()
        self._read_artifacts: OrderedDict = OrderedDict()

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
        return chat_tools(self, user_id, agent_id, load_tools)

    @classmethod
    def from_environment(cls) -> "EnterpriseBusiness":
        """Load optional host-managed source and read endpoint grants."""
        return cls(**load_enterprise_config())

    def _directory(self, user_id: str, agent_id: str) -> Path:
        return self.directory / _digest([user_id, agent_id])

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
        snapshot = self._receipts.get(inventory) if inventory.is_file() else {}
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
            saved = self._receipts.get(receipt)
            records = {}
            for row in saved["records"]:
                record = dict(row)
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
        catalog = (
            self.compiler.catalog(user_id) if catalog is None else catalog
        )
        reads = []
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
            doc = entry["resolved"]
            try:
                artifact = self._read_artifact(record, doc)
                record = {**record, "artifact": artifact}
            except (KeyError, TypeError, ValueError, SchemaError) as exc:
                raise ActionBlocked(
                    "unsupported_contract", step.key
                ).with_cause("contract_binding", exc) from exc
            if artifact["plan"]["endpoint"]["method"] != "GET":
                raise ActionBlocked("documented_read_required", step.key)
            if src := record.get("source_location"):
                try:
                    root = self.compiler.source_path(user_id, src)
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
                            or self._source_digests.get(location) != expected
                        ):
                            raise ActionBlocked("source_changed", step.key)
                except ActionBlocked:
                    raise
                except (OSError, ValueError) as exc:
                    raise ActionBlocked("source_changed", step.key).with_cause(
                        "source_binding", exc
                    ) from exc
            if recipe or "authentication" not in record:
                try:
                    profile = compile_authentication(
                        doc, doc["paths"][route]["get"], recipe
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ActionBlocked(
                        "unsupported_authentication_contract", step.key
                    ).with_cause("authentication_binding", exc) from exc
                record = {**record, "authentication": profile}
            login_service = _login_key(record, api)
            credential_generation = None
            if login_service is not None:
                if self.login is None:
                    raise ActionBlocked("authentication_required", step.key)
                credential_generation = partial(
                    self.login.credential_generation, login_service
                )
            sample = api.samples[route]
            revision = _digest(
                [
                    _record_revision(record),
                    record["name"],
                    api.key,
                    api.success,
                    record["authentication"],
                    *([api.login_adapter] if api.login_adapter else []),
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

            try:
                tool = _runtime(record, api, self.login)
            except ValueError as exc:
                raise ActionBlocked(
                    "invalid_read_connection", step.key
                ) from exc
            reads.append(
                BoundRead(
                    tool,
                    route,
                    revision,
                    sample,
                    arguments,
                    login_service,
                    credential_generation,
                )
            )
        return reads

    def _read_artifact(self, record: dict, document: dict) -> dict:
        if "artifact" in record:
            return record["artifact"]
        # The immutable inventory record includes the contract digest and plan.
        # Retain the record together with the artifact to prevent identity reuse.
        key = id(record)
        with self._binding_lock:
            if key in self._read_artifacts:
                self._read_artifacts.move_to_end(key)
                return self._read_artifacts[key][1]
            artifact = _api_artifact(
                document, record["route"], "get", record["name"]
            )
            if "client_plan" in record:
                artifact = compile_source_plan(
                    record["client_plan"], artifact["operation"]
                )
            self._read_artifacts[key] = (record, artifact)
            if len(self._read_artifacts) > 128:
                self._read_artifacts.popitem(last=False)
            return artifact

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

        return await verify_action(
            path,
            bind,
            state,
            draft=draft,
            poll_seconds=self.limits.publication_poll_seconds,
        )

    def _action_progress(
        self,
        user_id: str,
        agent_id: str,
        action_id: str,
        offset: int,
        inventory_id: str = "",
        catalog: dict | None = None,
        *,
        state: AgentState | None = None,
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
        if state is not None:
            denied = {
                name
                for name, rules in state.permission_context.deny_rules.items()
                if any(not rule.rule_content for rule in rules)
            }
            records = [
                record
                for record in records
                if record.get("visible") is not False
                and "business_action_" + record["id"][:32] not in denied
                and all(
                    step["operation"] not in denied
                    for step in record["plan"]["steps"]
                )
            ]
        catalog = (
            self.compiler.catalog(user_id)
            if catalog is None and records
            else catalog
        )
        items = []
        for record in records[offset : offset + self.limits.page_size]:
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
        self.limits.check_input(source)

        def scan() -> str:
            kind, batch, records, diagnostics, _ = self.compiler.compile(
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
        catalog = self.compiler.catalog(user_id)
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
            bindings(
                self.login,
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
            _brief_record(row, self.limits, include_evidence=True)
            for row in filtered[offset : offset + self.limits.page_size]
        ]
        return {
            "inventory_id": batch,
            "kind": metadata["kind"],
            "business_scope": scope or metadata.get("business_scope", ""),
            "total": len(rows),
            "counts": dict(Counter(r["status"] for r in rows.values())),
            "diagnostics": [
                str(s)[: self.limits.summary_chars]
                for s in metadata.get("diagnostics", [])[
                    : self.limits.diagnostic_count
                ]
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
        self.limits.check_input(value)
        recipe = (
            LoginRecipe.model_validate(authentication)
            if authentication is not None
            else None
        )
        kind, batch, records, diagnostics, catalog = await asyncio.to_thread(
            self.compiler.compile,
            user_id,
            value,
            business_scope.strip(),
            recipe,
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
        semaphore = asyncio.Semaphore(self.limits.concurrent_probes)

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
                    record["authentication_issue"] = str(exc)[
                        : self.limits.summary_chars
                    ]
                    record["diagnostic"] = report_failure(
                        record["reason"], "read_authentication", exc
                    )
                    login_failures[group] = {
                        k: record[k]
                        for k in (
                            "reason",
                            "authentication_issue",
                            "diagnostic",
                        )
                    }
                except (
                    httpx.HTTPError,
                    ValueError,
                    subprocess.TimeoutExpired,
                    OSError,
                ) as exc:
                    record["reason"] = "verification_failed"
                    record["diagnostic"] = report_failure(
                        record["reason"], "read_probe", exc
                    )
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

    def _view(
        self, user_id: str, agent_id: str, batch: dict, catalog: dict
    ) -> dict:
        bindings(
            self.login, user_id, agent_id, batch, catalog, load_tools=False
        )
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
                user_id, agent_id, payload, self.compiler.catalog(user_id)
            )

        return await asyncio.to_thread(load)

    def _imported_tools(
        self, user_id: str, agent_id: str, catalog: dict
    ) -> dict:
        directory = self._directory(user_id, agent_id)
        # Check each receipt, including replacements made by other workers.
        files = []
        for path in json_paths(directory):
            try:
                files.append((path, file_stamp(path)))
            except FileNotFoundError:
                # A worker may remove a receipt after the directory was listed.
                continue
        grants = _digest(
            [
                (
                    revision,
                    [
                        (
                            api.key,
                            sorted(api.samples),
                            api.success,
                            api.login_adapter,
                            _digest(api.token),
                        )
                        for api in entry["reads"]
                    ],
                )
                for revision, entry in catalog.items()
            ]
        )
        version = (tuple(files), grants, self.login)
        key = (user_id, agent_id)
        with self._binding_lock:
            cached = self._read_catalogs.get(key)
            if cached is None or cached[0] != version:
                read_records = []
                save_records = []
                for path, _ in files:
                    try:
                        batch = self._receipts.get(path)
                    except FileNotFoundError:
                        continue
                    for row in batch["records"]:
                        record = dict(row)
                        record.setdefault(
                            "document_id", batch.get("document_id")
                        )
                        if record["method"] == "POST" and record.get(
                            "artifact"
                        ):
                            save_records.append(record)
                        elif record["status"] == "registered":
                            read_records.append(record)
                reads = bindings(
                    self.login,
                    user_id,
                    agent_id,
                    {"records": read_records},
                    catalog,
                    load_tools=True,
                )
                cached = (version, reads, save_records)
                self._read_catalogs[key] = cached
                if len(self._read_catalogs) > 64:
                    self._read_catalogs.popitem(last=False)
            self._read_catalogs.move_to_end(key)
        _, reads, saves = cached
        # Keep save publications and their confirmation objects fresh.
        return {
            **reads,
            **bindings(
                self.login,
                user_id,
                agent_id,
                {"records": [dict(record) for record in saves]},
                catalog,
                load_tools=True,
            ),
        }

    async def tools(self, user_id: str, agent_id: str) -> list[ToolBase]:
        if not (
            self.read_apis.get(user_id) or self.save_registries.get(user_id)
        ):
            return []

        def load() -> list[ToolBase]:
            catalog = self.compiler.catalog(user_id)
            tools = self._imported_tools(user_id, agent_id, catalog)

            def bind(action: dict, plan: ActionPlan) -> list[BoundRead]:
                return self._bind_action(user_id, agent_id, action, plan)

            actions = self._directory(user_id, agent_id) / "actions"
            for path in json_paths(actions):
                try:
                    action = self._receipts.get(path)
                except FileNotFoundError:
                    continue
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
