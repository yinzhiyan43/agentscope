# -*- coding: utf-8 -*-
"""Exercise contract-driven private login through a real chat import."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from agentscope.app._enterprise_login import EnterpriseLogin
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.tool_login import (
    LoginRejected,
    ToolLogin,
    tool_login_scope,
)
from agentscope.business._enterprise import (
    EnterpriseBusiness,
    EnterpriseReadAPI,
)
from agentscope.business._enterprise_auth import (
    LoginRecipe,
    compile_authentication,
)


def definition():
    response = {"200": {"content": {"application/json": {"schema": {}}}}}
    body = {
        "type": "object",
        "required": ["cluster", "station", "pin"],
        "properties": {
            "cluster": {"type": "integer"},
            "station": {"type": "string"},
            "pin": {"type": "string", "writeOnly": True},
            "nonce": {"type": "string"},
        },
    }
    param = {
        "in": "query",
        "name": "group",
        "required": True,
        "schema": {"type": "integer"},
    }
    return {
        "openapi": "3.1.0",
        "security": [{"Ticket": []}],
        "components": {
            "securitySchemes": {
                "Ticket": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Ticket",
                }
            }
        },
        "paths": {
            "/session/start": {
                "post": {
                    "security": [],
                    "requestBody": {
                        "content": {"application/json": {"schema": body}}
                    },
                    "responses": response,
                }
            },
            "/regions": {"get": {"security": [], "responses": response}},
            "/stations": {
                "get": {
                    "security": [],
                    "parameters": [param],
                    "responses": response,
                }
            },
            "/reports/count": {"get": {"responses": response}},
            "/reports/list": {"get": {"responses": response}},
        },
    }


def recipe():
    return {
        "route": "/session/start",
        "credential": "/session/key",
        "lookups": {
            "station": {
                "route": "/stations",
                "parameters": {"group": "cluster"},
                "rows": "/items",
                "value": "/key",
            },
            "cluster": {
                "route": "/regions",
                "value": "/id",
                "label": "/label",
            },
        },
    }


class EnterpriseAuthenticationTest(IsolatedAsyncioTestCase):
    async def test_static_header_key_requires_no_chat_login(self) -> None:
        source = json.dumps(definition())
        requests = []
        client = httpx.AsyncClient

        def respond(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers["X-Ticket"], "private-configured-key"
            )
            self.assertNotIn("Authorization", request.headers)
            requests.append(request.url.path)
            return httpx.Response(200, json={"count": 2})

        with (
            TemporaryDirectory() as tmp,
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: client(
                    transport=httpx.MockTransport(respond), **kwargs
                ),
            ),
        ):
            root = Path(tmp)
            spec = root / "spec.json"
            spec.write_text(source)
            api = EnterpriseReadAPI(
                spec,
                "https://service.test",
                {"/reports/count": {}},
                token="private-configured-key",
            )
            converter = EnterpriseBusiness(
                root / "imports", read_apis={"owner": [api]}
            )
            result = await converter.convert(
                "owner", "agent", source, business_scope="/reports/count"
            )
            self.assertEqual(result["tools"][0]["status"], "registered")
            (tool,) = await converter.tools("owner", "agent")
            await tool.call()
            self.assertEqual(requests, ["/reports/count", "/reports/count"])
            (receipt,) = (root / "imports").glob("*/*.json")
            self.assertNotIn("private-configured-key", receipt.read_text())

    async def test_customer_form_and_chained_dependencies(self):
        doc = definition()
        # The first lookup exists only to feed the next lookup.
        body = doc["paths"]["/session/start"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]
        del body["properties"]["cluster"]
        body["required"].remove("cluster")
        body["additionalProperties"] = False
        credentials = []
        requests = []
        broker = ToolLogin(InMemoryMessageBus(), timeout=5)
        scope = ("owner", "agent", "chat")
        base = "https://service.test/v3"
        real = httpx.AsyncClient

        def upstream(request):
            path = request.url.path.removeprefix("/v3")
            requests.append(path)
            if path == "/regions":
                self.assertNotIn("x-ticket", request.headers)
                return httpx.Response(
                    200,
                    json=[
                        {"id": 3, "label": "North"},
                        {"id": 7, "label": "South"},
                    ],
                )
            if path == "/stations":
                self.assertEqual(dict(request.url.params), {"group": "7"})
                return httpx.Response(200, json={"items": [{"key": "S1"}]})
            if path == "/session/start":
                data = json.loads(request.content)
                if data["pin"] == "incorrect-pin":
                    return httpx.Response(
                        401, json={"message": "Invalid credentials"}
                    )
                self.assertEqual(
                    data,
                    {"station": "S1", "pin": "private-input"},
                )
                credentials.append("private-ticket")
                return httpx.Response(
                    200, json={"session": {"key": credentials[-1]}}
                )
            self.assertIn(path, ("/reports/count", "/reports/list"))
            self.assertEqual(request.headers["x-ticket"], "private-ticket")
            return httpx.Response(200, json={"count": 2})

        async def wait_form(previous=None):
            async with asyncio.timeout(4):
                while True:
                    view = broker.pending(scope)
                    if view and view.id != previous:
                        return view
                    await asyncio.sleep(0.005)

        with (
            TemporaryDirectory() as tmp,
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: real(
                    transport=httpx.MockTransport(upstream), **kwargs
                ),
            ),
        ):
            root = Path(tmp)
            spec = root / "spec.json"
            source = json.dumps(doc)
            spec.write_text(source)
            converter = EnterpriseBusiness(
                root / "imports",
                read_apis={
                    "owner": [
                        EnterpriseReadAPI(
                            spec,
                            base,
                            {"/reports/count": {}, "/reports/list": {}},
                        )
                    ]
                },
            )
            converter.login = EnterpriseLogin(broker)
            # Outside a chat an import stays pending without prompting or calling the API.
            result = await converter.convert(
                "owner",
                "agent",
                source,
                business_scope="/reports",
                authentication=recipe(),
            )
            self.assertEqual(
                {r["reason"] for r in result["tools"]},
                {"authentication_required"},
            )
            self.assertEqual(requests, [])
            chat_tools = {
                t.name: t for t in converter.chat_tools("owner", "agent", None)
            }
            inspected = await chat_tools[
                "inspect_enterprise_authentication"
            ].call(
                source=source,
                route="/session/start",
            )
            (login,) = json.loads(inspected.content[0].text)["operations"]
            self.assertEqual(
                {f["name"] for f in login["fields"] if f["required"]},
                {"station", "pin"},
            )
            register = chat_tools["register_enterprise_business"]
            with tool_login_scope(*scope):
                run = asyncio.create_task(
                    register.call(
                        source=source,
                        business_scope="/reports",
                        authentication=recipe(),
                    )
                )
                choice = await wait_form()
                self.assertEqual(choice.step.title_key, "chooseTitle")
                self.assertEqual(choice.step.submit_label_key, "continue")
                (field,) = choice.step.fields
                self.assertEqual(
                    (field.name, [o.label for o in field.options]),
                    ("cluster", ["North", "South"]),
                )
                with self.assertRaises(LoginRejected):
                    await broker.submit(scope, choice.id, 0, {"cluster": "7"})
                await broker.submit(scope, choice.id, 0, {"cluster": "1"})
                form = await wait_form(choice.id)
                self.assertEqual(
                    [(f.name, f.required) for f in form.step.fields],
                    [("pin", True), ("nonce", False)],
                )
                with self.assertRaises(LoginRejected) as rejected:
                    await broker.submit(
                        scope, form.id, form.revision, {"pin": "incorrect-pin"}
                    )
                self.assertEqual(rejected.exception.code, "rejected")
                self.assertFalse(run.done())
                self.assertEqual(await broker.pending_view(scope), form)
                await broker.submit(
                    scope, form.id, 0, {"pin": "private-input"}
                )
                receipt = json.loads((await run).content[0].text)
                self.assertEqual(receipt["registered"], 2)
                tools = await converter.tools("owner", "agent")
                await tools[0].call()
                self.assertEqual(requests.count("/regions"), 1)
                self.assertEqual(requests.count("/stations"), 1)
                self.assertEqual(len(credentials), 1)

            stored = next((root / "imports").glob("*/*.json")).read_text()
            for secret in ("private-input", "private-ticket"):
                self.assertNotIn(secret, stored + str(receipt))
            self.assertEqual(
                await converter.tools("owner", "another-agent"), []
            )
            with tool_login_scope("owner", "agent", "other"):
                pending = asyncio.create_task(tools[0].call())
                async with asyncio.timeout(3):
                    while not (
                        view := broker.pending(("owner", "agent", "other"))
                    ):
                        await asyncio.sleep(0.005)
                self.assertEqual(view.step.fields[0].name, "cluster")
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending

    def test_contract_overrides_and_invalid_dependencies(self):
        doc = definition()
        operation = doc["paths"]["/reports/count"]["get"]
        login = LoginRecipe.model_validate(recipe())
        compiled = compile_authentication(doc, operation, login)
        self.assertEqual(compiled["header"], "X-Ticket")
        self.assertEqual(
            compile_authentication(doc, {"security": []}, login),
            {"mode": "public"},
        )
        for source in ("station", "pin", "unknown"):
            invalid = login.model_copy(deep=True)
            invalid.lookups["station"].parameters["group"] = source
            with self.assertRaises(ValueError):
                compile_authentication(doc, operation, invalid)
        doc["paths"]["/regions"]["get"]["security"] = [{"Ticket": []}]
        with self.assertRaises(ValueError):
            compile_authentication(doc, operation, login)
        with self.assertRaises(ValueError):
            compile_authentication(
                doc, {"security": [{"A": [], "B": []}]}, None
            )

    async def test_expired_direct_bearer_resumes_same_read(self):
        broker = ToolLogin(InMemoryMessageBus(), timeout=3)
        owner = ("owner", "agent", "session")
        sent = []

        def upstream(request):
            sent.append((str(request.url), request.headers["authorization"]))
            return httpx.Response(401 if len(sent) == 1 else 200)

        auth = EnterpriseLogin(broker).auth(
            "https://test.example/api",
            "issuer",
            {"mode": "bearer", "scheme": "Access"},
        )
        url = "https://test.example/api/items?limit=2"
        with tool_login_scope(*owner):
            async with httpx.AsyncClient(
                auth=auth, transport=httpx.MockTransport(upstream)
            ) as client:
                request = asyncio.create_task(client.get(url))
                previous = None
                for value in ("expired", "renewed"):
                    async with asyncio.timeout(2):
                        while (
                            not (view := broker.pending(owner))
                            or view.id == previous
                        ):
                            await asyncio.sleep(0.005)
                    previous = view.id
                    await broker.submit(
                        owner, view.id, 0, {"credential": value}
                    )
                self.assertEqual((await request).status_code, 200)
                self.assertEqual(
                    sent, [(url, "Bearer expired"), (url, "Bearer renewed")]
                )
