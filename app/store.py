"""审计结果存储：进程内字典 + data/audits/<id>.json 落盘。

用 JSON 文件而不是数据库，是为了让「一次运行的完整证据」可以直接被人打开看，
也避免为了一个作品集项目引入 SQLite/Redis 之类的额外依赖。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .pipeline import AuditResult


class AuditStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, AuditResult] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for path in sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            result = AuditResult(
                id=payload["id"],
                trace_id=payload.get("traceId", ""),
                status=payload.get("status", "done"),
                created_at=payload.get("createdAt", ""),
                duration_ms=float(payload.get("durationMs", 0)),
                dataset=payload.get("dataset", {}),
                traces=payload.get("traces", []),
                report=payload.get("report"),
                llm=payload.get("llm", {}),
                error=payload.get("error"),
            )
            self._items[result.id] = result
            self._order.append(result.id)

    def save(self, result: AuditResult) -> None:
        with self._lock:
            self._items[result.id] = result
            if result.id not in self._order:
                self._order.append(result.id)
        payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        (self._dir / f"{result.id}.json").write_text(payload, encoding="utf-8")

    def get(self, audit_id: str) -> AuditResult | None:
        with self._lock:
            return self._items.get(audit_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            return [self._items[i].summary() for i in ids if i in self._items]

    def count(self) -> int:
        with self._lock:
            return len(self._items)
