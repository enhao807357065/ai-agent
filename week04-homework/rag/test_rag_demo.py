from __future__ import annotations

from pathlib import Path

from rag_demo import build_nodes, format_build_observability, load_documents, rrf_fuse


def test_build_nodes_keeps_source_metadata(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    docs = corpus / "docs"
    docs.mkdir(parents=True)
    (docs / "session-rules.md").write_text(
        "# 登录会话\n\n登录会话有效期为 30 分钟。\n",
        encoding="utf-8",
    )

    nodes = build_nodes(load_documents(corpus), chunk_size=64)

    assert nodes
    assert nodes[0].metadata["source_path"] == "docs/session-rules.md"
    assert "30 分钟" in nodes[0].text


def test_rrf_fuse_prefers_node_returned_by_both_retrievers() -> None:
    assert rrf_fuse([["vector-only", "shared"], ["shared", "bm25-only"]], top_k=3) == [
        "shared",
        "vector-only",
        "bm25-only",
    ]


def test_rechunked_nodes_reflect_manual_source_change(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    docs = corpus / "docs"
    app = corpus / "app"
    docs.mkdir(parents=True)
    app.mkdir()
    rules = docs / "session-rules.md"
    rules.write_text("# 规则\n\n登录会话有效期为 30 分钟。", encoding="utf-8")
    (app / "session.py").write_text("SESSION_TTL_SECONDS = 30 * 60\n", encoding="utf-8")
    (docs / "faq.md").write_text("默认 30 分钟", encoding="utf-8")
    (docs / "testing-guide.md").write_text("推后 30 分钟", encoding="utf-8")

    before = build_nodes(load_documents(corpus), chunk_size=64)
    rules.write_text("# 规则\n\n登录会话有效期为 20 分钟。", encoding="utf-8")
    after = build_nodes(load_documents(corpus), chunk_size=64)

    before_text = "\n".join(node.text for node in before)
    after_text = "\n".join(node.text for node in after)
    after_rules_text = "\n".join(
        node.text for node in after if node.metadata["source_path"] == "docs/session-rules.md"
    )
    assert "30 分钟" in before_text
    assert "30 分钟" not in after_rules_text
    assert "20 分钟" in after_rules_text


def test_observability_report_includes_model_and_milvus_evidence() -> None:
    report = format_build_observability(
        {
            "provider": "FastEmbed",
            "model": "BAAI/bge-small-zh-v1.5",
            "dimension": 512,
            "model_load_seconds": 1.25,
            "embedding_calls": 2,
            "embedded_text_count": 17,
            "embedding_seconds": 0.5,
            "milvus_uri": "/tmp/milvus.db",
            "collection_name": "session_chunks",
            "overwrite": True,
            "row_count_after_insert": 17,
        }
    )

    assert "provider=FastEmbed" in report
    assert "model=BAAI/bge-small-zh-v1.5" in report
    assert "dimension=512" in report
    assert "embedded_text_count=17" in report
    assert "row_count_after_insert=17" in report


def test_rechunked_nodes_do_not_keep_the_old_ttl(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    docs = corpus / "docs"
    app = corpus / "app"
    docs.mkdir(parents=True)
    app.mkdir()
    rules = docs / "session-rules.md"
    rules.write_text("# 规则\n\n登录会话有效期为 30 分钟。", encoding="utf-8")
    (app / "session.py").write_text("SESSION_TTL_SECONDS = 30 * 60\n", encoding="utf-8")
    (docs / "faq.md").write_text("默认 30 分钟", encoding="utf-8")
    (docs / "testing-guide.md").write_text("推后 30 分钟", encoding="utf-8")

    before = build_nodes(load_documents(corpus), chunk_size=64)
    rules.write_text("# 规则\n\n登录会话有效期为 20 分钟。", encoding="utf-8")
    after = build_nodes(load_documents(corpus), chunk_size=64)

    before_text = "\n".join(node.text for node in before)
    after_text = "\n".join(node.text for node in after)
    after_rules_text = "\n".join(
        node.text for node in after if node.metadata["source_path"] == "docs/session-rules.md"
    )
    assert "30 分钟" in before_text
    assert "30 分钟" not in after_rules_text
    assert "20 分钟" in after_rules_text
