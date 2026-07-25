"""Tiny file-backed response cache with TTL — keeps dev reruns from hammering Timely.

This is a DEVELOPMENT aid, not a production politeness measure. In CI it can never hit:
runners start clean, .cache/ is gitignored, and every key is requested exactly once per
run — so all it did there was write ~92 MB to a disk thrown away minutes later. The sync
workflow sets KIDA_CACHE_TTL_SECONDS=0 to disable it. Politeness in production comes from
the request pacer and the de-duplication in fetch_availability, which cut requests at the
source rather than caching stale availability into a product whose whole value is freshness.

Keys are namespaced by SCHEMA_VERSION so a change to the key shape orphans old entries
loudly (they are simply never read) instead of serving responses recorded under different
semantics. Bump it whenever a cache key's meaning changes.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

SCHEMA_VERSION = "v2"      # v2: keys are namespaced by service id (see TimelyClient.cache_ns)


class ResponseCache:
    def __init__(self, directory: str = ".cache", ttl_seconds: int = 900, enabled: bool = True):
        self.dir = Path(directory) / SCHEMA_VERSION
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
        try:
            if time.time() - p.stat().st_mtime > self.ttl:
                return None
            return p.read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, key: str, value: str):
        if not self.enabled:
            return
        # Write-then-rename: an interrupted write previously left a truncated file that
        # would be served as valid for the rest of the TTL.
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(value, encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            tmp.unlink(missing_ok=True)
