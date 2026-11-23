# -*- coding: utf-8 -*-
"""Limit legacy enterprise import receipts in model input only."""
from collections.abc import Callable
from typing import Any

from ...agent import Agent
from ...message import Msg, TextBlock, ToolResultBlock
from ...middleware import MiddlewareBase


def _shorten_legacy_receipt(text: str) -> str:
    # Older tools emitted the full tools array. AgentScope may already have
    # truncated its JSON and added a pointer to the offloaded remainder.
    if len(text) <= 12_000 or not text.startswith('{"kind":'):
        return text
    if '"tools":' not in text[:1024] or '"registered":' in text[:100]:
        return text
    marker = "\n<<<TRUNCATED>>>\n<system-reminder>"
    _, separator, reminder = text.rpartition(marker)
    note = "旧版企业业务注册回执的详细清单已省略。可用 list_enterprise_skills 核对当前已注册功能。"
    if separator and len(reminder) < 1024:
        note += "\n<system-reminder>" + reminder
    return note


def _model_message(message: Msg) -> Msg:
    content = message.content.copy()
    for i, block in enumerate(content):
        if not isinstance(block, ToolResultBlock) or (
            block.name != "register_enterprise_business"
        ):
            continue
        if isinstance(block.output, str):
            short = _shorten_legacy_receipt(block.output)
            if short != block.output:
                content[i] = block.model_copy(update={"output": short})
        else:
            output = block.output.copy()
            for j, part in enumerate(output):
                if isinstance(part, TextBlock):
                    short = _shorten_legacy_receipt(part.text)
                    if short != part.text:
                        output[j] = part.model_copy(update={"text": short})
            if any(a is not b for a, b in zip(output, block.output)):
                content[i] = block.model_copy(update={"output": output})
    if any(a is not b for a, b in zip(content, message.content)):
        return message.model_copy(update={"content": content})
    return message


class EnterpriseReceiptMiddleware(MiddlewareBase):
    """Keep the original transcript and tool-call IDs available to chat."""

    async def on_model_call(
        self, agent: Agent, input_kwargs: dict, next_handler: Callable
    ) -> Any:
        return await next_handler(
            **{
                **input_kwargs,
                "messages": [
                    _model_message(m) for m in input_kwargs["messages"]
                ],
            }
        )
