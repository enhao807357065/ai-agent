"""RAG 分步验证入口：不会修改 corpus 中的任何资料。

使用方式：
  1. 初次建库并验证初始资料：
     PYTHONPATH=rag python rag/run_demo.py --stage initial

  2. 手工修改 corpus 后，只同步变更并验证当前资料：
     PYTHONPATH=rag python rag/run_demo.py --stage sync

  3. 只查询当前索引：
     PYTHONPATH=rag python rag/run_demo.py --stage query
"""

from __future__ import annotations

import argparse

from rag_demo import (
    QUESTION,
    answer_with_citations,
    format_build_observability,
    format_citation_evidence,
    format_hits,
    rebuild_indexes,
    retrieve_hybrid,
    retrieve_vector,
    sync_incremental,
)


def section(number: int, title: str) -> None:
    print(f"\n{'=' * 18} {number}. {title} {'=' * 18}")


def print_retrieval_and_answer(indexes) -> None:
    """展示向量、BM25、RRF 的 Top 5，并让 DeepSeek 基于 RRF 证据回答。"""
    vector_hits = retrieve_vector(indexes, QUESTION, top_k=5, login_only=True)
    bm25_hits = indexes.bm25.retrieve(QUESTION, top_k=5, login_only=True)
    hybrid_hits = retrieve_hybrid(indexes, QUESTION, top_k=5)

    section(2, "查询：向量、BM25、RRF 各自 Top 5")
    print("\n[vector]")
    print(format_hits(vector_hits))
    print("\n[bm25]")
    print(format_hits(bm25_hits))
    print("\n[rrf-hybrid]")
    print(format_hits(hybrid_hits))

    section(3, "真实模型基于 RRF 混合证据生成带来源引用的回答")
    print("[answer]")
    print(answer_with_citations(QUESTION, hybrid_hits))
    print("\n[citation-evidence：编号对应的来源和原文]")
    print(format_citation_evidence(hybrid_hits))


def run_initial() -> None:
    """步骤 1：初次全量建库；步骤 2、3：检索和带引用回答。"""
    section(1, "初次建库：同一批节点建立 Milvus 向量索引和 BM25")
    indexes = rebuild_indexes()
    print(format_build_observability(indexes.observability))
    print(f"nodes={len(indexes.nodes)}")
    sample = indexes.nodes[0]
    print(f"sample_source={sample.metadata['source_path']}")
    print(f"sample_text={sample.text[:240].replace(chr(10), ' ')}")
    print_retrieval_and_answer(indexes)


def run_sync() -> None:
    """步骤 4：同步手工修改后的资料；步骤 5：重新检索和回答。"""
    section(4, "同步手工修改后的 corpus：局部更新 Milvus 与当前 BM25")
    indexes = sync_incremental()
    print(format_build_observability(indexes.observability))
    print(f"nodes={len(indexes.nodes)}")
    print(
        "验收重点：embedded_text_count 仅应统计变更文件的新分块；"
        "overwrite=False 表示未删除整个 Milvus collection。"
    )

    section(5, "基于同步后的双路索引重新检索并生成带引用回答")
    vector_hits = retrieve_vector(indexes, QUESTION, top_k=5, login_only=True)
    bm25_hits = indexes.bm25.retrieve(QUESTION, top_k=5, login_only=True)
    hybrid_hits = retrieve_hybrid(indexes, QUESTION, top_k=5)
    print("\n[vector]")
    print(format_hits(vector_hits))
    print("\n[bm25]")
    print(format_hits(bm25_hits))
    print("\n[rrf-hybrid]")
    print(format_hits(hybrid_hits))
    print("\n[answer]")
    print(answer_with_citations(QUESTION, hybrid_hits))
    print("\n[citation-evidence：编号对应的来源和原文]")
    print(format_citation_evidence(hybrid_hits))


def run_query() -> None:
    """不改资料，只做一次同步检查后查询当前索引。"""
    section(1, "检查 corpus 变更并加载当前双路索引")
    indexes = sync_incremental()
    print(format_build_observability(indexes.observability))
    print_retrieval_and_answer(indexes)


def main() -> None:
    parser = argparse.ArgumentParser(description="RAG 建库、局部同步与检索验证")
    parser.add_argument(
        "--stage",
        choices=("initial", "sync", "query"),
        required=True,
        help="initial=首次全量建库；sync=手工修改 corpus 后局部同步；query=查询当前索引",
    )
    args = parser.parse_args()
    if args.stage == "initial":
        run_initial()
    elif args.stage == "sync":
        run_sync()
    else:
        run_query()


if __name__ == "__main__":
    main()
