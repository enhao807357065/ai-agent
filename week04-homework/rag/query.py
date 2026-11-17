"""查询当前索引：输出三路检索结果和 DeepSeek 的带引用回答。"""

from rag_demo import QUESTION, answer_with_citations, format_hits, retrieve_hybrid, retrieve_vector, sync_incremental


if __name__ == "__main__":
    indexes = sync_incremental()
    vector_hits = retrieve_vector(indexes, QUESTION, top_k=5, login_only=True)
    bm25_hits = indexes.bm25.retrieve(QUESTION, top_k=5, login_only=True)
    hybrid_hits = retrieve_hybrid(indexes, QUESTION, top_k=5)

    print("[vector]\n" + format_hits(vector_hits))
    print("\n[bm25]\n" + format_hits(bm25_hits))
    print("\n[rrf-hybrid]\n" + format_hits(hybrid_hits))
    print("\n[answer]\n" + answer_with_citations(QUESTION, hybrid_hits))
