"""Rank RecordStore retrieval candidates with a dedicated reranker model.

A reranker (cross-encoder) scores query/document relevance. It is not the same
capability as the default model rerank, which judges usefulness and drops
candidates. Here the reranker owns ordering and RecordStore.retrieve(...) keeps
policy, packaging and diagnostics.

Run against any Cohere/Jina-shaped /rerank endpoint:

    RERANK_BASE_URL=http://127.0.0.1:8000/v1 \
    RERANK_API_KEY=... \
    RERANK_MODEL=bge-reranker-v2-m3 \
    python examples/record_store/record_store_rerank_provider.py
"""

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.base import record_store_registry  # noqa: E402


NOTES = [
    "本地模型接入：通过 settings 配置 OpenAI 兼容的 base_url 与 api_key",
    "团建活动安排：周六下午两点在公司楼下集合",
    "报销流程：发票拍照上传后由主管审批",
    "重排模型 bge-reranker-v2-m3 用于检索候选的相关性打分",
]
QUERY = "怎么给框架接本地模型服务"


def build_rerank_agent() -> Any:
    """One Agent configured as a rerank model client; no chat settings involved."""
    agent = Agently.create_agent()
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ["RERANK_BASE_URL"],
            "api_key": os.getenv("RERANK_API_KEY", ""),
            "model_type": "rerank",
            "model": os.environ["RERANK_MODEL"],
        },
    )
    return agent


async def main() -> dict[str, object]:
    with TemporaryDirectory(prefix="agently-record-store-rerank-") as temp_dir:
        record_store = record_store_registry.create(
            temp_dir,
            mode="read_write",
            rerank_provider="agent",
            rerank_options={"agent": build_rerank_agent()},
        )
        for note in NOTES:
            await record_store.put(
                {"note": note},
                collection="notes",
                kind="team_note",
                summary=note,
                indexed=True,
            )

        package = await record_store.retrieve(QUERY, rerank=True)
        diagnostics = cast(dict[str, Any], package["diagnostics"]["rerank"])
        ordered = [str(item.get("summary")) for item in package["items"]]
        scores = [item.get("score") for item in package["items"]]
        result: dict[str, object] = {
            "engine": diagnostics["engine"],
            "provider": diagnostics["provider"],
            "scored": diagnostics["scored"],
            "unscored": diagnostics["unscored"],
            "dropped": diagnostics["dropped"],
            "top_note": ordered[0],
            "top_score_above_rest": bool(
                scores[0] is not None and scores[1] is not None and scores[0] > scores[1]
            ),
            "order": ordered,
        }
        # Structure and ownership are deterministic facts; which note ranks first
        # is the reranker model's judgment and is printed, not asserted by keyword.
        assert result["engine"] == "provider"
        assert result["dropped"] == 0
        assert result["unscored"] == 0
        assert result["scored"] == len(NOTES)
        return result


if __name__ == "__main__":
    print(asyncio.run(main()))


# Expected key output (real run, oMLX bge-reranker-v2-m3, 2026-09-14):
# engine: provider
# provider: agent
# scored: 4
# unscored: 0
# dropped: 0
# top_note: 本地模型接入：通过 settings 配置 OpenAI 兼容的 base_url 与 api_key
# top_score_above_rest: True
# observed scores: 0.8825, 0.0023, 1.64e-05, 1.62e-05
#
# Working principle:
#   retrieve() builds candidates -> bound RerankProvider scores every candidate
#   -> retrieve() sorts by score and keeps packaging/budget ownership.
# Relevance scores use the model's own scale: comparable inside one response,
# not across models. Nothing is dropped unless the caller passes an explicit
# budget={"rerank_score_floor": ...}.
