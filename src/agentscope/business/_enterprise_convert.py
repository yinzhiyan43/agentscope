# -*- coding: utf-8 -*-
"""Resolve authorized contracts and compile API or source-backed operations."""

import json
import re
import subprocess
from collections import OrderedDict, defaultdict
from pathlib import Path
from threading import Lock
from typing import Any
from weakref import WeakValueDictionary

import yaml
from jsonschema import SchemaError

from ._business import (
    FORMAT,
    bind_route,
    compile_source_plan,
    node,
    resolve_path_items,
)
from ._business import definition_digest as _digest
from ._enterprise_auth import (
    LoginRecipe,
    compile_authentication,
    inspect_authentication,
)
from ._enterprise_config import EnterpriseLimits, EnterpriseReadAPI
from ._enterprise_diagnostics import report_failure
from ._file_cache import FileCache, FileStamp, file_stamp
from ._save_registry import SaveBusinessRegistry

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


def _read(path: Path, limits: EnterpriseLimits) -> str:
    if not path.is_file():
        raise ValueError("Expected an OpenAPI file.")
    with path.open("rb") as handle:
        raw = handle.read(limits.max_input_bytes + 1)
    if len(raw) > limits.max_input_bytes:
        raise ValueError(
            f"The OpenAPI file exceeds {limits.max_input_bytes} bytes."
        )
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
    ) as exc:
        record["reason"] = "unsupported_contract"
        record["diagnostic"] = report_failure(
            record["reason"], "contract_compilation", exc
        )
    if source:
        record["source_location"] = str(Path(source).expanduser().resolve())
    if (
        method not in ("get", "post")
        and record["reason"] == "operation_confirmation_required"
    ):
        record["reason"] = "unsupported_write"
    return record


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


class EnterpriseCompiler:
    """Compile only from the configured account's sources and API contracts."""

    def __init__(
        self,
        limits: EnterpriseLimits,
        source_roots: dict[str, list[Path]],
        api_documents: dict[str, dict[str, Path]],
        read_apis: dict[str, list[EnterpriseReadAPI]],
        save_registries: dict[str, list[SaveBusinessRegistry]],
    ) -> None:
        self.limits = limits
        self.source_roots = source_roots
        self.api_documents = api_documents
        self.read_apis = read_apis
        self.save_registries = save_registries
        self._documents = FileCache(
            self._load_document, capacity=24, max_bytes=32 * 1024 * 1024
        )
        self._scans: OrderedDict[
            tuple[Path, str | None], tuple[dict, dict[str, FileStamp | None]]
        ] = OrderedDict()
        self._scan_lock = Lock()
        self._scan_jobs = WeakValueDictionary()

    def _load_document(self, path: Path) -> tuple[dict, str, dict]:
        document = _document(_read(path, self.limits))
        return document, _digest(document), resolve_path_items(document, [])

    @staticmethod
    def _scan_current(
        root: Path, dependencies: dict[str, FileStamp | None]
    ) -> bool:
        for relative, expected in dependencies.items():
            try:
                stamp = file_stamp(root / relative, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                stamp = None
            except OSError:
                return False
            if stamp != expected:
                return False
        return True

    def _scan(self, root: Path, selected: str | None) -> dict:
        key = (root, selected)
        # Concurrent imports of the same source can share a scan. Other
        # projects and chat tool assembly never wait for this subprocess.
        with self._scan_lock:
            job = self._scan_jobs.setdefault(key, Lock())
        with job:
            with self._scan_lock:
                cached = self._scans.pop(key, None)
            if cached is not None:
                result, dependencies = cached
                if self._scan_current(root, dependencies):
                    with self._scan_lock:
                        self._scans[key] = cached
                        if len(self._scans) > 8:
                            self._scans.popitem(last=False)
                    return result
            result = node(
                "extract.mjs",
                str(root),
                "--discover",
                "",
                json.dumps(
                    {
                        "selectedFile": selected,
                        "discoveryLimit": self.limits.max_discovery_candidates,
                        "includePlans": True,
                        "inferLocalHttpClients": True,
                        "trackDiscovery": True,
                    }
                ),
            )
            if manifest := result.pop("dependencies", None):
                dependencies = {
                    relative: FileStamp(*map(int, stamp))
                    if stamp is not None
                    else None
                    for relative, stamp in manifest.items()
                }
                if self._scan_current(root, dependencies):
                    with self._scan_lock:
                        self._scans[key] = (result, dependencies)
                        if len(self._scans) > 8:
                            self._scans.popitem(last=False)
            return result

    def inspect_authentication(
        self, user_id: str, source: str, route: str, offset: int
    ) -> dict:
        self.limits.check_input(source)
        _, document, _, _, _ = self._prepare(
            user_id, source, contract_only=True
        )
        if document is None:
            return {"status": "source_trace_required"}
        return inspect_authentication(document, route, offset)

    def source_path(self, user_id: str, value: str) -> Path:
        if len(value) > self.limits.max_source_path_chars:
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
            return (
                "api",
                self._documents.get(path)[0],
                [],
                [],
                path.name,
            )
        if not value.startswith(("/", "~/")):
            return "api", _document(value), [], [], "OpenAPI"
        path = self.source_path(user_id, value)
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
                return (
                    "api",
                    self._documents.get(path)[0],
                    [],
                    [],
                    path.name,
                )
        else:
            root, selected = path, None

        doc_path = (
            self._embedded_document(root)
            if selected is None or contract_only
            else None
        )
        doc = self._documents.get(doc_path)[0] if doc_path else None
        if contract_only:
            return "source", doc, [], [], path.name
        diagnostics = []
        try:
            scan = self._scan(root, selected)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            report_failure("source_scan_unavailable", "source_discovery", exc)
            scan = {"actions": [], "diagnostics": []}
            diagnostics.append("source_scan_unavailable")
        if scan.get("truncated"):
            diagnostics.append("source_scan_limited")
        if scan["diagnostics"]:
            diagnostics.append("some_source_actions_unsupported")
        label = (
            doc_path.relative_to(root).as_posix() if doc_path else path.name
        )
        return (
            "source",
            doc,
            [dict(action) for action in scan["actions"]],
            diagnostics,
            label,
        )

    def catalog(self, user_id: str) -> dict:
        """Index the authorized contracts once, including write targets."""
        paths = {a.document for a in self.read_apis.get(user_id, [])}
        paths.update(r.openapi for r in self.save_registries.get(user_id, []))
        catalog = {}
        for path in sorted(paths):
            try:
                doc, revision, resolved = self._documents.get(path)
            except (OSError, ValueError, TypeError, RecursionError):
                continue
            entry = catalog.setdefault(
                revision,
                {
                    "document": doc,
                    "resolved": resolved,
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

    def compile(
        self,
        user_id: str,
        value: str,
        scope: str = "",
        authentication: LoginRecipe | None = None,
    ) -> tuple:
        kind, doc, actions, diagnostics, label = self._prepare(user_id, value)
        catalog = self.catalog(user_id)
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
                        sibling_doc, sibling_revision, _ = self._documents.get(
                            path
                        )
                        contracts.setdefault(
                            sibling_revision,
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
                    if len(records) >= self.limits.max_operations:
                        raise ValueError(
                            f"Limit each import to {self.limits.max_operations:,} business operations."
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
                        except (
                            KeyError,
                            TypeError,
                            ValueError,
                            SchemaError,
                        ) as exc:
                            record["reason"] = (
                                "unsupported_authentication_contract"
                            )
                            record["diagnostic"] = report_failure(
                                record["reason"],
                                "authentication_compilation",
                                exc,
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
