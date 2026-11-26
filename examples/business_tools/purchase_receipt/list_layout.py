# -*- coding: utf-8 -*-
"""Yuanjing ERP purchase list column rules from PurchaseOrderView.vue."""

import asyncio
import math
import re

import httpx

from agentscope.business import ListColumn, ListLayout

MODULE = "purchase.order"
AUDIT_COLUMNS = (
    ("createdByName", "创建人"),
    ("createdTime", "创建时间"),
    ("updatedByName", "修改人"),
    ("updatedTime", "修改时间"),
    ("finalApproverName", "终审人"),
    ("finalApproveTime", "终审时间"),
)
BASE_COLUMNS = (
    ("billNo", "入库单号"),
    ("billType", "单据类型"),
    ("supplierName", "供应商"),
    ("supplierCode", "供应商编码"),
    ("departmentName", "部门"),
    ("supplierPhone", "供应商电话"),
    ("supplierType", "供应商类型"),
    ("buyerName", "采购员"),
    ("inventoryKeeperName", "入库责任人"),
    ("companyName", "子公司"),
    ("teamName", "隶属团队"),
    ("landingPercent", "落地百分比"),
    ("inboxPercent", "入筐百分比"),
    ("forkliftPercent", "叉车百分比"),
    ("landingCheckError", "落地检错"),
    ("inboxCheckError", "入筐检错"),
    ("forkliftCheckError", "叉车检错"),
    ("outboundStatus", "出库状态"),
    ("factoryDiscountPercent", "厂家折扣百分比"),
    ("factoryAddPercent", "厂家增值百分比"),
    ("factoryRebatePercent", "厂家返利"),
    ("goodsTotalAmount", "货物总额"),
    ("factoryDiscountAmount", "厂家优惠金额"),
    ("actualFactoryAmount", "实际厂家扣款"),
    ("paidAmount", "已付金额"),
    ("totalFreePay", "总计免付"),
    ("unpaidAmount", "未付金额"),
    ("truckFreight", "大车运费"),
    ("damageTotalAmount", "破损总额"),
    ("landingTotalAmount", "搬运总金额"),
    ("forkliftTotalAmount", "叉车总金额"),
    ("supplierBalance", "供应商余额"),
    ("prepaidBalance", "预付款余额"),
    ("totalWeight", "整单重量(吨)"),
    ("remark", "表头备注"),
    ("tailNote", "表头备注2"),
    *AUDIT_COLUMNS,
)
FIELD_KEYS = {
    "departmentName": "departmentId",
    "buyerName": "buyerId",
    "inventoryKeeperName": "inventoryKeeperId",
    "companyName": "companyId",
    "teamName": "teamId",
    "paidAmount": "_paidAmount",
    "totalFreePay": "_totalFreePay",
    "unpaidAmount": "_unpaidAmount",
}
AUDIT_FIELDS = {key for key, _ in AUDIT_COLUMNS}
STATUS_COLUMNS = (
    ("listPrintCount", "打印次数"),
    ("listApproveStatus", "审核状态"),
    ("listClosedStatus", "结案状态"),
)
DEFAULT_RANKS = {
    key: index
    for index, key in enumerate(
        [
            FIELD_KEYS.get(key, key)
            for key, _ in BASE_COLUMNS
            # These fields use their base position when no sort was saved.
            if key not in AUDIT_FIELDS
            and key not in {
                "factoryDiscountPercent",
                "factoryAddPercent",
                "factoryRebatePercent",
            }
        ]
        + ["billStatus", "approvalStatus", "closedStatus"]
        + [key for key, _ in AUDIT_COLUMNS]
        + ["personalMark"],
        1,
    )
}


def _numeric(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return number if math.isfinite(number) else 0


def _config_key(key: str) -> str:
    custom = re.fullmatch(r"custom(?:_fieldField|_field_|Field|_)(\d+)", key)
    return f"customField{custom[1]}" if custom else FIELD_KEYS.get(key, key)


async def _setting(client: httpx.AsyncClient, path: str) -> object:
    response = await client.get(path)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("无法读取 ERP 的采购入库单显示配置。")
    return payload.get("data")


async def purchase_list_layout(client: httpx.AsyncClient) -> ListLayout:
    """Read the ERP account's field permissions and column settings."""
    hidden, configs, custom, descriptions = await asyncio.gather(
        _setting(client, f"system/column-config/visible-columns/{MODULE}"),
        _setting(client, f"system/column-config/list/{MODULE}"),
        _setting(client, f"system/custom-field/list/{MODULE}"),
        _setting(client, "system/field-description/all"),
    )
    if (
        (
            hidden is not None
            and (
                not isinstance(hidden, list)
                or any(not isinstance(key, str) for key in hidden)
            )
        )
        or not isinstance(configs, list)
        or not isinstance(custom, list)
        or not isinstance(descriptions, dict)
    ):
        raise ValueError("ERP 的采购入库单显示配置格式不正确。")
    hidden_keys = {_config_key(key) for key in (hidden or [])}
    rules = descriptions.get(MODULE) or {}
    saved = {}
    priorities = {}
    for config in configs:
        raw_key = config.get("columnKey")
        if not raw_key:
            continue
        key = _config_key(raw_key)
        sort = config.get("columnSort")
        priority = (
            8 * bool(config.get("id"))
            + (raw_key == key)
            + 2 * (sort is not None)
        )
        if priority >= priorities.get(key, -1):
            saved[key] = config
            priorities[key] = priority

    columns = list(BASE_COLUMNS)
    for field in custom:
        if type(field.get("isVisible")) is int and field["isVisible"] == 1:
            data_key = re.sub(r"_(\d+)$", r"Field\1", field["fieldCode"])
            columns.append((data_key, field["fieldLabel"]))
    # Fallback positions start after the field permission filter, before the
    # saved visibility filter. The client then inserts the personal mark.
    columns = [
        (key, label)
        for key, label in columns
        if _config_key(key) not in hidden_keys
    ]
    if "personalMark" not in hidden_keys:
        columns.append(("personalMark", "独属标记"))
    visible = []
    for position, (data_key, label) in enumerate(columns, 1):
        key = _config_key(data_key)
        config = saved.get(key, {})
        if config.get("columnVisible") in (0, "0"):
            continue
        saved_rank = _numeric(config.get("columnSort"))
        source_rank = DEFAULT_RANKS.get(key)
        rule = rules.get(key) or rules.get(data_key) or {}
        collapsed = int(_numeric(rule.get("isCollapsed")) == 1)
        visible.append(
            (
                saved_rank if saved_rank > 0 else source_rank or position,
                2 if key in AUDIT_FIELDS else collapsed,
                source_rank or math.inf,
                key.lower(),
                position,
                ListColumn(data_key, label),
            )
        )
    # These display-only fields are appended by the client's bill status view.
    status = [
        ListColumn(key, label)
        for key, label in STATUS_COLUMNS
        if key not in hidden_keys
    ]
    visible.sort(key=lambda entry: entry[:-1])
    ordered = [entry[-1] for entry in visible]
    return ListLayout("采购入库单", tuple(ordered + status))


def purchase_list_cells(row: dict) -> dict:
    """Flatten cells the ERP renders from derived or nested values."""
    status = row.get("listStatus") or {}
    approval = status.get("listApproveStatus")
    approve_status = row.get("approveStatus")
    if approve_status is None:
        approve_status = row.get("approvalStatus")
    if _numeric(approve_status) == 2 or _numeric(row.get("billStatus")) == 2:
        approval = "已审核"
    return {
        **row,
        "companyName": row.get("companyName") or row.get("company_name") or "-",
        "departmentName": row.get("departmentName") or "-",
        "buyerName": row.get("buyerName") or "-",
        "inventoryKeeperName": row.get("inventoryKeeperName") or "-",
        "teamName": row.get("teamName") or "-",
        "outboundStatus": row.get("outboundStatus") or "未入库",
        "listPrintCount": status.get("listPrintCount"),
        "listApproveStatus": approval,
        "listClosedStatus": status.get("listClosedStatus"),
    }


async def configured_purchase_page(
    client: httpx.AsyncClient, page: dict
) -> dict:
    """Pair the fetched rows with the current client's display rules."""
    layout = await purchase_list_layout(client)
    return {
        "total": page["total"],
        "records": [purchase_list_cells(row) for row in page["records"]],
        "layout": layout,
    }
