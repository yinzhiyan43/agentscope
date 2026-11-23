# -*- coding: utf-8 -*-
"""Exercise enterprise business import, verification, and Agent scoping."""
from typing import Any
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from agentscope.business._enterprise import (
    EnterpriseBusiness,
    EnterpriseReadAPI,
)


class EnterpriseBusinessTest(IsolatedAsyncioTestCase):
    """An import is usable on its own and runs only approved read probes."""

    async def test_chat_inventory_progress_across_scopes_and_restarts(
        self,
    ) -> None:
        paths = {
            f"/jobs/{i}": {
                "get": {
                    "summary": f"Action {i}",
                    "tags": ["Work"],
                    "responses": {"204": {}},
                }
            }
            for i in range(23)
        }
        source = json.dumps({"openapi": "3.1.0", "paths": paths})
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = root / "spec.json"
            spec.write_text(source)
            config = {
                "owner": [
                    EnterpriseReadAPI(
                        spec, "https://example.test", {"/jobs/2": {}}
                    )
                ]
            }
            storage = root / "imports"
            converter = EnterpriseBusiness(storage, read_apis=config)
            chat = {
                t.name: t for t in converter.chat_tools("owner", "agent", None)
            }

            async def call(name, **kwargs):
                return json.loads(
                    (await chat[name].call(**kwargs)).content[0].text
                )

            with patch(
                "httpx.AsyncClient",
                side_effect=AssertionError("Discovery made an HTTP request"),
            ):
                first = await call(
                    "discover_enterprise_actions",
                    source=source,
                    business_scope="Work",
                )
                self.assertEqual(first["counts"], {"discovered": 23})
                self.assertEqual(first["next_offset"], len(first["actions"]))
                inventory_id = first["inventory_id"]
                rest = await call(
                    "get_enterprise_progress",
                    inventory_id=inventory_id,
                    offset=first["next_offset"],
                )
                self.assertIsNone(rest["next_offset"])
                self.assertEqual(
                    len(
                        {
                            r["route"]
                            for r in first["actions"] + rest["actions"]
                        }
                    ),
                    23,
                )
                self.assertEqual(await converter.tools("owner", "agent"), [])
                self.assertIsNone(await converter.latest("owner", "agent"))

            client = httpx.AsyncClient
            with patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: client(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(204)
                    ),
                    **kwargs,
                ),
            ):
                await call(
                    "register_enterprise_business",
                    source=source,
                    business_scope="/jobs/2",
                )
            fresh = EnterpriseBusiness(storage, read_apis=config)
            registered = await fresh.progress(
                "owner",
                "agent",
                inventory_id=inventory_id,
                status="registered",
            )
            self.assertEqual(
                registered["counts"], {"discovered": 22, "registered": 1}
            )
            self.assertEqual(
                [r["route"] for r in registered["actions"]], ["/jobs/2"]
            )
            self.assertTrue(registered["actions"][0]["evidence"])
            self.assertEqual(registered["actions"][0]["basis"], "api")
            by_scope = await fresh.progress(
                "owner", "agent", business_scope="Work"
            )
            self.assertEqual(by_scope["inventory_id"], inventory_id)
            self.assertEqual(by_scope["total"], 23)
            # Revoked connections cannot leave the progress display saying 'registered'.
            revoked = await EnterpriseBusiness(storage).progress(
                "owner", "agent", inventory_id=inventory_id, status="pending"
            )
            self.assertEqual(
                revoked["actions"][0]["reason"], "read_connection_required"
            )
            for user, agent in (("owner", "other"), ("other", "agent")):
                result = await fresh.progress(
                    user, agent, inventory_id=inventory_id
                )
                self.assertIsNone(result["inventory_id"])
            with self.assertRaises(ValueError):
                await fresh.progress("owner", "agent", inventory_id="../other")
            # A receipt saved by an older version is still browseable on its own.
            directory = fresh._directory("owner", "agent")
            imported = next(directory.glob("*.json"))
            (directory / "inventories" / imported.name).unlink()
            old = json.loads(imported.read_bytes())
            old.pop("business_scope")
            old.pop("inventory_id")
            imported.write_text(json.dumps(old))
            legacy = await fresh.progress(
                "owner", "agent", inventory_id=imported.stem
            )
            self.assertEqual(legacy["counts"], {"registered": 1})

    async def test_changed_source_action_needs_fresh_verification(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "client"
            source.mkdir()
            script = source / "reports.ts"
            spec = root / "api.json"
            spec.write_text(
                json.dumps(
                    {
                        "openapi": "3.1.0",
                        "paths": {
                            "/reports": {
                                "get": {
                                    "parameters": [
                                        {
                                            "in": "query",
                                            "name": "size",
                                            "schema": {"type": "integer"},
                                        }
                                    ],
                                    "responses": {"204": {}},
                                }
                            }
                        },
                    }
                )
            )
            converter = EnterpriseBusiness(
                root / "output",
                source_roots={"u": [source]},
                read_apis={
                    "u": [
                        EnterpriseReadAPI(
                            spec, "https://example.test", {"/reports": {}}
                        )
                    ]
                },
            )
            code = "import axios from 'axios';\nexport function reports(size: number = 20) {return axios.get('/reports', {params: {size: size}});}"
            script.write_text(code)
            client = httpx.AsyncClient
            with patch(
                "httpx.AsyncClient",
                side_effect=lambda **kwargs: client(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(204)
                    ),
                    **kwargs,
                ),
            ):
                result = await converter.convert("u", "a", str(source))
            self.assertEqual(result["tools"][0]["status"], "registered")
            same = await converter.discover("u", "a", str(source))
            self.assertEqual(same["counts"], {"registered": 1})
            script.write_text(code.replace("= 20", "= 10"))
            changed = await converter.discover("u", "a", str(source))
            self.assertEqual(changed["counts"], {"discovered": 1})
            self.assertEqual(changed["actions"][0]["basis"], "source")

    async def test_chat_import_scope_and_receipt_size(self) -> None:
        operations = {
            f"/orders/{n}": {
                "get": {"tags": ["库存 · 入库单"], "responses": {"204": {}}}
            }
            for n in range(30)
        }
        operations["/orders-old"] = {
            "get": {"tags": ["历史入库单"], "responses": {"204": {}}}
        }
        definition = json.dumps({"openapi": "3.1.0", "paths": operations})
        with TemporaryDirectory() as tmp:
            converter = EnterpriseBusiness(Path(tmp))
            register = converter.chat_tools("owner", "a", None)[0]
            for scope in ("入库单", "/orders/"):
                chunk = await register.call(
                    source=definition, business_scope=scope
                )
                receipt = json.loads(chunk.content[0].text)
                self.assertEqual(
                    (
                        receipt["total"],
                        receipt["registered"],
                        receipt["pending"],
                    ),
                    (30, 0, 30),
                )
                self.assertEqual(
                    len(receipt["tools"]) + receipt["omitted"], 30
                )
                self.assertLess(len(receipt["tools"]), 30)
                self.assertEqual(
                    len((await converter.latest("owner", "a"))["tools"]), 30
                )
                self.assertNotIn("/orders-old", str(receipt))
            missing = await register.call(
                source=definition, business_scope="不存在"
            )
            self.assertEqual(json.loads(missing.content[0].text)["total"], 0)
            full = await converter.convert("owner", "a", definition)
            self.assertEqual(len(full["tools"]), 31)

    async def test_legacy_receipt_only_shortened_in_model_input(self) -> None:
        from agentscope.app.middleware._enterprise_receipt import (
            EnterpriseReceiptMiddleware,
        )
        from agentscope.message import AssistantMsg, ToolResultBlock, TextBlock
        from unittest.mock import AsyncMock

        raw = '{"kind": "api", "diagnostics": [], "tools": [' + "x" * 25000
        raw += (
            "\n<<<TRUNCATED>>>\n<system-reminder>saved file</system-reminder>"
        )
        result = ToolResultBlock(
            id="one",
            name="register_enterprise_business",
            output=[TextBlock(text=raw)],
        )
        msg = AssistantMsg(name="agent", content=[result])
        handler = AsyncMock()
        await EnterpriseReceiptMiddleware().on_model_call(
            None, {"messages": [msg]}, handler
        )
        projected = handler.await_args.kwargs["messages"][0].content[0]
        self.assertEqual(projected.id, result.id)
        self.assertLess(len(projected.output[0].text), 500)
        self.assertIn("saved file", projected.output[0].text)
        self.assertEqual(msg.content[0].output[0].text, raw)

    async def test_api_import_registers_only_a_validated_read(self) -> None:
        response = {
            "200": {
                "description": "OK",
                "content": {
                    "application/json": {"schema": {"type": "integer"}}
                },
            },
        }
        route = "/purchase/receipts/{id}"
        document = {
            "openapi": "3.1.0",
            "servers": [{"url": "https://untrusted.example"}],
            "paths": {
                route: {
                    "parameters": [
                        {
                            "in": "path",
                            "name": "id",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "get": {"summary": "查询采购入库单", "responses": response},
                    "delete": {"summary": "删除采购入库单", "responses": response},
                },
                "/failedRead": {"get": {"responses": response}},
            },
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "openapi.json"
            spec.write_text(json.dumps(document), encoding="utf-8")
            profile = EnterpriseReadAPI(
                spec,
                "http://127.0.0.1:8002/api",
                {route: {"path": {"id": 1090}}, "/failedRead": {}},
            )
            converter = EnterpriseBusiness(
                root / "output",
                read_apis={"owner": [profile]},
            )
            sent = []

            def respond(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.host, "127.0.0.1")
                sent.append(request.url.path)
                if request.url.path.endswith("failedRead"):
                    return httpx.Response(200, json={"invalid": True})
                return httpx.Response(200, json=1090)

            real_client = httpx.AsyncClient
            with patch(
                "agentscope.business._business.httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(
                    transport=httpx.MockTransport(respond),
                    **kwargs,
                ),
            ):
                result = await converter.convert(
                    "owner", "agent", spec.read_text()
                )
                self.assertEqual(result["kind"], "api")
                status = {tool["label"]: tool for tool in result["tools"]}
                self.assertEqual(status["查询采购入库单"]["status"], "registered")
                self.assertEqual(status["删除采购入库单"]["status"], "pending")
                self.assertEqual(
                    status["/failedRead"]["reason"],
                    "verification_failed",
                )
                self.assertCountEqual(
                    sent,
                    ["/api/purchase/receipts/1090", "/api/failedRead"],
                )
                self.assertEqual(
                    await converter.latest("owner", "agent"), result
                )
                self.assertIsNone(await converter.latest("other", "agent"))
                self.assertEqual(
                    await converter.tools("owner", "other-agent"), []
                )
                tools = await converter.tools("owner", "agent")
                self.assertEqual(len(tools), 1)
                await tools[0].call(path={"id": 1091})
                self.assertEqual(sent[-1], "/api/purchase/receipts/1091")

            spec.write_text('{"openapi":"3.1.0","paths":{}}', encoding="utf-8")
            self.assertEqual(await converter.tools("owner", "agent"), [])

    async def test_source_alone_preserves_a_client_plan(self) -> None:
        fixture = (
            Path(__file__).parent / "fixtures/business_tools/purchase/client"
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            converter = EnterpriseBusiness(
                root,
                source_roots={"owner": [fixture]},
            )
            with self.assertRaises(PermissionError):
                await converter.convert(
                    "other", "agent", str(fixture.resolve())
                )
            result = await converter.convert(
                "owner", "agent", str(fixture.resolve())
            )
            self.assertEqual(result["kind"], "source")
            self.assertTrue(
                any(
                    tool["reason"] == "contract_required"
                    for tool in result["tools"]
                ),
            )
            stored = json.loads(next(root.glob("*/*.json")).read_text())
            self.assertTrue(
                any(
                    record["source_plan"]["functions"]
                    for record in stored["records"]
                )
            )
            self.assertEqual(await converter.tools("owner", "agent"), [])

    async def test_yaml_definition_generates_a_draft_without_a_connection(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            converter = EnterpriseBusiness(Path(directory))
            draft = await converter.convert(
                "owner",
                "agent",
                """
openapi: 3.0.1
paths:
  /receipts:
    get:
      responses:
        200:
          description: OK
""",
            )
            self.assertEqual(len(draft["tools"]), 1)
            self.assertEqual(
                draft["tools"][0]["reason"], "verification_required"
            )
            with self.assertRaisesRegex(ValueError, "configured OpenAPI"):
                await converter.convert(
                    "owner", "agent", "http://127.0.0.1/api"
                )

    async def test_source_uses_verified_plan_and_authorized_contract(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "client"
            source.mkdir()
            (source / "purchase.ts").write_text(
                "import axios from 'axios';\n"
                "export function receipts(limit: number = 20) {"
                "return axios.get('/receipts', {params: {limit: limit}}); }"
            )
            spec: dict = {
                "openapi": "3.1.0",
                "paths": {
                    "/receipts": {"$ref": "#/components/pathItems/list"},
                    "/unrelated": {"get": {"responses": {"204": {}}}},
                },
                "components": {
                    "pathItems": {
                        "list": {
                            "get": {
                                "parameters": [
                                    {
                                        "in": "query",
                                        "name": "limit",
                                        "schema": {"type": "integer"},
                                    }
                                ],
                                "responses": {"204": {}},
                            }
                        }
                    }
                },
            }
            canonical = root / "spec.json"
            canonical.write_text(json.dumps(spec))
            api = EnterpriseReadAPI(
                canonical, "https://example.test", {"/receipts": {}}
            )
            converter = EnterpriseBusiness(
                root / "data",
                source_roots={"owner": [source]},
                read_apis={"owner": [api]},
            )
            calls = []

            def respond(request: httpx.Request) -> httpx.Response:
                calls.append(
                    (request.url.path, request.url.params.get("limit"))
                )
                return httpx.Response(204)

            client = httpx.AsyncClient
            with patch(
                "agentscope.business._business.httpx.AsyncClient",
                side_effect=lambda **opts: client(
                    transport=httpx.MockTransport(respond), **opts
                ),
            ):
                result = await converter.convert("owner", "agent", str(source))
                self.assertEqual(
                    [(r["route"], r["status"]) for r in result["tools"]],
                    [("/receipts", "registered")],
                )
                (tool,) = await converter.tools("owner", "agent")
                await tool.call()
                await tool.call(limit=7)
                self.assertEqual(
                    calls,
                    [
                        ("/receipts", "20"),
                        ("/receipts", "20"),
                        ("/receipts", "7"),
                    ],
                )
                # Bundling the contract must preserve the same compiled entry.
                (source / "openapi.json").write_text(json.dumps(spec))
                result = await converter.convert("owner", "agent", str(source))
                self.assertEqual(calls[-1], ("/receipts", "20"))
                api.samples["/receipts"] = {"query": {"limit": 33}}
                await converter.convert("owner", "agent", str(source))
                self.assertEqual(calls[-1], ("/receipts", "33"))
                self.assertEqual(
                    len(await converter.tools("owner", "agent")), 1
                )
            spec["paths"]["/missing"] = {"$ref": "#/components/pathItems/nope"}
            result = await converter.convert(
                "owner", "agent", json.dumps(spec)
            )
            self.assertIn(
                "unresolved_path_reference: /missing", result["diagnostics"]
            )
            self.assertIn("/receipts", [r["route"] for r in result["tools"]])

    async def test_protected_http_routes_require_server_identity(self) -> None:
        from fastapi import FastAPI, Depends
        from agentscope.app.deps import get_current_user_id

        with TemporaryDirectory() as directory:
            app = FastAPI()
            app.state.enterprise_business = EnterpriseBusiness(
                Path(directory), source_roots={"owner": [Path(directory)]}
            )

            @app.get("/chat")
            @app.get("/agent/a/enterprise-business")
            async def identity(
                user: str = Depends(get_current_user_id),
            ) -> str:
                return user

            def transport(principal: str | None = None) -> httpx.ASGITransport:
                async def authenticated(
                    scope: dict, receive: Any, send: Any
                ) -> None:
                    if principal is not None:
                        scope["agentscope.user_id"] = principal
                    await app(scope, receive, send)

                return httpx.ASGITransport(
                    app=authenticated, client=("127.0.0.1", 1234)
                )

            async with httpx.AsyncClient(
                transport=transport(), base_url="http://localhost"
            ) as client:
                self.assertEqual(
                    (
                        await client.get(
                            "/chat", headers={"X-User-ID": "owner"}
                        )
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/agent/a/enterprise-business",
                            headers={"X-User-ID": "other"},
                        )
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/chat", headers={"X-User-ID": "other"}
                        )
                    ).status_code,
                    200,
                )
            async with httpx.AsyncClient(
                transport=transport("owner"), base_url="http://localhost"
            ) as client:
                self.assertEqual(
                    (await client.get("/agent/a/enterprise-business")).json(),
                    "owner",
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/chat", headers={"X-User-ID": "other"}
                        )
                    ).status_code,
                    403,
                )

    async def test_agent_skill_catalog_across_imports_and_sessions(
        self,
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from agentscope.app._router._enterprise_business import (
            enterprise_business_router,
        )
        from agentscope.permission import (
            PermissionBehavior,
            PermissionDecision,
        )
        from agentscope.state import AgentState
        from agentscope.tool import FunctionTool
        from agentscope.tool._business_search import PublishedBusinessTool

        async def never_invoke() -> str:
            raise AssertionError("Showing a skill must not execute the tool")

        with TemporaryDirectory() as temp:
            root = Path(temp)
            profiles = []
            for index in range(2):
                spec = root / f"{index}.json"
                routes = {
                    f"/batch{index}/operation{n}": {
                        "get": {
                            "summary": f"业务 {index}-{n}",
                            "responses": {"204": {}},
                        },
                        "post": {"responses": {"204": {}}},
                    }
                    for n in range(3)
                }
                spec.write_text(
                    json.dumps({"openapi": "3.1.0", "paths": routes})
                )
                profiles.append(
                    EnterpriseReadAPI(
                        spec, "https://test.invalid", dict.fromkeys(routes, {})
                    )
                )
            converter = EnterpriseBusiness(
                root / "records",
                local_user="owner",
                read_apis={"owner": profiles},
            )
            client_cls = httpx.AsyncClient
            with patch(
                "agentscope.business._business.httpx.AsyncClient",
                side_effect=lambda **opts: client_cls(
                    transport=httpx.MockTransport(
                        lambda _: httpx.Response(204)
                    ),
                    **opts,
                ),
            ):
                for profile in profiles:
                    await converter.convert(
                        "owner", "purchasing", profile.document.read_text()
                    )
                await converter.convert(
                    "owner", "purchasing", profiles[-1].document.read_text()
                )

            save = PublishedBusinessTool(
                never_invoke,
                business="采购入库单",
                action="save",
                name="save_receipt",
            )
            hidden = PublishedBusinessTool(
                never_invoke,
                business="采购入库单",
                action="query",
                name="hidden",
                permission=PermissionDecision(
                    behavior=PermissionBehavior.DENY, message="Not available"
                ),
            )
            host = AsyncMock(
                return_value=[
                    save,
                    hidden,
                    FunctionTool(never_invoke, name="register_example"),
                ]
            )

            async def factory(user: str, agent: str, session: str) -> list:
                return [
                    *await converter.tools(user, agent),
                    *await host(user, agent, session),
                ]

            app = FastAPI()
            app.include_router(enterprise_business_router)
            app.state.enterprise_business = converter
            app.state.extra_agent_tools = factory
            app.state.storage = SimpleNamespace(
                get_agent=AsyncMock(
                    side_effect=lambda u, a: object()
                    if u == "owner" and a in {"purchasing", "other"}
                    else None
                ),
                get_session=AsyncMock(
                    side_effect=lambda u, a, s: SimpleNamespace(
                        state=AgentState()
                    )
                    if (u, a, s) == ("owner", "purchasing", "current")
                    else None
                ),
            )
            path = "/agent/purchasing/enterprise-business/skills"
            async with client_cls(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                headers={"X-User-ID": "owner"},
            ) as client:
                listed = (await client.get(path)).json()
                self.assertEqual(
                    {s["display_name"] for s in listed},
                    {f"业务 {i}-{n}" for i in range(2) for n in range(3)},
                )
                self.assertEqual(len(listed), 6)
                self.assertEqual(
                    (
                        await client.get(
                            path, params={"session_id": "foreign"}
                        )
                    ).status_code,
                    404,
                )
                host.assert_not_awaited()
                current = (
                    await client.get(path, params={"session_id": "current"})
                ).json()
                self.assertEqual(
                    {s["name"] for s in current},
                    {s["name"] for s in listed} | {"save_receipt"},
                )
                host.assert_awaited_once_with("owner", "purchasing", "current")
                self.assertEqual(
                    (
                        await client.get(
                            path, headers={"X-User-ID": "outsider"}
                        )
                    ).status_code,
                    401,
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/agent/other/enterprise-business/skills"
                        )
                    ).json(),
                    [],
                )
                profiles[0].document.write_text(
                    '{"openapi":"3.1.0","paths":{}}'
                )
                self.assertEqual(len((await client.get(path)).json()), 3)
