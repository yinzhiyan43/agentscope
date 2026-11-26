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
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("List title must be a nonempty string.")
        keys = set()
        names = {}
        for column in self.columns:
            if not isinstance(column, ListColumn) or not all(
                isinstance(name, str) and name.strip()
                for name in (column.key, column.label)
            ):
                raise ValueError("List columns need a nonempty key and label.")
            if column.key in keys:
                raise ValueError("Duplicate list column key.")
            keys.add(column.key)
            for name in (column.key, column.label):
                if names.setdefault(name, column.key) != column.key:
                    raise ValueError(
                        "List column names must identify a single column."
                    )

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
