# -*- coding: utf-8 -*-
"""Authenticate enterprise reads using the current chat's private login form."""

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator, FormatChecker

from ..business._business import validate_api_base_url
from .tool_login import (
    LoginFailed,
    LoginField,
    LoginOption,
    LoginRejected,
    LoginStep,
    LoginSuccess,
    ToolLogin,
)


def _at(value: Any, pointer: str) -> Any:
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        value = (
            value[int(key)]
            if isinstance(value, list) and key.isdigit()
            else value[key]
        )
    return value


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
        raise LoginRejected(f"请按接口要求填写 {field}。")
    return value


async def _login(
    broker: ToolLogin, service: str, base: str, profile: dict
) -> str:
    login = profile.get("login")
    mode = profile["mode"]
    if not login:
        fields = (
            [
                LoginField(name="username", label="Username"),
                LoginField(name="password", label="Password", kind="secret"),
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

        return await broker.request(
            service,
            LoginStep(
                title="接口认证",
                description=base,
                submit_label="继续",
                fields=fields,
            ),
            direct,
        )

    schema = login["schema"]
    props = schema["properties"]
    payload: dict = {}

    async def collect(
        names: list[str],
        destination: str,
        *,
        lookup: bool = False,
        finish: Callable[[dict], Awaitable[str]] | None = None,
    ) -> Any:
        # Submit the last page to the service inside the private form callback.
        # A rejected login can then be corrected in the same form.
        for start in range(0, max(1, len(names)), 12):
            fields = [
                LoginField(
                    name=name,
                    label=props[name].get("title") or name,
                    kind="text" if lookup else "secret",
                    required=lookup or name in schema.get("required", []),
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
                    title="接口认证",
                    description=destination,
                    submit_label="继续",
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
                    for source in dep["parameters"].values()
                    if source not in payload
                )
            )
            if missing:
                await collect(
                    missing,
                    "查询选项 · GET " + base + dep["route"],
                    lookup=True,
                )
            route, query = dep["route"], {}
            for param, source in dep["parameters"].items():
                value = payload[source]
                item = dep["params"][param]
                if not _checked(item["schema"], value):
                    raise LoginFailed("上游返回值与依赖接口的参数类型不符。")
                if item["in"] == "path":
                    segment = str(value)
                    if (
                        not segment
                        or segment in (".", "..")
                        or any(c in segment for c in "/\\%")
                    ):
                        raise LoginFailed("依赖接口返回了无效的路径参数。")
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
                    f"无法从 {dep['route']} 解析 {name} 的选项。"
                ) from None
            if not options:
                raise LoginFailed(f"{dep['route']} 未返回 {name} 的可选值。")
            index = 0
            if len(options) > 1:

                async def choose(
                    step: LoginStep, values: Mapping[str, str]
                ) -> LoginSuccess:
                    return LoginSuccess(int(values[step.fields[0].name]))

                index = await broker.request(
                    service,
                    LoginStep(
                        title="选择认证参数",
                        description=base + route,
                        submit_label="继续",
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

        async def exchange(body: dict) -> str:
            body = {key: value for key, value in body.items() if key in props}
            if not _checked(schema, body):
                raise LoginRejected("认证参数组合不符合接口定义。")
            try:
                response = await client.post(
                    login["route"].lstrip("/"), json=body
                )
                if response.status_code in (400, 401, 403, 422):
                    raise LoginRejected(
                        "服务未接受这些认证参数，请核对后重试。"
                    )
                response.raise_for_status()
                try:
                    value = _at(response.json(), login["credential"])
                except (IndexError, TypeError, KeyError):
                    value = None
                if not isinstance(value, str) or not value:
                    raise LoginRejected("服务未返回凭据，请核对认证参数。")
                return _header_value(value, mode)
            except (httpx.HTTPError, ValueError):
                raise LoginFailed("认证服务的响应与接口定义不符。") from None

        names = [
            name
            for name, prop in props.items()
            if name not in payload and not prop.get("readOnly")
        ]
        return await collect(
            names, "POST " + base + login["route"], finish=exchange
        )


def _header_value(value: str, mode: str) -> str:
    if (
        not value.isascii()
        or not value.isprintable()
        or value != value.strip()
        or not value
    ):
        raise LoginRejected("服务凭据格式无效。")
    return "Bearer " + value if mode == "bearer" else value


@dataclass
class _Challenge:
    task: asyncio.Task[str]
    waiters: int = 0


class EnterpriseLogin:
    """Keep issued credentials in memory only, scoped to the chat and contract."""

    def __init__(self, broker: ToolLogin) -> None:
        self.broker = broker
        self._headers: OrderedDict[tuple, str] = OrderedDict()
        self._pending: dict[tuple, _Challenge] = {}

    async def _obtain(
        self, key: tuple, base: str, service: str, profile: dict
    ) -> str:
        challenge = self._pending.get(key)
        if challenge is None:
            challenge = _Challenge(
                asyncio.create_task(
                    _login(self.broker, service, base, profile)
                )
            )
            self._pending[key] = challenge
        challenge.waiters += 1
        try:
            value = await asyncio.shield(challenge.task)
            self._headers[key] = value
            if len(self._headers) > 256:
                self._headers.popitem(last=False)
            return value
        finally:
            challenge.waiters -= 1
            if not challenge.waiters:
                self._pending.pop(key, None)
                if not challenge.task.done():
                    challenge.task.cancel()
                    await asyncio.gather(
                        challenge.task, return_exceptions=True
                    )

    def auth(self, base: str, service: str, profile: dict) -> httpx.Auth:
        validate_api_base_url(base, authenticated=True)
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
                for attempt in range(2):
                    header = manager._headers.get(key)
                    if header is None:
                        header = await manager._obtain(
                            key, base, service, profile
                        )
                    manager._headers.move_to_end(key)
                    request.headers[name] = header
                    response = yield request
                    if response.status_code != 401:
                        break
                    if manager._headers.get(key) == header:
                        manager._headers.pop(key, None)
                    if not attempt:
                        await response.aread()

        return ScopedReadAuth()
