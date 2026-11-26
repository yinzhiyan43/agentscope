"""Match documented HTTP and JSON business response conditions."""

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


def at_path(payload: object, path: str) -> Any:
    """Read a dotted path in a JSON object."""
    for segment in path.split("."):
        if not isinstance(payload, dict):
            return None
        payload = payload.get(segment)
    return payload


def json_object(response: httpx.Response) -> dict:
    """Read optional JSON while preserving the HTTP status for
    classification."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


class ResponseMatch(BaseModel):
    """Conjunction of status and exact JSON conditions supplied by the
    agent."""

    model_config = ConfigDict(extra="forbid")

    status: int | None = Field(default=None, ge=100, le=599)
    fields: dict[str, str | int | bool] = Field(default_factory=dict)

    def matches(self, response: httpx.Response) -> bool:
        """A match needs at least one explicit condition."""
        if self.status is not None and response.status_code != self.status:
            return False
        payload = json_object(response)
        return (self.status is not None or bool(self.fields)) and all(
            (actual := at_path(payload, key)) == value
            and type(actual) is type(value)
            for key, value in self.fields.items()
        )
