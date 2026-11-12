"""Read local task sources and return a TaskBoard report without a file artifact.

Configure OMLX_BASE_URL, OMLX_API_KEY and optionally OMLX_MODEL in the environment.
Expected key output from a real local Qwen3.8-27B-4bit run on 2026-09-29:
K-11=27 (not below 20), M-24=14 (below 15), R-08=8 (below 10).
The final report stayed inline; the workspace contained only the two input CSVs.
Source files are synthetic business data; planning and calculations use the model.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import find_dotenv, load_dotenv

from agently import Agently


async def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))
    with TemporaryDirectory(prefix="agently-inventory-") as directory:
        workspace = Path(directory)
        (workspace / 'opening.csv').write_text('sku,opening,reorder_level\nK-11,40,20\nM-24,25,15\nR-08,12,10\n', encoding="utf-8")
        (workspace / 'movements.csv').write_text('id,sku,kind,qty,status\nt1,K-11,out,18,posted\nt2,K-11,in,5,posted\nt3,M-24,out,14,posted\nt4,M-24,return,3,posted\nt5,R-08,out,6,void\nt6,R-08,out,4,posted\nt7,K-11,out,9,void\n', encoding="utf-8")
        agent = Agently.create_agent().use_task_workspace(workspace)
        agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
            "base_url": os.environ["OMLX_BASE_URL"],
            "auth": os.environ["OMLX_API_KEY"],
            "model": os.getenv("OMLX_MODEL", "Qwen3.8-27B-4bit"),
            "stream": False,
            "request_options": {"temperature": 0, "max_tokens": 8192,
                                "chat_template_kwargs": {"enable_thinking": False}},
        })

        @agent.action_func
        def read_document(path: str) -> dict[str, str]:
            """Read a supplied task document by relative filename; no external data."""
            target = (workspace / path).resolve()
            target.relative_to(workspace.resolve())
            return {"path": path, "content": target.read_text(encoding="utf-8")}

        execution = (
            agent.goal(
                '核对仓库期末库存。读取 opening.csv 和 movements.csv，以期初加有效入库/退回、减有效出库计算每个SKU的期末库存；status=void的流水不计。用Markdown表格给出SKU、期末库存、补货线，以及是否低于补货线。补充简短处理口径，并引用读取的文件名。直接返回报告正文，不需要创建文件。',
                success_criteria=['报告所有SKU的正确期末库存和是否低于补货线。', '计算基于实际读取的两份资料；作废流水不计，退回计入库存。'],
            )
            .use_actions([read_document])
            .strategy("taskboard")
        )
        result = await execution.async_start()
        print(result)
        print("Workspace files:", sorted(str(path.relative_to(workspace))
                                         for path in workspace.rglob("*") if path.is_file()))


if __name__ == "__main__":
    asyncio.run(main())
