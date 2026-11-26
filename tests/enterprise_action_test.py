# -*- coding: utf-8 -*-
"""Validate composed reads through chat tools and the current Agent's skills."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from httpx import AsyncClient

from agentscope.app._enterprise_login import EnterpriseLogin
from agentscope.app._router._enterprise_business import (
    enterprise_business_router,
)
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.tool_login import ToolLogin, tool_login_scope
from agentscope.business import EnterpriseBusiness, EnterpriseReadAPI
from agentscope.business._enterprise import enterprise_skill_views
from agentscope.business._enterprise_action import ActionBlocked
from agentscope.permission import (
    PermissionBehavior,
    PermissionEngine,
    PermissionRule,
)
from agentscope.state import AgentState
from agentscope.tool._business_search import BusinessToolSearch


class EnterpriseActionTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.spec = self.root / "api.json"
        response = {"200": {"content": {"application/json": {"schema": {}}}}}

        def op(name, where, kind):
            return {
                "get": {
                    "summary": name,
                    "parameters": [
                        {
                            "name": name,
                            "in": where,
                            "required": True,
                            "schema": {"type": kind},
                        }
                    ],
                    "responses": response,
                }
            }

        self.spec.write_text(
            json.dumps(
                {
                    "openapi": "3.1.0",
                    "paths": {
                        "/find": op("code", "query", "string"),
                        "/entries/{key}": {
                            **op("key", "path", "integer"),
                            "delete": {"responses": {"204": {}}},
                        },
                    },
                }
            )
        )
        self.api = EnterpriseReadAPI(
            self.spec,
            "https://example.test/v1",
            {
                "/find": {"query": {"code": "A"}},
                "/entries/{key}": {"path": {"key": 7}},
            },
        )
        self.state = AgentState()
        self.svc = self.service()
        self.chat = self.chat_tools(self.svc)
        inventory = await self.call(
            "discover_enterprise_actions", source=self.spec.read_text()
        )
        self.inventory = inventory["inventory_id"]
        operations = {
            (item["method"], item["route"]): item["name"]
            for item in inventory["actions"]
        }
        self.detail = operations["GET", "/entries/{key}"]
        self.delete = operations["DELETE", "/entries/{key}"]
        business_input = {"source": "input", "pointer": "/number"}
        self.plan = {
            "business": "Entries",
            "title": "Find entry by number",
            "inputs": {"number": "string"},
            "steps": [
                {
                    "key": "find",
                    "operation": operations["GET", "/find"],
                    "query": {"code": business_input},
                    "select": "/rows",
                    "match": {"/number": business_input},
                },
                {
                    "key": "detail",
                    "operation": self.detail,
                    "path": {"key": {"source": "find", "pointer": "/key"}},
                    "match": {"/number": business_input},
                },
            ],
        }
        self.sent = []
        self.second_started = asyncio.Event()
        self.mode = "ok"
        client = httpx.AsyncClient
        fake = patch(
            "httpx.AsyncClient",
            side_effect=lambda **opts: client(
                transport=httpx.MockTransport(self.upstream), **opts
            ),
        )
        fake.start()
        self.addCleanup(fake.stop)

    def service(self):
        return EnterpriseBusiness(
            self.root / "private", read_apis={"one": [self.api]}
        )

    def chat_tools(self, service, owner="one", agent="current"):
        return {t.name: t for t in service.chat_tools(owner, agent, None)}

    async def call(self, name, **kwargs):
        tool = self.chat[name]
        if tool.is_state_injected:
            kwargs["_agent_state"] = self.state
        return json.loads((await tool.call(**kwargs)).content[0].text)

    async def register(self):
        return await self.call(
            "register_enterprise_action",
            inventory_id=self.inventory,
            plan=self.plan,
            sample_inputs={"number": "A"},
        )

    async def upstream(self, request):
        self.assertEqual(request.method, "GET")
        self.sent.append(str(request.url))
        if request.url.path.endswith("/find"):
            number = request.url.params["code"]
            row = {
                "key": 7 if number == "A" else 8,
                "number": number,
                "ignored": "NOT-IN-CHECKPOINT",
            }
            rows = [row, row] if self.mode == "ambiguous" else [row]
            total = 12 if self.mode == "paginated" else len(rows)
            return httpx.Response(200, json={"rows": rows, "total": total})
        self.second_started.set()
        if self.mode == "interrupt":
            await asyncio.Future()
        if self.mode == "fail":
            return httpx.Response(503)
        number = "A" if request.url.path.endswith("/7") else "B"
        return httpx.Response(
            200, json={"number": number, "description": "from detail"}
        )

    async def test_resume_after_cancellation_and_failure_then_use_skill(self):
        self.mode = "interrupt"
        active = asyncio.create_task(self.register())
        await asyncio.wait_for(self.second_started.wait(), timeout=3)
        items = await self.call("get_enterprise_action_progress")
        (item,) = items["actions"]
        self.assertEqual((item["completed"], item["next_step"]), (1, "detail"))
        self.assertEqual(await self.svc.tools("one", "current"), [])
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        (store,) = (self.svc._directory("one", "current") / "actions").glob(
            "*.json"
        )
        self.assertNotIn("NOT-IN-CHECKPOINT", store.read_text())
        self.svc = self.service()
        self.chat = self.chat_tools(self.svc)
        self.mode = "fail"
        result = await self.call(
            "resume_enterprise_action", action_id=item["action_id"]
        )
        self.assertEqual(
            (result["completed"], result["reason"]), (1, "upstream_http_503")
        )
        self.assertEqual(
            result["diagnostic"],
            {
                "code": "upstream_http_503",
                "context": {
                    "stage": "action_probe",
                    "error_type": "HTTPStatusError",
                    "http_status": 503,
                    "step": "detail",
                },
            },
        )
        self.mode = "ok"
        results = await asyncio.gather(
            *(
                self.call(
                    "resume_enterprise_action", action_id=item["action_id"]
                )
                for _ in range(2)
            )
        )
        self.assertTrue(all(r["status"] == "registered" for r in results))
        self.assertTrue(all("diagnostic" not in r for r in results))
        self.assertEqual(
            self.sent,
            ["https://example.test/v1/find?code=A"]
            + ["https://example.test/v1/entries/7"] * 3,
        )
        tools = await self.svc.tools("one", "current")
        self.assertEqual(
            [
                s.display_name
                for s in await enterprise_skill_views(tools, self.state)
            ],
            ["Find entry by number"],
        )
        search = BusinessToolSearch("search_business_tools", tools)
        found = await search.call(
            query="Find entry by number", _agent_state=self.state
        )
        (match,) = json.loads(found.content[0].text)["matches"]
        self.assertEqual(match["display_name"], "Find entry by number")
        self.assertTrue(await tools[0].show_schema(self.state))
        response = await tools[0].call(_agent_state=self.state, number="B")
        self.assertEqual(
            json.loads(response.content[0].text)["result"]["number"], "B"
        )
        self.assertEqual(
            self.sent[-2:],
            [
                "https://example.test/v1/find?code=B",
                "https://example.test/v1/entries/8",
            ],
        )
        for user, agent in (("one", "other"), ("other", "current")):
            self.assertEqual(await self.svc.tools(user, agent), [])
            with self.assertRaises(ValueError):
                await self.chat_tools(self.svc, user, agent)[
                    "resume_enterprise_action"
                ].call(_agent_state=self.state, action_id=item["action_id"])

    async def test_dependency_changes_and_permission_revocation(self):
        item = await self.register()
        (skill,) = await self.svc.tools("one", "current")
        self.plan["steps"][1]["match"]["/description"] = "from detail"
        await self.register()
        self.assertEqual(len(self.sent), 3)
        with self.assertRaises(ActionBlocked):
            await skill.call(_agent_state=self.state, number="A")
        (skill,) = await self.svc.tools("one", "current")
        PermissionEngine(self.state.permission_context).add_rule(
            PermissionRule(
                tool_name=self.detail,
                rule_content=None,
                behavior=PermissionBehavior.DENY,
                source="userSettings",
            )
        )
        self.assertEqual(await enterprise_skill_views([skill], self.state), [])
        with self.assertRaisesRegex(ActionBlocked, "step_permission_required"):
            await skill.call(_agent_state=self.state, number="A")
        del self.api.samples["/find"]
        self.assertEqual(await self.svc.tools("one", "current"), [])
        with self.assertRaisesRegex(
            ActionBlocked, "verification_sample_required"
        ):
            await skill.call(_agent_state=self.state, number="A")
        progress = await self.call(
            "get_enterprise_action_progress", action_id=item["action_id"]
        )
        self.assertEqual(progress["actions"][0]["status"], "pending")

    async def test_reauthentication_restarts_composed_reads(self):
        document = json.loads(self.spec.read_text())
        document["components"] = {
            "securitySchemes": {
                "Access": {"type": "http", "scheme": "bearer"},
            },
        }
        document["security"] = [{"Access": []}]
        source = json.dumps(document)
        self.spec.write_text(source)
        inventory = await self.call(
            "discover_enterprise_actions", source=source
        )
        self.inventory = inventory["inventory_id"]
        operations = {
            item["route"]: item["name"]
            for item in inventory["actions"]
            if item["method"] == "GET"
        }
        self.plan["steps"][0]["operation"] = operations["/find"]
        self.plan["steps"][1]["operation"] = operations["/entries/{key}"]

        broker = ToolLogin(InMemoryMessageBus(), timeout=3)
        self.svc.login = EnterpriseLogin(broker)
        scope = ("one", "current", "chat")
        expired = set()
        accounts = {"first-account": 7, "new-account": 8, "last-account": 9}
        requests = []

        def upstream(request):
            account = request.headers["authorization"].removeprefix("Bearer ")
            route = request.url.path.removeprefix("/v1")
            requests.append((account, route))
            if route == "/find":
                return httpx.Response(
                    200,
                    json={"rows": [{"key": accounts[account], "number": "A"}]},
                )
            if account in expired:
                return httpx.Response(401)
            self.assertEqual(route, f"/entries/{accounts[account]}")
            if account == "first-account":
                return httpx.Response(503)
            return httpx.Response(
                200, json={"number": "A", "account": account}
            )

        self.upstream = upstream

        async def complete_login(action, credential):
            task = asyncio.create_task(action)
            try:
                async with asyncio.timeout(2):
                    while not (view := broker.pending(scope)):
                        await asyncio.sleep(0.005)
                    await broker.submit(
                        scope,
                        view.id,
                        view.revision,
                        {"credential": credential},
                    )
                    return await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        with tool_login_scope(*scope):
            first = await complete_login(self.register(), "first-account")
            self.assertEqual(
                (first["completed"], first["reason"]), (1, "upstream_http_503")
            )

            async def resume():
                return await self.call(
                    "resume_enterprise_action", action_id=first["action_id"]
                )

            self.assertEqual((await resume())["completed"], 1)
            expired.add("first-account")
            switched = await complete_login(resume(), "new-account")
            self.assertEqual(
                (switched["completed"], switched["reason"]),
                (1, "approved_sample_mismatch"),
            )
            self.assertEqual(
                requests,
                [("first-account", "/find")]
                + [("first-account", "/entries/7")] * 3
                + [("new-account", "/find")],
            )
            self.api.samples["/entries/{key}"] = {"path": {"key": 8}}
            self.assertEqual((await resume())["status"], "registered")
            self.assertEqual(requests[-1], ("new-account", "/entries/8"))

            (skill,) = await self.svc.tools("one", "current")
            expired.add("new-account")
            result = await complete_login(
                skill.call(_agent_state=self.state, number="A"), "last-account"
            )
            self.assertEqual(
                json.loads(result.content[0].text)["result"]["account"],
                "last-account",
            )
            self.assertEqual(
                requests[-4:],
                [
                    ("new-account", "/find"),
                    ("new-account", "/entries/8"),
                    ("last-account", "/find"),
                    ("last-account", "/entries/9"),
                ],
            )
            # The registered receipt still refers to the previous account.
            before = len(requests)
            stale = await resume()
            self.assertEqual(stale["reason"], "approved_sample_mismatch")
            self.assertEqual(requests[before:], [("last-account", "/find")])

            directory = self.svc._directory("one", "current") / "actions"
            (store,) = directory.glob("*.json")
            receipt = store.read_text()
            self.assertTrue(
                all(account not in receipt for account in accounts)
            )

    async def test_panel_hides_actions_and_resumes_in_owned_session(self):
        self.mode = "fail"
        visible = await self.register()
        self.plan["title"] = "Private action"
        hidden = await self.register()
        hidden_path = self.svc._action_path(
            "one", "current", hidden["action_id"]
        )
        hidden_record = json.loads(hidden_path.read_text())
        hidden_record["visible"] = False
        hidden_path.write_text(json.dumps(hidden_record))

        app = FastAPI()
        app.include_router(enterprise_business_router)
        app.state.enterprise_business = self.svc
        self.svc.local_user = "one"
        verify = self.svc._verify_action

        async def verify_in_chat(user_id, agent_id, action_id, state):
            self.assertEqual(
                ToolLogin.current_scope(), ("one", "current", "chat")
            )
            return await verify(user_id, agent_id, action_id, state)

        resume = patch.object(self.svc, "_verify_action", verify_in_chat)
        resume.start()
        self.addCleanup(resume.stop)
        app.state.storage = SimpleNamespace(
            get_agent=AsyncMock(return_value=object()),
            get_session=AsyncMock(
                side_effect=lambda _u, _a, session: SimpleNamespace(
                    agent_id="current" if session == "chat" else "other",
                    state=self.state,
                )
            ),
        )
        path = "/agent/current/enterprise-business/actions"
        params = {"session_id": "chat"}
        resume_path = path + "/" + visible["action_id"] + "/resume"
        hidden_resume = path + "/" + hidden["action_id"] + "/resume"
        async with AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers={"X-User-ID": "one"},
        ) as client:
            outsider = await client.get(
                path, headers={"X-User-ID": "outsider"}
            )
            self.assertEqual(outsider.status_code, 401)
            response = await client.get(path, params=params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json(),
                {
                    "actions": [
                        {
                            "action_id": visible["action_id"],
                            "title": "Find entry by number",
                            "status": "pending",
                            "completed": 1,
                            "total": 2,
                            "can_resume": True,
                        },
                    ],
                    "next_offset": None,
                },
            )
            foreign = await client.post(
                resume_path, params={"session_id": "foreign"}
            )
            self.assertEqual(foreign.status_code, 404)
            self.assertEqual(
                (await client.post(hidden_resume, params=params)).status_code,
                404,
            )
            engine = PermissionEngine(self.state.permission_context)
            engine.add_rule(
                PermissionRule(
                    tool_name=self.detail,
                    rule_content=None,
                    behavior=PermissionBehavior.DENY,
                    source="userSettings",
                )
            )
            denied = await client.get(path, params=params)
            self.assertEqual(denied.json()["actions"], [])
            self.assertEqual(
                (await client.post(resume_path, params=params)).status_code,
                404,
            )
            self.state.permission_context.deny_rules.clear()
            self.assertEqual(len(self.sent), 4)

            self.mode = "ok"
            result = await client.post(resume_path, params=params)
            self.assertEqual(result.status_code, 200)
            action = result.json()
            self.assertEqual(
                (action["status"], action["completed"]), ("registered", 2)
            )
            self.assertEqual(
                self.sent[4:], ["https://example.test/v1/entries/7"]
            )
            self.assertFalse(action["can_resume"])

    async def test_source_change_invalidates_only_the_affected_suffix(self):
        source = self.root / "client"
        source.mkdir()
        (source / "find.ts").write_text(
            "import axios from 'axios';\n"
            "export function find(code: string) { return axios.get('/find', {params: {code: code}}); }"
        )
        detail = source / "detail.ts"
        code = "import axios from 'axios';\nexport function detail(key: number) { return axios.get(`/entries/${key}`); }"
        detail.write_text(code)
        self.svc = EnterpriseBusiness(
            self.root / "private",
            source_roots={"one": [source]},
            read_apis={"one": [self.api]},
        )
        self.chat = self.chat_tools(self.svc)

        async def discover():
            inv = await self.call(
                "discover_enterprise_actions", source=str(source)
            )
            self.inventory = inv["inventory_id"]
            rows = {row["route"]: row for row in inv["actions"]}
            for step, route in zip(
                self.plan["steps"], ["/find", "/entries/{key}"], strict=True
            ):
                self.assertEqual(rows[route]["basis"], "source")
                step["operation"] = rows[route]["name"]

        await discover()
        first = await self.call(
            "register_enterprise_action",
            inventory_id=self.inventory,
            plan=self.plan,
        )
        self.assertEqual(first["status"], "registered")
        detail.write_text(code + "\n// updated client")
        self.assertEqual(await self.svc.tools("one", "current"), [])
        progress = await self.call(
            "get_enterprise_action_progress", action_id=first["action_id"]
        )
        (stale,) = progress["actions"]
        self.assertEqual(
            (stale["completed"], stale["blocked_step"], stale["reason"]),
            (1, "detail", "source_changed"),
        )
        await discover()
        fresh = await self.register()
        self.assertEqual(
            (fresh["status"], fresh["action_id"]),
            ("registered", first["action_id"]),
        )
        self.assertEqual(
            self.sent,
            ["https://example.test/v1/find?code=A"]
            + ["https://example.test/v1/entries/7"] * 2,
        )

    async def test_missing_verification_input_is_reported_by_name(self):
        self.plan["inputs"]["expected"] = "string"
        self.plan["steps"][-1]["match"]["/description"] = {
            "source": "input",
            "pointer": "/expected",
        }
        result = await self.call(
            "register_enterprise_action",
            inventory_id=self.inventory,
            plan=self.plan,
        )
        self.assertEqual(
            (result["reason"], result["missing_inputs"], self.sent),
            ("verification_inputs_required", ["expected"], []),
        )
        complete = await self.call(
            "register_enterprise_action",
            inventory_id=self.inventory,
            plan=self.plan,
            sample_inputs={"expected": "from detail"},
        )
        self.assertEqual(complete["action_id"], result["action_id"])
        self.assertEqual(complete["status"], "registered")

    async def test_ambiguity_sample_mismatch_and_write_never_advance(self):
        self.mode = "ambiguous"
        item = await self.register()
        self.assertEqual(
            (item["reason"], item["completed"]), ("ambiguous_records", 0)
        )
        self.mode = "paginated"
        self.plan["steps"][0]["total"] = "/total"
        item = await self.register()
        self.assertEqual(
            (item["reason"], item["completed"]),
            ("incomplete_candidate_list", 0),
        )
        self.mode = "ok"
        self.api.samples["/entries/{key}"] = {"path": {"key": 9}}
        item = await self.register()
        self.assertEqual(
            (item["reason"], item["next_step"]),
            ("approved_sample_mismatch", "detail"),
        )
        self.plan["steps"][1]["operation"] = self.delete
        item = await self.register()
        self.assertEqual(item["reason"], "documented_read_required")
        self.assertTrue(all("/find?" in url for url in self.sent))
