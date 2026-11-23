# -*- coding: utf-8 -*-
"""Validate composed reads through chat tools and the current Agent's skills."""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

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
