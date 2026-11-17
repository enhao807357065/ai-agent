from __future__ import annotations

from pathlib import Path

from rag_demo import build_nodes, diff_documents, load_documents


def write_doc(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_nodes_have_stable_ids_for_unchanged_document(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    write_doc(corpus / "docs" / "session.md", "# 会话\n\n有效期为 30 分钟。")

    first = build_nodes(load_documents(corpus))
    second = build_nodes(load_documents(corpus))

    assert [node.node_id for node in first] == [node.node_id for node in second]


def test_document_diff_only_marks_changed_added_and_deleted_sources(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    write_doc(corpus / "docs" / "changed.md", "旧内容")
    write_doc(corpus / "docs" / "unchanged.md", "保持不变")
    before = load_documents(corpus)
    old_manifest = {
        document.metadata["source_path"]: document.metadata["content_hash"]
        for document in before
    }

    write_doc(corpus / "docs" / "changed.md", "新内容")
    (corpus / "docs" / "unchanged.md").unlink()
    write_doc(corpus / "docs" / "added.md", "新增内容")

    diff = diff_documents(load_documents(corpus), old_manifest)

    assert diff["changed"] == {"docs/changed.md"}
    assert diff["added"] == {"docs/added.md"}
    assert diff["deleted"] == {"docs/unchanged.md"}
    assert diff["unchanged"] == set()
