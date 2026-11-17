"""同步手工修改后的 corpus：仅更新新增、修改、删除的源文件。"""

from rag_demo import format_build_observability, sync_incremental


if __name__ == "__main__":
    indexes = sync_incremental()
    print(format_build_observability(indexes.observability))
    print(f"nodes={len(indexes.nodes)}")
