"""初次全量建库：仅在没有现有索引时使用。不会修改 corpus。"""

from rag_demo import format_build_observability, rebuild_indexes


if __name__ == "__main__":
    indexes = rebuild_indexes()
    print(format_build_observability(indexes.observability))
    print(f"nodes={len(indexes.nodes)}")
