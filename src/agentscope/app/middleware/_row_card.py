# -*- coding: utf-8 -*-
"""Open a row quoted in a chat turn through the ordinary tool pipeline."""
import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import aclosing
from typing import Any

from ...message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from ...middleware import MiddlewareBase
from ...model import ChatResponse
from ...agent import Agent

_MATCH_FEEDBACK = frozenset(
    {"ambiguous", "not_found", "invalid_column", "refresh_required"},
)


def _row_number(text: str) -> int | None:
    digits = "一二三四五六七八九"
    match = re.fullmatch(
        r"(?:请)?(?:打开|查看)(?:刚才的?)?\s*第\s*"
        r"([0-9]{1,9}|[一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])"
        r"\s*(?:张|条|行|个)(?:单据|工单|记录)?[。！!]?",
        text.strip(),
    ) or re.fullmatch(r"open row ([0-9]{1,9})", text.strip(), re.I)
    if not match:
        return None
    ordinal = match[1]
    if ordinal.isdigit():
        return int(ordinal)
    if "十" in ordinal:
        tens, units = ordinal.split("十")
        return (digits.index(tens) + 1 if tens else 1) * 10 + (
            digits.index(units) + 1 if units else 0
        )
    return digits.index(ordinal) + 1


def _output(result: ToolResultBlock) -> str:
    if isinstance(result.output, str):
        return result.output
    return "\n".join(
        block.text for block in result.output if isinstance(block, TextBlock)
    )


def _json_result(result: ToolResultBlock) -> dict:
    try:
        value = json.loads(_output(result))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _history_results(agent: Agent) -> Iterator[ToolResultBlock]:
    for message in reversed(agent.state.context):
        if message.role == "assistant":
            yield from reversed(message.get_content_blocks("tool_result"))


def _condition_text(text: str, *, quoted: bool) -> tuple[str, bool] | None:
    text = text.strip().rstrip("。！!")
    followup = re.match(r"^(?:再)?补充(?:一个)?条件\s*[:：]?\s*", text)
    if followup:
        return text[followup.end() :], True
    command = re.match(r"^(?:请\s*)?(?:帮我\s*)?(打开|查看|获取|查询)\s*", text)
    if command:
        if quoted or command[1] == "打开" or text.endswith(("详情", "明细")):
            text = text[command.end() :]
        else:
            return None
    elif not quoted:
        return None
    text = re.sub(
        r"^(?:(?:刚才|当前|上一份)的?\s*)?"
        r"(?:(?:列表|卡片)[里中]?\s*[,，:：]?\s*)?(?:按|根据)?\s*",
        "",
        text,
    )
    text = re.sub(
        r"(?:的?(?:那|这)[张条行个](?:单据|记录)?)?的?(?:详情|明细)$",
        "",
        text,
    )
    if re.search(r"[=＝]|等于|为|是", text):
        return text, False
    return None


def _visible_conditions(text: str, columns: list[dict]) -> dict[str, str]:
    names = {
        name for col in columns for name in (col["key"], col["label"]) if name
    }
    if not names:
        raise ValueError("当前列表没有可用于匹配的可见列。")
    # Match the longest actual label first. A label can itself contain “是”.
    field = "|".join(
        re.escape(name) for name in sorted(names, key=len, reverse=True)
    )
    operator = r"\s*(?:[=＝]|等于|为|是)\s*"
    condition = re.compile(
        rf"\s*({field}){operator}"
        r'''(?:"([^"]*)"|“([^”]*)”|'([^']*)'|([^,，;；]+?))\s*'''
        rf"(?:[,，;；]\s*(?:(?:并且|且)\s+)?"
        rf"|(?:并且|且)(?=\s*(?:{field}){operator})|$)"
    )
    filters = {}
    position = 0
    while position < len(text):
        match = condition.match(text, position)
        if not match or match[1] in filters:
            raise ValueError(
                "请使用原列表中的可见列名和精确值，例如“列名=值”。"
            )
        filters[match[1]] = next(
            value for value in match.groups()[1:] if value is not None
        )
        position = match.end()
    if not filters:
        raise ValueError("请补充至少一个可见列条件。")
    return filters


def _card_page(result: ToolResultBlock) -> dict | None:
    if result.state != "success" or not re.fullmatch(
        r"query_recent_[a-zA-Z0-9_-]+", result.name
    ):
        return None
    page = _json_result(result)
    card = page.get("row_card")
    if (
        isinstance(card, dict)
        and card.get("version") == 1
        and isinstance(page.get("list_ref"), str)
    ):
        return page
    return None


def _conditions_for_page(text: str, page: dict) -> dict[str, str]:
    columns = page["row_card"].get("columns")
    if not isinstance(columns, list) or any(
        not isinstance(column, dict)
        or not isinstance(column.get("key"), str)
        or not isinstance(column.get("label"), str)
        for column in columns
    ):
        raise ValueError("无法读取当前列表的可见列，请重新查询。")
    return _visible_conditions(text, columns)


def _condition_call_ids(reply: Msg) -> set[str]:
    ids = set()
    for block in reply.get_content_blocks("tool_call"):
        if not re.fullmatch(r"open_[a-zA-Z0-9_-]+_row", block.name):
            continue
        try:
            args = json.loads(block.input)
        except ValueError:
            continue
        if isinstance(args, dict) and isinstance(args.get("conditions"), dict):
            ids.add(block.id)
    return ids


def _is_field_request(agent: Agent, text: str) -> bool:
    if _condition_text(text, quoted=False) is not None:
        return True
    if _condition_text(text, quoted=True) is None:
        return False
    last = next(_history_results(agent), None)
    return (
        last is not None
        and last.state == "success"
        and bool(re.fullmatch(r"open_[a-zA-Z0-9_-]+_row", last.name))
        and _json_result(last).get("row_match") == "ambiguous"
    )


class RowCardMiddleware(MiddlewareBase):
    """Resolve a selected list and call its registered row reader."""

    def __init__(
        self,
        get_message: Callable[[str], Awaitable[Msg | None]],
    ) -> None:
        super().__init__()
        self.get_message = get_message
        self.request: Msg | None = None

    async def on_reply(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable,
    ) -> AsyncGenerator:
        """Bind list selectors to the current user turn."""
        inputs = input_kwargs.get("inputs")
        messages = inputs if isinstance(inputs, list) else [inputs]
        self.request = next(
            (
                msg
                for msg in reversed(messages)
                if isinstance(msg, Msg)
                and msg.role == "user"
                and (
                    "row_card_reply" in msg.metadata
                    or _is_field_request(agent, msg.get_text_content())
                )
            ),
            None,
        )
        async with aclosing(next_handler(**input_kwargs)) as stream:
            async for event in stream:
                yield event

    async def _quoted_results(self, quote: Any) -> list[ToolResultBlock]:
        if not isinstance(quote, dict) or not all(
            isinstance(quote.get(key), str) and 0 < len(quote[key]) <= 256
            for key in ("message_id", "tool_call_id")
        ):
            raise ValueError("缺少原列表消息，请重新选择卡片。")
        msg = await self.get_message(quote["message_id"])
        if msg is None or msg.role != "assistant":
            return []
        return [
            result for result in msg.get_content_blocks("tool_result")
            if result.id == quote["tool_call_id"]
        ]

    async def _open(self, agent: Agent, call_id: str) -> ToolCallBlock | None:
        from ...business.row_references import OpenRowTool

        assert self.request is not None
        quoted = "row_card_reply" in self.request.metadata
        results = (
            await self._quoted_results(self.request.metadata["row_card_reply"])
            if quoted else _history_results(agent)
        )
        for result in results:
            page = _card_page(result)
            if page is None:
                continue
            name = result.name.removeprefix("query_recent_")
            tool = await agent.toolkit.get_tool(f"open_{name}_row")
            if not isinstance(tool, OpenRowTool):
                raise ValueError("当前会话没有这张卡片的只读详情工具。")
            text = self.request.get_text_content()
            args: dict[str, Any] = {"list_ref": page["list_ref"]}
            row = _row_number(text)
            if row:
                args["row_number"] = row
            else:
                selector = _condition_text(text, quoted=quoted)
                if selector is None and not quoted:
                    bare = _condition_text(text, quoted=True)
                    if bare is not None:
                        selector = bare[0], True
                if selector is None:
                    raise ValueError("请指定行号或可见列条件，例如“打开第二张”。")
                conditions = _conditions_for_page(selector[0], page)
                if selector[1]:
                    prior = next(
                        (
                            _json_result(block)
                            for block in _history_results(agent)
                            if block.name == tool.name
                        ),
                        {},
                    )
                    if (
                        prior.get("row_match") != "ambiguous"
                        or prior.get("list_ref") != page["list_ref"]
                        or not isinstance(prior.get("conditions"), dict)
                    ):
                        raise ValueError("请给出当前列表的完整匹配条件。")
                    conditions = {**prior["conditions"], **conditions}
                args["conditions"] = conditions
            return ToolCallBlock(
                id=call_id,
                name=tool.name,
                input=json.dumps(args),
            )
        if quoted:
            raise ValueError("在当前会话中找不到原列表，请重新查询。")
        return None

    async def on_model_call(
        self,
        agent: Agent,
        input_kwargs: dict,
        next_handler: Callable,
    ) -> Any:
        """Generate one checked tool call, including across a parked reply."""
        call_id = f"row_{agent.state.reply_id}"
        reply = agent.state.get_reply_message(agent.name)
        result = (
            next(
                (
                    block
                    for block in reply.get_content_blocks("tool_result")
                    if block.id == call_id
                ),
                None,
            )
            if reply
            else None
        )
        condition_calls = _condition_call_ids(reply) if reply else set()
        if reply and condition_calls:
            from ...business.row_references import OpenRowTool

            for item in reversed(reply.get_content_blocks("tool_result")):
                if item.id not in condition_calls or item.state != "success":
                    continue
                payload = _json_result(item)
                outcome = payload.get("row_match")
                if (
                    isinstance(outcome, str)
                    and outcome in _MATCH_FEEDBACK
                    and isinstance(payload.get("message"), str)
                    and isinstance(
                        await agent.toolkit.get_tool(item.name), OpenRowTool
                    )
                ):
                    return ChatResponse(
                        content=[TextBlock(text=payload["message"])], is_last=True
                    )
                break
        if result is not None:
            if result.state == "success":
                # Indent as literal text so upstream values cannot add links
                # or other markup to the detail message.
                payload = _json_result(result)
                if (
                    call_id in condition_calls
                    and payload.get("row_match") == "matched"
                ):
                    payload = payload["detail"]
                text = (
                    json.dumps(payload, ensure_ascii=False, indent=2)
                    if payload else _output(result)
                )
                text = "详情\n\n" + "\n".join(
                    "    " + line for line in text.splitlines()
                )
            else:
                text = "无法打开原单，请检查当前权限及账套，或重新查询已过期的列表。"
            block = TextBlock(text=text)
        elif self.request is None:
            return await next_handler(**input_kwargs)
        elif input_kwargs.get("tool_choice") == "none":
            block = TextBlock(text="本轮无法执行详情查询，请稍后重试。")
        else:
            try:
                block = await self._open(agent, call_id)
                if block is None:
                    return await next_handler(**input_kwargs)
            except ValueError as exc:
                block = TextBlock(text=str(exc))
        return ChatResponse(content=[block], is_last=True)
