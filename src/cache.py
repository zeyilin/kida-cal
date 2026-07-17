"""Tiny file-backed response cache with TTL — keeps dev reruns from hammering Timely."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path


class ResponseCache:
    def __init__(self, directory: str = ".cache", ttl_seconds: int = 900, enabled: bool = True):
        self.dir = Path(directory)
        self.ttl = ttl_seconds
        self.enabled = enabled and ttl_seconds > 0
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest() + ".txt")

    def get(self, key: str):
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self.ttl:
            return None
        return p.read_text(encoding="utf-8")

    def put(self, key: str, value: str):
        if not self.enabled:
            return
        self._path(key).write_text(value, encoding="utf-8")
