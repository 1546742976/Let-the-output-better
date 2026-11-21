"""把页面上的 6 个示例跑一遍 Python 实现，产出 examples_reference.json 供 TS 侧比对。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["OPTIMIZE_ENV_FILE"] = "no-such-env-file.env"
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.optimizer import optimize  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.settings import PipelineConfig, Settings, default_steps  # noqa: E402


async def main() -> None:
    examples = json.loads((ROOT / "sites" / "dev" / "examples.json").read_text(encoding="utf-8"))
    settings = Settings()
    config = PipelineConfig(steps=default_steps(), audit=False)
    out: list[dict] = []
    for example in examples:
        result = await optimize(
            example["raw_output"],
            schema=example["schema"] or None,
            instruction=example["instruction"],
            input_text=example["input_text"],
            provider=MockProvider(),
            settings=settings,
            config=config,
            reference_style=example["reference_style"] or None,
            request_id="req_example",
        )
        payload = result.as_response()
        for key in ("dimensions", "markers", "changes", "errors", "degraded_reason"):
            payload[key] = getattr(result, key)
        out.append({"label": example["label"], "result": payload})
    (ROOT / "sites" / "dev" / "examples_reference.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"examples={len(out)} -> sites/dev/examples_reference.json")


if __name__ == "__main__":
    asyncio.run(main())
