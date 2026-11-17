from __future__ import annotations

from llama_index.core.schema import NodeWithScore, TextNode

from rag_demo import format_citation_evidence


def test_citation_evidence_shows_number_source_and_original_text() -> None:
    hits = [
        NodeWithScore(
            node=TextNode(
                text="文档规则：登录会话有效期为 20 分钟。",
                metadata={"source_path": "docs/session-rules.md"},
            ),
            score=0.1,
        ),
        NodeWithScore(
            node=TextNode(
                text="SESSION_TTL_SECONDS = 25 * 60",
                metadata={"source_path": "app/session.py"},
            ),
            score=0.1,
        ),
    ]

    evidence = format_citation_evidence(hits)

    assert "[1] docs/session-rules.md" in evidence
    assert "登录会话有效期为 20 分钟" in evidence
    assert "[2] app/session.py" in evidence
    assert "SESSION_TTL_SECONDS = 25 * 60" in evidence
