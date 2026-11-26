"""Regressions for the enterprise ERP client login and identity checks."""

import asyncio
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from examples.business_tools.purchase_receipt.login import login_adapter

from agentscope.app._enterprise_login import EnterpriseLogin
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.tool_login import (
    LoginFailed,
    LoginRejected,
    ToolLogin,
    tool_login_scope,
)
from agentscope.business._enterprise_auth import (
    compile_authentication,
)


def contract():
    return {
        "info": {"title": "元境建材ERP系统API文档"},
        "security": [{"JWT": []}],
        "components": {
            "securitySchemes": {"JWT": {"type": "http", "scheme": "bearer"}}
        },
        "paths": {
            "/auth/login": {
                "post": {
                    "security": [],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": [
                                        "tenantId",
                                        "username",
                                        "password",
                                    ],
                                    "properties": {
                                        "region": {"type": "array"},
                                        "tenantId": {"type": "integer"},
                                        "username": {"type": "string"},
                                        "password": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                }
            },
            "/auth/tenants": {"get": {"security": []}},
            "/auth/login-accounts": {"get": {"security": []}},
            "/auth/info": {"get": {}},
            "/purchase/order/page": {"get": {}},
        },
    }


class YuanjingAuthenticationTest(IsolatedAsyncioTestCase):
    def test_document_title_does_not_select_an_adapter(self):
        doc = contract()
        before = compile_authentication(doc, {"security": [{"JWT": []}]}, None)
        self.assertEqual(before, {"mode": "bearer", "scheme": "JWT"})
        doc["info"]["title"] = "Unrelated application"
        self.assertEqual(
            compile_authentication(doc, {"security": [{"JWT": []}]}, None),
            before,
        )
        self.assertEqual(
            compile_authentication(doc, {"security": []}, None),
            {"mode": "public"},
        )

    async def exercise_login(self, change_identity):
        doc = contract()
        profile = compile_authentication(
            doc, doc["paths"]["/purchase/order/page"]["get"], None
        )
        broker = ToolLogin(InMemoryMessageBus(), timeout=4)
        scope = ("owner", "purchase", "chat")
        manager = EnterpriseLogin(
            broker, adapters={"customer-a": login_adapter}
        )
        orders = []
        identities = []
        usernames = ["alice", "bob" if change_identity else "alice"]

        def erp(request):
            path = request.url.path
            if path.endswith("/tenants"):
                data = [
                    {"id": 91, "tenantName": "南方", "province": "广东省"},
                    {"id": 31, "tenantName": "北方 A", "province": "11"},
                    {"id": 32, "tenantName": "北方 B", "province": "北京市"},
                ]
            elif path.endswith("/login-accounts"):
                self.assertEqual(request.url.params["tenantId"], "32")
                data = [{"username": "listed"}]
            elif path.endswith("/auth/login"):
                body = json.loads(request.content)
                self.assertEqual(body["region"], ["110000"])
                self.assertEqual(body["tenantId"], 32)
                identities.append(body["username"])
                data = {"token": "test-" + str(len(identities))}
            elif path.endswith("/auth/info"):
                self.assertEqual(
                    request.headers["authorization"],
                    "Bearer test-" + str(len(identities)),
                )
                data = {
                    "tenantId": 99 if len(identities) == 1 else 32,
                    "username": identities[-1],
                }
            else:
                self.assertEqual(path, "/api/purchase/order/page")
                orders.append(request.headers["authorization"])
                if len(orders) == 1:
                    return httpx.Response(200, json={"code": 401})
                data = []
            return httpx.Response(200, json={"code": 200, "data": data})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(erp)

        async def form(field):
            async with asyncio.timeout(2):
                while (
                    not (view := broker.pending(scope))
                    or view.step.fields[0].name != field
                ):
                    await asyncio.sleep(0.005)
                return view

        async def submit(view, **values):
            return await broker.submit(scope, view.id, view.revision, values)

        with (
            tool_login_scope(*scope),
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kw: real_client(
                    transport=transport, **kw
                ),
            ),
        ):
            async with real_client(
                transport=transport,
                auth=manager.auth(
                    "https://erp.test/api",
                    "erp",
                    profile,
                    adapter="customer-a",
                ),
            ) as client:
                read = asyncio.create_task(
                    client.get("https://erp.test/api/purchase/order/page")
                )
                for number, username in enumerate(usernames):
                    view = await form("region")
                    await submit(view, region="110000")
                    async with asyncio.timeout(2):
                        while (
                            next_step := broker.pending(scope)
                        ) is None or next_step.id == view.id:
                            await asyncio.sleep(0.005)
                    await submit(next_step, region="current")
                    view = await form("tenantId")
                    self.assertEqual(
                        [o.label for o in view.step.fields[0].options],
                        ["北方 A", "北方 B"],
                    )
                    await submit(view, tenantId="1")
                    view = await form("username")
                    self.assertEqual(
                        view.step.fields[0].suggestions, ["listed"]
                    )
                    if number == 0:
                        with self.assertRaises(LoginRejected):
                            await submit(
                                view,
                                username=username,
                                password="test",
                                agreement="no",
                            )
                        with self.assertRaises(LoginRejected):
                            await submit(
                                view,
                                username=username,
                                password="test",
                                agreement="yes",
                            )
                        self.assertEqual(orders, [])
                    await submit(
                        view,
                        username=username,
                        password="test",
                        agreement="yes",
                    )
                if change_identity:
                    with self.assertRaises(LoginFailed):
                        await read
                    self.assertEqual(len(orders), 1)
                else:
                    self.assertEqual((await read).json()["code"], 200)
                    self.assertEqual(
                        orders, ["Bearer test-2", "Bearer test-3"]
                    )

    async def test_login_and_body_expiration(self):
        await self.exercise_login(False)

    async def test_changed_identity_stops_original_query(self):
        await self.exercise_login(True)
