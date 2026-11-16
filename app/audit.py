"""JSONL 审计留痕：写入失败仅告警，不影响主流程返回。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, record: dict[str, Any]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("审计写入失败：%s（%s）", self.path, exc)
            return False
