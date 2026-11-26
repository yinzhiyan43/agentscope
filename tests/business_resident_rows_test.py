# -*- coding: utf-8 -*-
"""Resident tool composition and per-chat ERP permission refresh."""
import json
from typing import Any
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx

from agentscope.app.tool_login import ToolLogin, tool_login_scope
from agentscope.business import RowActionSource
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool
from examples.business_tools.purchase_receipt.authentication import (
    PurchaseAuthentication,
)
from examples.business_tools.purchase_receipt.resident import (
    with_purchase_rows,
)


class ResidentPurchaseTest(IsolatedAsyncioTestCase):
    """Use the existing list reader and the normal scoped chat factory."""

    async def test_grants_original_page_order_and_current_permissions(
        self,
    ) -> None:
        """Check list snapshots, live permissions and ledger isolation."""
        scope = ("reader", "agent", "chat")
        display_row = {
            "billNo": "同号",
            "billDate": "隐藏业务日期",
            "supplierName": "供应商 A",
            "supplierPhone": "权限隐藏电话",
            "goodsTotalAmount": 10,
            "paidAmount": 7,
            "customField1": "可见自定义字段",
            "customField2": "隐藏自定义字段",
            "listStatus": {
                "listPrintCount": 0,
                "listApproveStatus": "已审核",
                "listPrintStatus": "隐藏打印状态",
            },
        }
        rows = [{**display_row, "id": key} for key in (41, 29)]
        info = {
            "tenantId": 12,
            "username": "employee",
            "permissionConfigured": True,
            "permissions": ["purchase:order:view"],
        }
        calls = []
        settings = {
            "visible-columns": ["supplierPhone", "paidAmount"],
            "column-config/list": [
                {"columnKey": "supplierName", "columnSort": 1},
                {"columnKey": "billNo", "columnSort": 2},
                {"columnKey": "goodsTotalAmount", "columnVisible": 0},
            ],
            "custom-field/list": [
                {
                    "fieldCode": "custom_1",
                    "fieldLabel": "渠道",
                    "isVisible": 1,
                },
                {
                    "fieldCode": "custom_2",
                    "fieldLabel": "内部",
                    "isVisible": 0,
                },
            ],
            "field-description/all": {},
        }

        def backend(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            self.assertEqual(request.method, "GET")
            if request.url.path.endswith("/auth/info"):
                data = info
            elif "/system/" in request.url.path:
                data = next(
                    value
                    for path, value in settings.items()
                    if path in request.url.path
                )
            else:
                data = {"order": {"id": 29}, "details": [{"remark1": "第二行"}]}
            return httpx.Response(200, json={"code": 200, "data": data})

        connection = Mock(
            base_url="https://example.invalid/api/",
            context={"tenant": 12, "account": "employee"},
            broker=ToolLogin,
        )
        connection.read_auth.return_value = httpx.BasicAuth("test", "test")
        connection.read = AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request(
                    "GET",
                    "https://example.invalid/api/purchase/order/page",
                ),
                json={"code": 200, "data": {"total": 2, "records": rows}},
            ),
        )
        auth = PurchaseAuthentication(
            Path("."),
            {},
            Path("unused"),
            connection,
            AsyncMock(),
        )
        self.enterContext(patch.object(auth, "published", return_value=Mock()))
        decision = PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Test purchase read",
        )

        def legacy(limit: int = 5) -> int:
            """List records."""
            return limit

        original = FunctionTool(
            legacy,
            name="query_recent_purchase_receipts",
            permission=decision,
        )
        other = FunctionTool(legacy, name="existing_tool", permission=decision)
        host = AsyncMock(return_value=[original, other])
        client = httpx.AsyncClient

        def connect(**kwargs: Any) -> httpx.AsyncClient:
            return client(**kwargs, transport=httpx.MockTransport(backend))

        with TemporaryDirectory() as tmp, patch.object(
            httpx,
            "AsyncClient",
            side_effect=connect,
        ):
            spec = Path(tmp) / "api.json"
            opt = {
                "operationId": "detail",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            }
            spec.write_text(
                json.dumps(
                    {
                        "openapi": "3.1.0",
                        "paths": {
                            "/purchase/order/page": {"get": {}},
                            "/purchase/order/{id}": {"get": opt},
                        },
                    },
                ),
                encoding="utf-8",
            )
            source = RowActionSource(
                Path(__file__).parent / "fixtures/row_actions",
                spec,
                "Purchase.vue",
                "handleEdit",
                "/purchase/order/page",
                "/purchase/order/{id}",
            )
            factory = with_purchase_rows(
                host,
                auth,
                source,
                Path(tmp) / "rows.sqlite",
                decision,
            )
            with tool_login_scope(*scope):
                tools = {t.name: t for t in await factory(*scope)}
                self.assertIs(tools["existing_tool"], other)
                listed = await tools["query_recent_purchase_receipts"](limit=2)
                page = json.loads(listed.content[0].text)
                self.assertEqual(page["records"][0], page["records"][1])
                columns = page["row_card"]["columns"]
                self.assertEqual(
                    columns[:2],
                    [
                        {"key": "supplierName", "label": "供应商"},
                        {"key": "billNo", "label": "入库单号"},
                    ],
                )
                projected = page["records"][0]
                self.assertEqual(list(projected), [c["key"] for c in columns])
                self.assertEqual(projected["customField1"], "可见自定义字段")
                self.assertEqual(projected["listPrintCount"], 0)
                self.assertFalse({
                    "id", "billDate", "supplierPhone", "goodsTotalAmount",
                    "paidAmount", "customField2", "listStatus", "listPrintStatus",
                }.intersection(projected))
                ref = page["list_ref"]
                args = {"list_ref": ref, "row_number": 2}
                rows.reverse()
                opener = tools["open_purchase_receipts_row"]
                opened = await opener(**args)
                self.assertEqual(
                    json.loads(opened.content[0].text)["order"]["id"],
                    29,
                )
                self.assertEqual(calls.count("/api/purchase/order/29"), 1)
                info["permissions"] = []
                with self.assertRaisesRegex(PermissionError, "无权"):
                    await opener(**args)
                info["tenantId"] = 99
                with self.assertRaisesRegex(PermissionError, "账套"):
                    await opener(**args)
                self.assertEqual(calls.count("/api/purchase/order/29"), 1)
                connection.read.assert_awaited_once()
                info["permissions"] = ["purchase:order:view"]
                info["tenantId"] = 12
                settings["visible-columns"] = {"failed": True}
                with self.assertRaisesRegex(ValueError, "显示配置格式不正确"):
                    await tools["query_recent_purchase_receipts"](limit=2)
            with tool_login_scope("reader", "agent", "other-chat"):
                with self.assertRaisesRegex(PermissionError, "变化"):
                    await opener(**args)
            host.return_value = [other]
            self.assertEqual(await factory("nobody", "agent", "chat"), [other])
