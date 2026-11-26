# -*- coding: utf-8 -*-
"""Exercise contract-driven private login through a real chat import."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from agentscope.app import EnterpriseLoginAdapter
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
    inspect_authentication,
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
    async def test_authenticator_code_needs_no_issuance_endpoint(self):
        doc = definition()
        schema = doc["paths"]["/session/start"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]
        schema.update(
            properties={"factor": {"type": "string"}}, required=["factor"]
        )
        profile = compile_authentication(
            doc,
            doc["paths"]["/reports/list"]["get"],
            LoginRecipe(
                route="/session/start",
                credential="/ticket",
                challenge={"answer": "factor"},
            ),
        )
        paths = []

        def respond(request):
            paths.append(request.url.path)
            if request.method == "POST":
                self.assertEqual(
                    json.loads(request.content), {"factor": "000014"}
                )
                return httpx.Response(200, json={"ticket": "valid"})
            return httpx.Response(200)

        broker = ToolLogin(InMemoryMessageBus(), timeout=3)
        scope = ("owner", "agent", "authenticator")
        real = httpx.AsyncClient
        transport = httpx.MockTransport(respond)
        with (
            tool_login_scope(*scope),
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kw: real(transport=transport, **kw),
            ),
        ):
            auth = EnterpriseLogin(broker).auth(
                "https://reports.test", "report", profile
            )
            async with real(transport=transport, auth=auth) as client:
                task = asyncio.create_task(
                    client.get("https://reports.test/reports/list")
                )
                async with asyncio.timeout(2):
                    while not (view := broker.pending(scope)):
                        await asyncio.sleep(0.001)
                self.assertEqual(view.step.fields[0].kind, "one-time-code")
                self.assertEqual(paths, [])
                await broker.submit(
                    scope, view.id, view.revision, {"factor": "000014"}
                )
                await task
                self.assertEqual(paths, ["/session/start", "/reports/list"])

    def test_discover_challenge_fields_before_login(self):
        doc = definition()
        op = doc["paths"]["/session/start"]["post"]
        op["responses"]["403"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"state": {"type": "string"}},
                    },
                    "example": {"state": "private-example"},
                }
            }
        }
        report = inspect_authentication(doc, "/session/start", 0)
        self.assertEqual(
            report["operations"][0]["response_fields"]["403"]["/state"],
            "string",
        )
        self.assertNotIn("private-example", json.dumps(report))

    async def test_conditional_challenge_with_explicit_reissue(self):
        doc = definition()
        start = doc["paths"]["/session/start"]["post"]
        body = start["requestBody"]["content"]["application/json"]["schema"]
        body.update(
            required=["account"],
            properties={
                key: {"type": "string"}
                for key in ("account", "answer", "nonce")
            },
        )
        doc["paths"]["/issue"] = {
            "post": {
                "security": [],
                "responses": start["responses"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "recipient": {"type": "string"}
                                },
                                "required": ["recipient"],
                            }
                        }
                    }
                },
            }
        }
        config = {
            "route": "/session/start",
            "credential": "/ticket",
            "challenge": {
                "route": "/issue",
                "answer": "answer",
                "parameters": {"recipient": "account"},
                "bindings": {"nonce": "/id"},
                "when": [{"fields": {"state": "verify"}}],
                "expired": [{"fields": {"state": "stale"}}],
            },
        }
        profile = compile_authentication(
            doc,
            doc["paths"]["/reports/count"]["get"],
            LoginRecipe.model_validate(config),
        )
        for skip in (True, False):
            calls = []
            issues = 0
            broker = ToolLogin(InMemoryMessageBus(), timeout=5)
            scope = ("owner", "agent", "session")

            def respond(req):
                nonlocal issues
                calls.append(req.url.path)
                if req.method == "GET":
                    self.assertEqual(req.headers["X-Ticket"], "granted")
                    return httpx.Response(200)
                data = json.loads(req.content)
                if req.url.path == "/issue":
                    self.assertEqual(data, {"recipient": "user"})
                    issues += 1
                    return httpx.Response(200, json={"id": str(issues)})
                if "answer" not in data:
                    self.assertEqual(data, {"account": "user"})
                    return httpx.Response(
                        200,
                        json={"ticket": "granted"}
                        if skip
                        else {"state": "verify"},
                    )
                self.assertEqual(
                    data,
                    {
                        "account": "user",
                        "answer": "0012",
                        "nonce": str(issues),
                    },
                )
                return httpx.Response(
                    200,
                    json={"state": "stale"}
                    if issues == 1
                    else {"ticket": "granted"},
                )

            async def wait_form():
                async with asyncio.timeout(3):
                    while not (view := broker.pending(scope)):
                        await asyncio.sleep(0.001)
                return view

            real = httpx.AsyncClient
            transport = httpx.MockTransport(respond)
            with (
                tool_login_scope(*scope),
                patch(
                    "httpx.AsyncClient",
                    side_effect=lambda **kw: real(transport=transport, **kw),
                ),
            ):
                async with real(
                    transport=transport,
                    auth=EnterpriseLogin(broker).auth(
                        "https://example.test", "reports", profile
                    ),
                ) as client:
                    task = asyncio.create_task(
                        client.get("https://example.test/reports/count")
                    )
                    view = await wait_form()
                    self.assertEqual(
                        [f.name for f in view.step.fields], ["account"]
                    )
                    await broker.submit(
                        scope, view.id, view.revision, {"account": "user"}
                    )
                    if not skip:
                        for index in range(2):
                            view = await wait_form()
                            self.assertEqual(
                                view.step.title_key,
                                "renewCode" if index else "requestCode",
                            )
                            self.assertEqual(issues, index)
                            self.assertNotIn("/reports/count", calls)
                            await broker.submit(
                                scope, view.id, view.revision, {}
                            )
                            view = await wait_form()
                            self.assertEqual(
                                [(f.name, f.kind) for f in view.step.fields],
                                [("answer", "one-time-code")],
                            )
                            await broker.submit(
                                scope,
                                view.id,
                                view.revision,
                                {"answer": "0012"},
                            )
                    self.assertEqual((await task).status_code, 200)
                    self.assertEqual(issues, 0 if skip else 2)
                    self.assertEqual(calls[-1], "/reports/count")
        config["challenge"]["parameters"]["recipient"] = "answer"
        with self.assertRaisesRegex(ValueError, "dependencies"):
            compile_authentication(
                doc,
                doc["paths"]["/reports/count"]["get"],
                LoginRecipe.model_validate(config),
            )

    async def test_host_adapter_for_verification_and_registered_query(self):
        source = json.dumps(definition())
        base = "https://reports.test"
        issued = []
        reads = []
        broker = ToolLogin(InMemoryMessageBus())
        real_client = httpx.AsyncClient

        async def issue(login_broker, service, url):
            self.assertIs(login_broker, broker)
            self.assertEqual(url, base)
            issued.append(service)
            return "report-ticket", ("station-3",)

        def respond(request):
            reads.append((request.url.path, request.headers["X-Ticket"]))
            return httpx.Response(200, json={"count": 1})

        with (
            TemporaryDirectory() as tmp,
            tool_login_scope("owner", "agent", "chat"),
            patch(
                "httpx.AsyncClient",
                side_effect=lambda **kw: real_client(
                    transport=httpx.MockTransport(respond), **kw
                ),
            ),
        ):
            root = Path(tmp)
            spec = root / "reports.json"
            spec.write_text(source)
            samples = {"/reports/count": {}}
            api = EnterpriseReadAPI(
                spec, base, samples, login_adapter="reports"
            )
            self.assertNotEqual(
                api.key, EnterpriseReadAPI(spec, base, samples).key
            )
            converter = EnterpriseBusiness(
                root / "imports", read_apis={"owner": [api]}
            )
            converter.login = EnterpriseLogin(
                broker, adapters={"reports": EnterpriseLoginAdapter(issue)}
            )
            result = await converter.convert(
                "owner", "agent", source, business_scope="/reports/count"
            )
            self.assertEqual(result["tools"][0]["status"], "registered")
            (tool,) = await converter.tools("owner", "agent")
            await tool.call()
            self.assertEqual(len(issued), 1)
            self.assertEqual(reads, [("/reports/count", "report-ticket")] * 2)

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

    async def test_filtered_lookup_identity_and_custom_expiration(self):
        # A project with a header API key, local row filtering, and no ERP conventions.
        doc = definition()
        project = recipe()
        project["lookups"]["station"].update(
            parameters={}, filters={"/group": "cluster"}
        )
        doc["paths"]["/stations"]["get"]["parameters"] = []
        doc["paths"]["/whoami"] = doc["paths"]["/reports/count"]
        project["identity"] = {
            "route": "/whoami",
            "fields": {"station": "/current"},
        }
        project["expired"] = [
            {"status": 200, "fields": {"error.name": "ticket-ended"}}
        ]
        profile = compile_authentication(
            doc,
            doc["paths"]["/reports/count"]["get"],
            LoginRecipe.model_validate(project),
        )
        broker = ToolLogin(InMemoryMessageBus(), timeout=3)
        manager = EnterpriseLogin(broker)
        scope = ("owner", "agent", "station-chat")
        read_headers = []
        confirmed = False

        def upstream(request):
            path = request.url.path
            if path == "/regions":
                data = [{"id": 7, "label": "North"}]
            elif path == "/stations":
                data = {
                    "items": [
                        {"group": 8, "key": "other"},
                        {"group": 7, "key": "mine"},
                    ]
                }
            elif path == "/session/start":
                self.assertEqual(
                    json.loads(request.content)["station"], "mine"
                )
                data = {"session": {"key": "new" if read_headers else "old"}}
            elif path == "/whoami":
                self.assertIn("X-Ticket", request.headers)
                data = {"current": "mine" if confirmed else "other"}
            else:
                read_headers.append(request.headers["X-Ticket"])
                data = (
                    {"error": {"name": "ticket-ended"}}
                    if len(read_headers) == 1
                    else {"count": 1}
                )
            return httpx.Response(200, json=data)

        transport = httpx.MockTransport(upstream)
        real_client = httpx.AsyncClient
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
                auth=manager.auth("https://station.test", "station", profile),
            ) as client:
                task = asyncio.create_task(
                    client.get("https://station.test/reports/count")
                )
                previous = None
                for attempt in range(2):
                    async with asyncio.timeout(2):
                        while (
                            not (view := broker.pending(scope))
                            or view.id == previous
                        ):
                            await asyncio.sleep(0.005)
                    previous = view.id
                    self.assertEqual(view.step.fields[0].name, "pin")
                    if not attempt:
                        with self.assertRaises(LoginRejected):
                            await broker.submit(
                                scope,
                                view.id,
                                view.revision,
                                {"pin": "fixture"},
                            )
                        self.assertFalse(read_headers)
                        confirmed = True
                    await broker.submit(
                        scope, view.id, view.revision, {"pin": "fixture"}
                    )
                self.assertEqual((await task).json(), {"count": 1})
                self.assertEqual(read_headers, ["old", "new"])

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
