"""Private login for the Yuanjing ERP client contract."""

import json
import re
from functools import lru_cache
from pathlib import Path

import httpx

from agentscope.app import EnterpriseLoginAdapter, ResponseMatch
from agentscope.app.tool_login import (
    LoginFailed,
    LoginField,
    LoginOption,
    LoginRejected,
    LoginStep,
    LoginSuccess,
    ToolLogin,
)


@lru_cache(maxsize=1)
def _regions() -> dict:
    """Province, city and district codes and labels from ERP regionData."""
    root = {}
    parents = [root]
    data = Path(__file__).with_name("login_data") / "regions.tsv"
    for line in data.read_text(encoding="utf-8").splitlines():
        depth, code, label = line.split("\t")
        children = {}
        parents[int(depth)][code] = (label, children)
        parents[int(depth) + 1 :] = [children]
    return root


def _region_matches(value, code, label, level):
    raw = str(value or "").strip()
    if not raw:
        return False
    suffix = r"(特别行政区|自治区|自治州|地区|盟|省|市|区|县)$"
    if re.sub(suffix, "", "".join(raw.split())) == re.sub(suffix, "", label):
        return True
    digits = re.sub(r"\D", "", raw)
    if level == 2:
        return digits == code or (
            len(code) == 6
            and (digits[-4:] if len(digits) == 6 else digits) == code[-4:]
        )
    width = (2, 4)[level]
    return digits[:width] == code[:width]


async def _choose(broker, service, name, label, options, description):
    async def select(_step, values):
        return LoginSuccess(values[name])

    return await broker.request(
        service,
        LoginStep(
            title=label,
            description=description,
            submit_label="继续",
            fields=[LoginField(name=name, label=label, options=options)],
        ),
        select,
    )


async def _select_tenant(broker, service, tenants, base):
    keys = ("province", "city", "district")
    regional = any(any(t.get(k) for k in keys) for t in tenants)
    description = base
    while True:
        nodes = _regions()
        selected = []
        candidates = tenants
        for level, label in enumerate(("省级地区", "市级地区", "区县")):
            if not nodes:
                break
            options = [
                LoginOption(value=k, label=v[0]) for k, v in nodes.items()
            ]
            if selected:
                options.insert(
                    0, LoginOption(value="current", label="使用当前地区")
                )
            code = await _choose(
                broker, service, "region", label, options, description
            )
            if code == "current":
                break
            name, nodes = nodes[code]
            selected.append(code)
            if regional:
                candidates = [
                    t
                    for t in candidates
                    if _region_matches(t.get(keys[level]), code, name, level)
                ]
            if not candidates:
                break
        if candidates:
            break
        description = "该地区没有可用账套，请重新选择地区。"
    if len(candidates) == 1:
        return selected, candidates[0]
    options = [
        LoginOption(
            value=str(i),
            label=t["tenantName"]
            + (f" ({t['tenantCode']})" if t.get("tenantCode") else ""),
        )
        for i, t in enumerate(candidates)
    ]
    choice = await _choose(
        broker, service, "tenantId", "选择账套", options, base
    )
    return selected, candidates[int(choice)]


def _data(response):
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 200:
        raise ValueError("ERP did not accept the request")
    return body["data"]


async def login_yuanjing(
    broker: ToolLogin, service: str, base: str
) -> tuple[str, tuple[int, str]]:
    async with httpx.AsyncClient(
        base_url=base.rstrip("/") + "/",
        timeout=15,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        try:
            tenants = _data(await client.get("auth/tenants"))
            if (
                not isinstance(tenants, list)
                or not tenants
                or not all(
                    isinstance(t, dict)
                    and type(t.get("id")) is int
                    and isinstance(t.get("tenantName"), str)
                    for t in tenants
                )
            ):
                raise ValueError
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LoginFailed("无法读取 ERP 账套。") from exc
        region, tenant = await _select_tenant(broker, service, tenants, base)
        try:
            rows = _data(
                await client.get(
                    "auth/login-accounts", params={"tenantId": tenant["id"]}
                )
            )
            accounts = [
                r["username"]
                for r in rows
                if isinstance(r.get("username"), str)
            ]
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            accounts = []
        agreement = json.loads(
            (
                Path(__file__).with_name("login_data") / "agreement.json"
            ).read_text(encoding="utf-8")
        )

        async def authenticate(_step, values):
            if values.get("agreement") != "yes":
                raise LoginRejected("请先阅读并同意服务协议与隐私政策。")
            username = values["username"]
            try:
                result = _data(
                    await client.post(
                        "auth/login",
                        json={
                            "region": region,
                            "tenantId": tenant["id"],
                            "username": username,
                            "password": values["password"],
                        },
                    )
                )
                token = result["token"]
                if not isinstance(token, str) or not re.fullmatch(
                    r"[!-~]+", token
                ):
                    raise ValueError
                header = "Bearer " + token
                info = _data(
                    await client.get(
                        "auth/info", headers={"Authorization": header}
                    )
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise LoginRejected(
                    "ERP 登录或身份核对失败，请检查账号及密码后重试。"
                ) from exc
            if not isinstance(info, dict) or (
                info.get("username") != username
                or str(info.get("tenantId")) != str(tenant["id"])
            ):
                raise LoginRejected("ERP 返回的账号或账套与所选身份不一致。")
            return LoginSuccess((token, (tenant["id"], username)))

        return await broker.request(
            service,
            LoginStep(
                title="ERP 登录",
                description=f"{tenant['tenantName']} · {base}",
                submit_label="登录",
                fields=[
                    LoginField(
                        name="username", label="用户名", suggestions=accounts
                    ),
                    LoginField(name="password", label="密码", kind="secret"),
                    LoginField(
                        name="agreement",
                        label=agreement["agreementPrompt"],
                        agreement=agreement["agreementDocumentText"],
                    ),
                ],
            ),
            authenticate,
        )


login_adapter = EnterpriseLoginAdapter(
    login=login_yuanjing,
    expired=(ResponseMatch(status=200, fields={"code": 401}),),
)
