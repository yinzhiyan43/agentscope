# -*- coding: utf-8 -*-
"""An ordered, authorized display projection for a business list."""

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ListColumn:
    """One visible client column and its display label."""

    key: str
    label: str


@dataclass(frozen=True)
class ListLayout:
    """Columns resolved for the current upstream user by a host connector."""

    title: str
    columns: tuple[ListColumn, ...]

    def __post_init__(self) -> None:
        keys = [column.key for column in self.columns]
        if (
            not self.title
            or any(not key for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("List columns must have unique, nonempty keys.")

    def project(self, row: dict) -> dict:
        """Expose visible scalar cells after connector normalization."""
        cells = {}
        for column in self.columns:
            value = row.get(column.key)
            cells[column.key] = (
                value if type(value) in (str, bool, int, float) else None
            )
        return cells

    def card(self) -> dict:
        """Serialize the same ordered layout used to project the payload."""
        return {
            "version": 1,
            "title": self.title,
            "columns": [
                {"key": column.key, "label": column.label}
                for column in self.columns
            ],
        }

    def present(self, records: Iterable[dict], total: int) -> dict:
        """Create a display-only result for any business read tool."""
        return {
            "total": total,
            "records": [self.project(row) for row in records],
            "row_card": self.card(),
        }
