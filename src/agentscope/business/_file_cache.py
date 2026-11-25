# -*- coding: utf-8 -*-
"""Reuse data derived from files while their filesystem identity is unchanged."""

from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Generic, NamedTuple, TypeVar

T = TypeVar("T")


class FileStamp(NamedTuple):
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


def file_stamp(path: Path, *, follow_symlinks: bool = True) -> FileStamp:
    stat = path.stat(follow_symlinks=follow_symlinks)
    return FileStamp(
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


class FileCache(Generic[T]):
    """Keep a bounded set of read-only derived values in process memory."""

    def __init__(
        self,
        load: Callable[[Path], T],
        capacity: int = 256,
        *,
        max_bytes: int | None = None,
    ) -> None:
        self._load = load
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._bytes = 0
        self._entries: OrderedDict[Path, tuple[FileStamp, T]] = OrderedDict()
        self._lock = RLock()

    def get(self, path: Path) -> T:
        with self._lock:
            for _ in range(2):
                stamp = file_stamp(path)
                cached = self._entries.get(path)
                if cached is not None and cached[0] == stamp:
                    self._entries.move_to_end(path)
                    return cached[1]
                if cached is not None:
                    del self._entries[path]
                    self._bytes -= cached[0].size
                value = self._load(path)
                if file_stamp(path) == stamp:
                    if (
                        self._max_bytes is not None
                        and stamp.size > self._max_bytes
                    ):
                        return value
                    self._entries[path] = (stamp, value)
                    self._bytes += stamp.size
                    while len(self._entries) > self._capacity or (
                        self._max_bytes is not None
                        and self._bytes > self._max_bytes
                    ):
                        _, (removed, _) = self._entries.popitem(last=False)
                        self._bytes -= removed.size
                    return value
            raise OSError("File changed during both read attempts.")


_receipt_paths = FileCache(
    lambda directory: tuple(sorted(directory.glob("*.json")))
)


def json_paths(directory: Path) -> tuple[Path, ...]:
    """List receipt paths using the directory's stamp as the version."""
    try:
        return _receipt_paths.get(directory)
    except FileNotFoundError:
        return ()
