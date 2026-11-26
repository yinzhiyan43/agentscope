# -*- coding: utf-8 -*-
"""Compile the authentication contract for an imported business operation."""

import re
from graphlib import CycleError, TopologicalSorter

from pydantic import BaseModel, ConfigDict, Field

from ._response_match import ResponseMatch
from ._business import bind_route, dereference, resolve_path_items

_ROUTE = re.compile(
    r"/(?:[\w~-]+(?:\.[\w~-]+)*|\{[\w-]+\})(?:/(?:[\w~-]+(?:\.[\w~-]+)*|\{[\w-]+\}))*"
)


class LoginLookup(BaseModel):
    """Read choices from a documented GET. Paths in JSON are JSON Pointers."""

    model_config = ConfigDict(extra="forbid")
    route: str
    rows: str = Field(
        default="",
        description="Pointer to the candidate list or single result.",
    )
    value: str = Field(
        default="",
        description="Pointer inside each candidate to its wire value.",
    )
    label: str | None = Field(
        default=None,
        description="Pointer inside each candidate to its display label.",
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="Map GET parameter names to login fields. Upstream lookups are resolved first.",
    )

    filters: dict[str, str] = Field(
        default_factory=dict,
        description="Keep rows whose JSON Pointer value equals the named prior login field.",
    )


class IdentityCheck(BaseModel):
    """Compare authenticated identity fields with values from this login."""

    model_config = ConfigDict(extra="forbid")
    route: str
    fields: dict[str, str] = Field(
        min_length=1,
        description="Map login field names to JSON Pointers in the identity response.",
    )


class LoginChallenge(BaseModel):
    """A human-entered one-time answer with an optional issuance operation."""

    model_config = ConfigDict(extra="forbid")
    answer: str
    route: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    bindings: dict[str, str] = Field(default_factory=dict)
    when: list[ResponseMatch] = Field(default_factory=list)
    success: ResponseMatch | None = None
    expired: list[ResponseMatch] = Field(default_factory=list)


class LoginRecipe(BaseModel):
    """Login metadata inferred by the Agent. Contains no credential values."""

    model_config = ConfigDict(extra="forbid")
    route: str = Field(
        description="Public JSON POST login path in the contract."
    )
    credential: str = Field(
        description="JSON Pointer to the issued credential."
    )
    lookups: dict[str, LoginLookup] = Field(
        default_factory=dict,
        description="Map login fields or intermediate lookup names to their source GET.",
    )
    identity: IdentityCheck | None = None
    challenge: LoginChallenge | None = None
    expired: list[ResponseMatch] = Field(default_factory=list)
    scheme: str | None = Field(
        default=None, description="OpenAPI security scheme name."
    )


def _public_operation(doc: dict, route: str, method: str) -> dict:
    if not _ROUTE.fullmatch(route):
        raise ValueError("Expected a documented relative API path")
    operation = doc["paths"][route][method]
    security = operation.get("security", doc.get("security"))
    if security and {} not in security:
        raise ValueError(
            "Login and its prerequisite APIs must allow anonymous access"
        )
    return bind_route(doc, route, method, "auth_step")


def _compile_login(doc: dict, recipe: LoginRecipe) -> dict:
    login = _public_operation(doc, recipe.route, "post")
    if login["pathNames"] or any(
        p.get("required") for p in login["parameters"]
    ):
        raise ValueError("Expected a JSON-body login endpoint")
    schema = login["bodySchema"] or {}
    if not isinstance(schema, dict):
        raise TypeError("Expected an object schema for login")
    fields = schema.get("properties", {})
    if not fields:
        raise ValueError("Login fields must be present in the API definition")
    if any(not isinstance(v, dict) for v in fields.values()):
        raise ValueError("Unsupported login field schema")
    if any(not name or len(name) > 80 for name in fields):
        raise ValueError("Unsupported login field name")
    schema = {
        **schema,
        "required": [
            name
            for name in schema.get("required", [])
            if not fields.get(name, {}).get("readOnly")
        ],
    }
    graph = {}
    for field, dep in recipe.lookups.items():
        if (
            not field
            or len(field) > 80
            or fields.get(field, {}).get("readOnly")
        ):
            raise ValueError("Invalid lookup field")
        sources = set(dep.parameters.values()) | set(dep.filters.values())
        if not sources <= fields.keys() | recipe.lookups.keys():
            raise ValueError("Unknown login field used in a lookup")
        for name in sources:
            spec = fields.get(name, {})
            if (
                spec.get("writeOnly")
                or spec.get("readOnly")
                or spec.get("format") == "password"
            ):
                raise ValueError(
                    "Private login fields cannot be lookup parameters"
                )
        graph[field] = sources & recipe.lookups.keys()
    used = set().union(*graph.values())
    if recipe.challenge:
        used.update(recipe.challenge.parameters.values())
    if recipe.lookups.keys() - fields.keys() - used:
        raise ValueError(
            "An intermediate lookup must feed another login dependency"
        )
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError:
        raise ValueError("Cyclic login lookup dependency") from None
    steps = []
    for name in order:
        dep = recipe.lookups[name]
        op = _public_operation(doc, dep.route, "get")
        params = {p["name"]: p for p in op["parameters"]}
        required = {p["name"] for p in op["parameters"] if p.get("required")}
        if (
            len(params) != len(op["parameters"])
            or op["bodyRequired"]
            or not required <= dep.parameters.keys() <= params.keys()
        ):
            raise ValueError(
                "Lookup arguments disagree with the GET definition"
            )
        for pointer in (dep.rows, dep.value, dep.label, *dep.filters):
            if pointer and not pointer.startswith("/"):
                raise ValueError("Expected a JSON Pointer in the lookup")
        lookup = dep.model_dump(
            exclude={"filters"} if not dep.filters else set()
        )
        steps.append({"field": name, **lookup, "params": params})
    if recipe.credential and not recipe.credential.startswith("/"):
        raise ValueError("Expected a JSON Pointer to the issued credential")
    extra = {}
    if challenge := recipe.challenge:
        outputs = {challenge.answer, *challenge.bindings}
        sources = set(challenge.parameters.values())
        lookup_sources = {
            source
            for dep in recipe.lookups.values()
            for source in (*dep.parameters.values(), *dep.filters.values())
        }
        if (
            not outputs <= fields.keys()
            or challenge.answer in challenge.bindings
            or (challenge.when and outputs & set(schema["required"]))
            or fields[challenge.answer].get("type") != "string"
            or any(fields[name].get("readOnly") for name in outputs)
            or outputs & (sources | recipe.lookups.keys() | lookup_sources)
            or (recipe.identity and outputs & recipe.identity.fields.keys())
            or not sources <= fields.keys() | recipe.lookups.keys()
            or any(fields.get(name, {}).get("readOnly") for name in sources)
            or any(
                not pointer.startswith("/")
                for pointer in challenge.bindings.values()
            )
        ):
            raise ValueError("Invalid authentication challenge dependencies")
        compiled = challenge.model_dump()
        if challenge.route:
            if challenge.route == recipe.route:
                raise ValueError(
                    "Challenge issuance requires a separate operation"
                )
            issue = _public_operation(doc, challenge.route, "post")
            body = issue["bodySchema"]
            if (
                (body is not None and not isinstance(body, dict))
                or issue["pathNames"]
                or any(p.get("required") for p in issue["parameters"])
                or not set((body or {}).get("required", []))
                <= challenge.parameters.keys()
                or not challenge.parameters.keys()
                <= (body or {}).get("properties", {}).keys()
            ):
                raise ValueError(
                    "Challenge inputs disagree with the issuance endpoint"
                )
            compiled["body_schema"] = body
        elif (
            challenge.parameters
            or challenge.bindings
            or challenge.expired
            or challenge.success
        ):
            raise ValueError(
                "Challenge issuance metadata requires a documented route"
            )
        extra["challenge"] = compiled
    if recipe.identity:
        check = recipe.identity
        if not _ROUTE.fullmatch(check.route) or "{" in check.route:
            raise ValueError(
                "Expected a documented identity GET without path arguments"
            )
        op = bind_route(doc, check.route, "get", "verify_identity")
        if op["bodyRequired"] or any(
            p.get("required") for p in op["parameters"]
        ):
            raise ValueError("Identity GET must not need additional arguments")
        if not check.fields.keys() <= fields.keys() or any(
            not pointer.startswith("/")
            or fields[name].get("writeOnly")
            or fields[name].get("readOnly")
            or fields[name].get("format") == "password"
            for name, pointer in check.fields.items()
        ):
            raise ValueError("Invalid identity field mapping")
        extra["identity"] = check.model_dump()
    if recipe.expired:
        extra["expired"] = [rule.model_dump() for rule in recipe.expired]
    return {
        **extra,
        "route": recipe.route,
        "credential": recipe.credential,
        "schema": schema,
        "steps": steps,
    }


def compile_authentication(
    doc: dict,
    operation: dict,
    recipe: LoginRecipe | None,
) -> dict:
    """Respect operation overrides, anonymous access and scheme alternatives."""
    if not isinstance(operation, dict):
        raise TypeError("Expected an OpenAPI operation")
    security = operation.get("security", doc.get("security"))
    if security is None and recipe is None:
        return {"mode": "legacy"}
    if security is None and recipe and recipe.scheme:
        security = [{recipe.scheme: []}]
    if security == [] or (isinstance(security, list) and {} in security):
        return {"mode": "public"}
    schemes = doc.get("components", {}).get("securitySchemes", {})
    for choice in security or []:
        if not isinstance(choice, dict) or len(choice) != 1:
            continue
        name = next(iter(choice))
        if recipe and recipe.scheme and recipe.scheme != name:
            continue
        spec = dereference(schemes.get(name, {}), doc)
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "http":
            mode = spec.get("scheme", "").lower()
            if mode not in ("basic", "bearer") or (mode == "basic" and recipe):
                continue
            target = {"mode": mode, "scheme": name}
        elif spec.get("type") == "apiKey" and spec.get("in") == "header":
            header = spec["name"]
            if not re.fullmatch(
                r"[A-Za-z0-9_-]+", header
            ) or header.lower() in {
                "host",
                "connection",
                "content-length",
                "transfer-encoding",
                "proxy-authorization",
                "cookie",
            }:
                continue
            target = {"mode": "header", "header": header, "scheme": name}
        else:
            continue
        if recipe:
            if recipe.identity:
                identity_operation = doc["paths"][recipe.identity.route]["get"]
                requirements = identity_operation.get(
                    "security", doc.get("security")
                )
                if (
                    not requirements
                    or {} in requirements
                    or {name: []} not in requirements
                ):
                    raise ValueError(
                        "Identity GET must use the same authentication scheme"
                    )
            target["login"] = _compile_login(doc, recipe)
        return target
    raise ValueError("No supported authentication contract")


def _response_fields(schema: dict | bool | None) -> dict:
    pointers = {}

    def visit(node: object, pointer: str) -> None:
        if not isinstance(node, dict) or len(pointers) >= 80:
            return
        pointers[pointer] = node.get("type", pointers.get(pointer, "unknown"))
        for child in node.get("allOf", []):
            visit(child, pointer)
        for name, child in node.get("properties", {}).items():
            visit(
                child,
                pointer + "/" + name.replace("~", "~0").replace("/", "~1"),
            )
        if "items" in node:
            visit(node["items"], pointer + "/*")

    visit(schema, "")
    return pointers


def inspect_authentication(doc: dict, route: str, offset: int) -> dict:
    """Show contract metadata to the Agent without sample values or HTTP calls."""
    doc = resolve_path_items(doc, [])
    if offset < 0:
        raise ValueError("Expected a nonnegative offset")
    schemes = {}
    for name, raw in (
        doc.get("components", {}).get("securitySchemes", {}).items()
    ):
        spec = dereference(raw, doc)
        schemes[name] = {
            k: spec[k] for k in ("type", "scheme", "in", "name") if k in spec
        }
    operations = []
    for path, item in doc["paths"].items():
        if route and path != route:
            continue
        for method in ("get", "post"):
            if method not in item:
                continue
            raw = item[method]
            security = raw.get("security", doc.get("security", []))
            if not route and security and {} not in security:
                continue
            row = {
                "method": method.upper(),
                "route": path,
                "summary": str(
                    raw.get("summary") or raw.get("operationId") or ""
                )[:160],
            }
            if route:
                op = bind_route(doc, path, method, "inspect_auth")
                body = op["bodySchema"] or {}
                row["fields"] = [
                    {
                        "name": key,
                        "type": value.get("type"),
                        **{
                            k: value[k]
                            for k in ("readOnly", "writeOnly", "format")
                            if k in value
                        },
                        "required": key in body.get("required", []),
                        "description": str(value.get("description", ""))[:200],
                    }
                    for key, value in body.get("properties", {}).items()
                    if isinstance(value, dict)
                ]
                row["parameters"] = [
                    {k: p[k] for k in ("name", "in", "required") if k in p}
                    for p in op["parameters"]
                ]
                row["security"] = security
                row["response_fields"] = {
                    status: _response_fields(schema)
                    for status, schema in op["responses"].items()
                }
                # A service can describe a second-factor challenge in a 4xx
                # response. Expose its field paths, never example payloads.
                for status, response in raw.get("responses", {}).items():
                    if status not in row["response_fields"]:
                        content = dereference(response, doc).get("content", {})
                        media = content.get(
                            "application/json", content.get("*/*", {})
                        )
                        row["response_fields"][status] = _response_fields(
                            media.get("schema")
                        )
            operations.append(row)
    return {
        "schemes": schemes,
        "operations": operations[offset : offset + 20],
        "total": len(operations),
    }
