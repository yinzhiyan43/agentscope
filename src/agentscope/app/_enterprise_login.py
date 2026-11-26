# -*- coding: utf-8 -*-
"""Authenticate enterprise reads using the current chat's private login form."""

import asyncio
import json
from collections import OrderedDict
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterator,
    Mapping,
)
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from ..business._business import validate_api_base_url
from ..business._enterprise_config import EnterpriseLimits
from ..business._response_match import ResponseMatch
from .tool_login import (
    LoginEnded,
    LoginFailed,
    LoginField,
    LoginOption,
    LoginRejected,
    LoginStep,
    LoginSuccess,
    ToolLogin,
)


class EnterpriseLoginChanged(Exception):
    """A composed read must restart because its credentials changed."""


_pinned_generations: ContextVar[dict[str, str] | None] = ContextVar(
    "enterprise_login_generations", default=None
)


@contextmanager
def pin_enterprise_credentials() -> Iterator[dict[str, str]]:
    """Keep each service on one credential generation for a composed read."""
    pinned: dict[str, str] = {}
    token = _pinned_generations.set(pinned)
    try:
        yield pinned
    finally:
        _pinned_generations.reset(token)


def _at(value: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        value = (
            value[int(key)]
            if isinstance(value, list) and key.isdigit()
            else value[key]
        )
    return value


def _matches_lookup(row: Any, filters: dict, payload: dict) -> bool:
    try:
        return all(
            _same_value(_at(row, pointer), payload[field])
            for pointer, field in filters.items()
        )
    except (IndexError, KeyError, TypeError):
        return False


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _checked(schema: dict, value: Any) -> bool:
    return Draft202012Validator(
        schema, format_checker=FormatChecker()
    ).is_valid(value)


def _input(field: str, schema: dict, raw: str) -> Any:
    value: Any = raw
    if not _checked(schema, value):
        try:
            value = json.loads(raw)
        except ValueError:
            pass
    if not _checked(schema, value):
        raise LoginRejected(
            f"Enter a valid value for {field}.",
            code="invalidField",
            params={"field": field},
        )
    return value


@dataclass(frozen=True)
class EnterpriseLoginAdapter:
    """A trusted host callback returning a credential and stable identity components."""

    login: Callable[[ToolLogin, str, str], Awaitable[tuple[str, tuple]]]
    expired: tuple[ResponseMatch, ...] = ()


async def _login(
    broker: ToolLogin, service: str, base: str, profile: dict
) -> tuple[str, tuple | None]:
    login = profile.get("login")
    mode = profile["mode"]
    if not login:
        fields = (
            [
                LoginField(
                    name="username", label="Username", label_key="username"
                ),
                LoginField(
                    name="password",
                    label="Password",
                    label_key="password",
                    kind="secret",
                ),
            ]
            if mode == "basic"
            else [
                LoginField(
                    name="credential",
                    label=profile.get("header", profile["scheme"]),
                    kind="secret",
                )
            ]
        )

        async def direct(
            _step: LoginStep, values: Mapping[str, str]
        ) -> LoginSuccess:
            if mode == "basic":
                auth = httpx.BasicAuth(values["username"], values["password"])
                header = next(
                    auth.auth_flow(httpx.Request("GET", base))
                ).headers["Authorization"]
                return LoginSuccess(header)
            return LoginSuccess(_header_value(values["credential"], mode))

        header = await broker.request(
            service,
            LoginStep(
                title="API authentication",
                title_key="authTitle",
                description=base,
                submit_label="Continue",
                submit_label_key="continue",
                fields=fields,
            ),
            direct,
        )
        return header, None

    schema = login["schema"]
    props = schema["properties"]
    payload: dict = {}
    challenge = login.get("challenge") or {}

    async def collect(
        names: list[str],
        destination: str,
        *,
        lookup: bool = False,
        required: bool = False,
        finish: Callable[[dict], Awaitable[tuple[str, tuple | None] | None]]
        | None = None,
    ) -> Any:
        # Submit the last page to the service inside the private form callback.
        # A rejected login can then be corrected in the same form.
        for start in range(0, max(1, len(names)), 12):
            fields = [
                LoginField(
                    name=name,
                    label=props[name].get("title") or name,
                    kind=(
                        "one-time-code"
                        if name == challenge.get("answer")
                        else "text"
                        if lookup
                        else "secret"
                    ),
                    required=lookup
                    or required
                    or name == challenge.get("answer")
                    or name in schema.get("required", []),
                )
                for name in names[start : start + 12]
            ]
            last = start + 12 >= len(names)

            async def complete(
                step: LoginStep,
                values: Mapping[str, str],
                final_page: bool = last,
            ) -> LoginSuccess:
                page = {
                    item.name: _input(
                        item.name, props[item.name], values[item.name]
                    )
                    for item in step.fields
                    if values.get(item.name, "") != ""
                }
                if final_page and finish:
                    return LoginSuccess(await finish({**payload, **page}))
                return LoginSuccess(page)

            result = await broker.request(
                service,
                LoginStep(
                    title="API authentication",
                    title_key="authTitle",
                    description=destination,
                    submit_label="Continue",
                    submit_label_key="continue",
                    fields=fields,
                ),
                complete,
            )
            if last and finish:
                return result
            payload.update(result)

    async with httpx.AsyncClient(
        base_url=base.rstrip("/") + "/",
        timeout=15,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        for dep in login["steps"]:
            name = dep["field"]
            field_schema = props.get(name, {})
            missing = list(
                dict.fromkeys(
                    source
                    for source in (
                        *dep["parameters"].values(),
                        *dep.get("filters", {}).values(),
                    )
                    if source not in payload
                )
            )
            if missing:
                await collect(
                    missing,
                    "GET " + base + dep["route"],
                    lookup=True,
                )
            route, query = dep["route"], {}
            for param, source in dep["parameters"].items():
                value = payload[source]
                item = dep["params"][param]
                if not _checked(item["schema"], value):
                    raise LoginFailed(
                        "A dependency returned a value with an unexpected type.",
                        code="lookupType",
                    )
                if item["in"] == "path":
                    segment = str(value)
                    if (
                        not segment
                        or segment in (".", "..")
                        or any(c in segment for c in "/\\%")
                    ):
                        raise LoginFailed(
                            "A dependency returned an invalid path parameter.",
                            code="lookupPath",
                        )
                    route = route.replace(
                        "{" + param + "}", quote(segment, safe="")
                    )
                else:
                    query[param] = value
            try:
                response = await client.get(route.lstrip("/"), params=query)
                response.raise_for_status()
                rows = _at(response.json(), dep["rows"])
                if rows is None:
                    rows = []
                elif not isinstance(rows, list):
                    rows = [rows]
                filters = dep.get("filters", {})
                if filters:
                    rows = [
                        row
                        for row in rows
                        if _matches_lookup(row, filters, payload)
                    ]
                options = []
                for index, row in enumerate(rows):
                    value = _at(row, dep["value"])
                    if not _checked(field_schema, value):
                        raise ValueError
                    label = (
                        _at(row, dep["label"])
                        if dep["label"] is not None
                        else value
                    )
                    if len(rows) > 1 and (
                        not isinstance(label, (str, float, int))
                        or not str(label)
                    ):
                        raise ValueError
                    options.append(
                        LoginOption(value=str(index), label=str(label))
                    )
            except (
                httpx.HTTPError,
                ValueError,
                IndexError,
                KeyError,
                TypeError,
            ):
                raise LoginFailed(
                    f"Could not load options for {name} from {dep['route']}.",
                    code="lookupFailed",
                    params={"route": dep["route"], "field": name},
                ) from None
            if not options:
                raise LoginFailed(
                    f"{dep['route']} returned no options for {name}.",
                    code="lookupEmpty",
                    params={"route": dep["route"], "field": name},
                )
            index = 0
            if len(options) > 1:

                async def choose(
                    step: LoginStep, values: Mapping[str, str]
                ) -> LoginSuccess:
                    return LoginSuccess(int(values[step.fields[0].name]))

                index = await broker.request(
                    service,
                    LoginStep(
                        title="Select an authentication value",
                        title_key="chooseTitle",
                        description=base + route,
                        submit_label="Continue",
                        submit_label_key="continue",
                        fields=[
                            LoginField(
                                name=name,
                                label=field_schema.get("title") or name,
                                options=options,
                            )
                        ],
                    ),
                    choose,
                )
            payload[name] = _at(rows[index], dep["value"])

        expired = [
            ResponseMatch.model_validate(rule)
            for rule in challenge.get("expired", [])
        ]
        triggers = [
            ResponseMatch.model_validate(rule)
            for rule in challenge.get("when", [])
        ]
        needs_code = bool(challenge) and not triggers

        async def exchange(body: dict) -> tuple[str, tuple | None] | None:
            body = {key: value for key, value in body.items() if key in props}
            if not _checked(schema, body):
                raise LoginRejected(
                    "The authentication inputs do not match the interface definition.",
                    code="invalidInputs",
                )
            try:
                response = await client.post(
                    login["route"].lstrip("/"), json=body
                )
                if not needs_code and any(
                    rule.matches(response) for rule in triggers
                ):
                    payload.update(body)
                    return None
                if needs_code and any(
                    rule.matches(response) for rule in expired
                ):
                    return None
                if response.status_code in (400, 401, 403, 422):
                    raise LoginRejected(
                        "The service rejected these authentication inputs. Check them and try again.",
                        code="rejected",
                    )
                response.raise_for_status()
                try:
                    value = _at(response.json(), login["credential"])
                except (IndexError, TypeError, KeyError):
                    value = None
                if not isinstance(value, str) or not value:
                    raise LoginRejected(
                        "The service did not issue a credential. Check your authentication inputs.",
                        code="missingCredential",
                    )
                header = _header_value(value, mode)
                check = login.get("identity")
                if not check:
                    return header, None
                identity_response = await client.get(
                    check["route"].lstrip("/"),
                    headers={profile.get("header", "Authorization"): header},
                )
                identity_response.raise_for_status()
                info = identity_response.json()
                try:
                    identity = []
                    for name, pointer in sorted(check["fields"].items()):
                        actual = _at(info, pointer)
                        if actual is None or not _same_value(
                            actual, body[name]
                        ):
                            raise ValueError
                        identity.append(json.dumps(actual, sort_keys=True))
                except (KeyError, IndexError, TypeError, ValueError):
                    raise LoginRejected("登录身份与所选条件不一致。") from None
                return header, tuple(identity)
            except (httpx.HTTPError, ValueError):
                raise LoginFailed(
                    "The authentication response does not match the interface definition.",
                    code="unexpectedResponse",
                ) from None

        async def issue_code(
            _step: LoginStep, _values: Mapping[str, str]
        ) -> LoginSuccess:
            route = challenge["route"]
            arguments = {
                key: payload[source]
                for key, source in challenge["parameters"].items()
            }
            body_schema = challenge["body_schema"]
            if body_schema is not None and not _checked(
                body_schema, arguments
            ):
                raise LoginFailed("验证码请求参数与接口定义不一致。")
            try:
                response = await client.post(
                    route.lstrip("/"),
                    json=arguments if body_schema is not None else None,
                )
                response.raise_for_status()
                if success := challenge.get("success"):
                    if not ResponseMatch.model_validate(success).matches(
                        response
                    ):
                        raise ValueError
                for name, pointer in challenge["bindings"].items():
                    value = _at(response.json(), pointer)
                    if value is None or not _checked(props[name], value):
                        raise ValueError
                    payload[name] = value
                return LoginSuccess(None)
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ):
                raise LoginFailed(
                    "获取验证码挑战失败，请重新发起登录。"
                ) from None

        renewed = False
        while True:
            if needs_code and (route := challenge.get("route")):
                missing = list(
                    dict.fromkeys(
                        name
                        for name in challenge["parameters"].values()
                        if name not in payload
                    )
                )
                if missing:
                    await collect(
                        missing, "POST " + base + route, required=True
                    )

                await broker.request(
                    service,
                    LoginStep(
                        title="Request a new code"
                        if renewed
                        else "Request a verification code",
                        title_key="renewCode" if renewed else "requestCode",
                        description=base + route,
                        submit_label="Request code",
                        submit_label_key="requestCode",
                        fields=[],
                    ),
                    issue_code,
                )
            names = [
                name
                for name, prop in props.items()
                if name not in payload
                and not prop.get("readOnly")
                and name != challenge.get("answer")
                and name not in challenge.get("bindings", {})
            ]
            if needs_code:
                names.append(challenge["answer"])
            result = await collect(
                names, "POST " + base + login["route"], finish=exchange
            )
            if result is not None:
                return result
            renewed = needs_code
            needs_code = True


def _header_value(value: str, mode: str) -> str:
    if (
        not value.isascii()
        or not value.isprintable()
        or value != value.strip()
        or not value
    ):
        raise LoginRejected(
            "The service credential format is invalid.",
            code="invalidCredential",
        )
    return "Bearer " + value if mode == "bearer" else value


@dataclass(frozen=True)
class _Credential:
    header: str = field(repr=False)
    identity: tuple | None = field(default=None, repr=False)
    generation: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class _Challenge:
    task: asyncio.Task[_Credential]
    waiters: int = 0


class EnterpriseLogin:
    """Keep issued credentials in memory only, scoped to the chat and contract."""

    def __init__(
        self,
        broker: ToolLogin,
        *,
        adapters: Mapping[str, EnterpriseLoginAdapter] | None = None,
        max_cached_credentials: int = EnterpriseLimits.max_cached_credentials,
    ) -> None:
        self.broker = broker
        self._adapters = dict(adapters or {})
        self._max_cached_credentials = max_cached_credentials
        self._headers: OrderedDict[tuple, _Credential] = OrderedDict()
        self._pending: dict[tuple, _Challenge] = {}

    def credential_generation(self, service: str) -> str | None:
        """Return the in-memory credential generation for this chat, if any."""
        try:
            key = (*self.broker.current_scope(), service)
        except LoginEnded:
            return None
        credential = self._headers.get(key)
        return credential.generation if credential is not None else None

    async def _issue(
        self,
        key: tuple,
        base: str,
        service: str,
        profile: dict,
        adapter: EnterpriseLoginAdapter | None,
    ) -> _Credential:
        if adapter is None:
            header, identity = await _login(
                self.broker, service, base, profile
            )
        else:
            value, identity = await adapter.login(self.broker, service, base)
            header = _header_value(value, profile["mode"])
        credential = _Credential(header, identity)
        self._headers[key] = credential
        if len(self._headers) > self._max_cached_credentials:
            self._headers.popitem(last=False)
        return credential

    async def _obtain(
        self,
        key: tuple,
        base: str,
        service: str,
        profile: dict,
        adapter: EnterpriseLoginAdapter | None,
    ) -> _Credential:
        challenge = self._pending.get(key)
        if challenge is None or challenge.task.done():
            challenge = _Challenge(
                asyncio.create_task(
                    self._issue(key, base, service, profile, adapter)
                )
            )
            self._pending[key] = challenge
        challenge.waiters += 1
        try:
            return await asyncio.shield(challenge.task)
        finally:
            challenge.waiters -= 1
            if not challenge.waiters:
                if self._pending.get(key) is challenge:
                    self._pending.pop(key)
                if not challenge.task.done():
                    challenge.task.cancel()
                    await asyncio.gather(
                        challenge.task, return_exceptions=True
                    )

    def auth(
        self,
        base: str,
        service: str,
        profile: dict,
        *,
        adapter: str | None = None,
    ) -> httpx.Auth:
        validate_api_base_url(base, authenticated=True)
        selected_adapter = self._adapters.get(adapter) if adapter else None
        if adapter and (
            selected_adapter is None
            or profile["mode"] not in ("header", "bearer")
        ):
            raise ValueError(
                "Unknown or incompatible enterprise login adapter"
            )
        if profile.get("client") and not selected_adapter:
            raise ValueError(
                "This saved login requires a configured host adapter"
            )
        expiry = (
            selected_adapter.expired
            if selected_adapter
            else tuple(
                ResponseMatch.model_validate(rule)
                for rule in profile.get("login", {}).get("expired", [])
            )
        )
        manager = self
        base = base.rstrip("/")
        origin = httpx.URL(base)
        name = profile.get("header", "Authorization")

        class ScopedReadAuth(httpx.Auth):
            async def async_auth_flow(
                self, request: httpx.Request
            ) -> AsyncGenerator[httpx.Request, httpx.Response]:
                url = request.url
                if (
                    request.method != "GET"
                    or (url.scheme, url.host, url.port)
                    != (
                        origin.scheme,
                        origin.host,
                        origin.port,
                    )
                    or not url.path.startswith(origin.path.rstrip("/") + "/")
                ):
                    raise ValueError("认证凭据仅用于已绑定服务的查询。")
                key = (*manager.broker.current_scope(), service)
                pinned = _pinned_generations.get()
                expired_identity = None
                for attempt in range(2):
                    credential = manager._headers.get(key)
                    if (
                        pinned is not None
                        and service in pinned
                        and (
                            credential is None
                            or credential.generation != pinned[service]
                        )
                    ):
                        raise EnterpriseLoginChanged
                    if credential is None:
                        credential = await manager._obtain(
                            key, base, service, profile, selected_adapter
                        )
                    if (
                        expired_identity is not None
                        and credential.identity != expired_identity
                    ):
                        raise LoginFailed(
                            "重新登录后的身份已改变，请重新发起查询。"
                        )
                    if manager._headers.get(key) is credential:
                        manager._headers.move_to_end(key)
                    elif pinned is not None:
                        raise EnterpriseLoginChanged
                    if pinned is not None:
                        pinned[service] = credential.generation
                    request.headers[name] = credential.header
                    response = yield request
                    expired = response.status_code == 401
                    if not expired and expiry:
                        await response.aread()
                        expired = any(
                            rule.matches(response) for rule in expiry
                        )
                    if not expired:
                        break
                    expired_identity = credential.identity
                    if manager._headers.get(key) is credential:
                        manager._headers.pop(key, None)
                    if pinned is not None:
                        # Later requests can contain values from earlier reads.
                        raise EnterpriseLoginChanged
                    if not attempt:
                        await response.aread()

        return ScopedReadAuth()
