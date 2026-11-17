"""RAG 验证：LlamaIndex 节点、Milvus Lite 向量索引、BM25 与 RRF。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import jieba
from llama_index.core import Document, Settings, StorageContext, VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import NodeWithScore, TextNode
from llama_index.core.vector_stores import SimpleVectorStore
from llama_index.embeddings.openai_like import OpenAILikeEmbedding
from rank_bm25 import BM25Okapi

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CORPUS = PROJECT_ROOT.parent / "corpus"
DEFAULT_STORAGE = PROJECT_ROOT / "storage"
DEFAULT_MILVUS_URI = str(PROJECT_ROOT / "milvus_login_demo.db")
DEFAULT_COLLECTION = "login_session_chunks"
QUESTION = "登录会话的有效期是多少分钟？"


@dataclass
class BuiltIndexes:
    nodes: list[TextNode]
    vector_index: VectorStoreIndex | None
    bm25: "ChineseBM25Retriever"
    storage_dir: Path
    milvus_uri: str
    collection_name: str
    observability: dict[str, object]


class LocalBGEEmbedding(BaseEmbedding):
    """使用本地 FastEmbed 的 BAAI/bge-small-zh-v1.5 生成真实向量。"""

    model_name: str = "BAAI/bge-small-zh-v1.5"
    _model: object | None = None
    load_seconds: float = 0.0
    embed_calls: int = 0
    embedded_text_count: int = 0
    embedding_seconds: float = 0.0

    def _get_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            started = time.perf_counter()
            self._model = TextEmbedding(self.model_name)
            self.load_seconds = time.perf_counter() - started
        return self._model

    def _embed_passages(self, texts: list[str]) -> list[list[float]]:
        started = time.perf_counter()
        vectors = [
            [float(value) for value in vector]
            for vector in self._get_model().passage_embed(texts)
        ]
        self.embed_calls += 1
        self.embedded_text_count += len(texts)
        self.embedding_seconds += time.perf_counter() - started
        return vectors

    def _get_query_embedding(self, query: str) -> list[float]:
        started = time.perf_counter()
        vector = list(self._get_model().query_embed([query]))[0]
        self.embed_calls += 1
        self.embedded_text_count += 1
        self.embedding_seconds += time.perf_counter() - started
        return [float(value) for value in vector]

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed_passages([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed_passages(texts)

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> list[float]:
        return self._get_text_embedding(text)


def load_documents(corpus_dir: Path) -> list[Document]:
    """读取 corpus 中 Markdown 与 Python 文件，并携带可引用来源路径。"""
    documents: list[Document] = []
    for path in sorted(corpus_dir.rglob("*")):
        if path.suffix not in {".md", ".py"}:
            continue
        relative_path = str(path.relative_to(corpus_dir))
        text = path.read_text(encoding="utf-8")
        frontmatter, body = _split_frontmatter(text)
        documents.append(
            Document(
                text=body,
                metadata={
                    "source_path": relative_path,
                    "source_type": "code" if path.suffix == ".py" else "document",
                    "service": frontmatter.get("service", "login_demo" if relative_path.startswith("app/") else "unknown"),
                    "lifecycle": "historical" if relative_path.startswith("history/") else "current",
                    "content_hash": _content_hash(text),
                },
                id_=relative_path,
            )
        )
    return documents


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def diff_documents(
    documents: Sequence[Document], old_hashes: dict[str, str]
) -> dict[str, set[str]]:
    """按 source_path + 内容 hash 分类新增、修改、删除和未变资料。"""
    current_hashes = {
        str(document.metadata["source_path"]): str(document.metadata["content_hash"])
        for document in documents
    }
    current_paths = set(current_hashes)
    old_paths = set(old_hashes)
    return {
        "added": current_paths - old_paths,
        "changed": {
            path for path in current_paths & old_paths if current_hashes[path] != old_hashes[path]
        },
        "deleted": old_paths - current_paths,
        "unchanged": {
            path for path in current_paths & old_paths if current_hashes[path] == old_hashes[path]
        },
    }


def build_nodes(documents: Iterable[Document], chunk_size: int = 220) -> list[TextNode]:
    """同一批 Document 仅切分一次，供向量索引和 BM25 索引共同使用。"""
    splitter = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=0,
        include_metadata=False,
    )
    nodes: list[TextNode] = []
    for document in documents:
        split_source = Document(text=document.text, id_=document.doc_id)
        for node in splitter.get_nodes_from_documents([split_source]):
            node.metadata = dict(document.metadata)
            source_path = str(node.metadata["source_path"])
            node.node_id = f"{source_path}:{len(nodes)}:{_content_hash(node.text)[:12]}"
            nodes.append(node)
    return nodes


def tokenize(text: str) -> list[str]:
    return [token for token in jieba.lcut(text.lower()) if token.strip()]


class ChineseBM25Retriever:
    """确保语料与查询都使用 jieba 分词，避免中文 BM25 的双端分词不一致。"""

    def __init__(self, nodes: Sequence[TextNode]) -> None:
        self.nodes = list(nodes)
        self._bm25 = BM25Okapi([tokenize(node.text) for node in self.nodes])

    def retrieve(self, query: str, top_k: int = 5, login_only: bool = False) -> list[NodeWithScore]:
        scores = self._bm25.get_scores(tokenize(query))
        ranked_indexes = sorted(range(len(scores)), key=lambda index: scores[index], reverse=True)
        results: list[NodeWithScore] = []
        for index in ranked_indexes:
            node = self.nodes[index]
            if login_only and not _is_current_login_node(node):
                continue
            results.append(NodeWithScore(node=node, score=float(scores[index])))
            if len(results) == top_k:
                break
        return results


def _is_current_login_node(node: TextNode) -> bool:
    return (
        node.metadata.get("service") == "login_demo"
        and node.metadata.get("lifecycle") == "current"
    )


def rrf_fuse(
    result_lists: Iterable[Sequence[NodeWithScore] | Sequence[str]],
    top_k: int = 5,
    k: int = 60,
) -> list[NodeWithScore] | list[str]:
    """RRF 只融合名次，避免直接比较向量分与 BM25 原始分。"""
    ranked_lists = [list(results) for results in result_lists]
    has_nodes = bool(ranked_lists and ranked_lists[0] and isinstance(ranked_lists[0][0], NodeWithScore))
    scores: dict[str, float] = {}
    items: dict[str, NodeWithScore | str] = {}
    for results in ranked_lists:
        for rank, item in enumerate(results, start=1):
            key = item.node.node_id if isinstance(item, NodeWithScore) else item
            scores[key] = scores.get(key, 0.0) + 1 / (k + rank)
            items.setdefault(key, item)
    ordered_pairs = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:top_k]
    if has_nodes:
        return [
            NodeWithScore(node=items[key].node, score=score)  # type: ignore[union-attr]
            for key, score in ordered_pairs
        ]
    return [str(items[key]) for key, _ in ordered_pairs]


def rebuild_indexes(
    corpus_dir: Path = DEFAULT_CORPUS,
    storage_dir: Path = DEFAULT_STORAGE,
    milvus_uri: str = DEFAULT_MILVUS_URI,
    collection_name: str = DEFAULT_COLLECTION,
    use_milvus: bool = True,
) -> BuiltIndexes:
    """删除旧索引后，从当前 corpus 重新切分同一批节点并建立两路索引。"""
    nodes = build_nodes(load_documents(corpus_dir))
    storage_dir.mkdir(parents=True, exist_ok=True)
    _write_node_snapshot(nodes, storage_dir / "nodes.json")
    _write_manifest(load_documents(corpus_dir), nodes, storage_dir / "manifest.json")

    embed_model = LocalBGEEmbedding()
    Settings.embed_model = embed_model
    vector_index: VectorStoreIndex | None = None
    active_milvus_uri = milvus_uri

    row_count_after_insert: int | str = "not-checked"
    if use_milvus:
        try:
            from llama_index.vector_stores.milvus import MilvusVectorStore

            db_path = Path(milvus_uri)
            if db_path.is_dir():
                shutil.rmtree(db_path)
            elif db_path.exists():
                db_path.unlink()
            vector_store = MilvusVectorStore(
                uri=milvus_uri,
                collection_name=collection_name,
                dim=512,
                overwrite=True,
                similarity_metric="COSINE",
            )
            vector_index = VectorStoreIndex(
                nodes,
                storage_context=StorageContext.from_defaults(vector_store=vector_store),
                embed_model=embed_model,
            )
            row_count_after_insert = vector_store.client.get_collection_stats(
                collection_name=collection_name
            )["row_count"]
        except Exception as error:
            raise RuntimeError(f"Milvus Lite 建库失败：{error}") from error
    else:
        vector_index = VectorStoreIndex(
            nodes,
            storage_context=StorageContext.from_defaults(vector_store=SimpleVectorStore()),
            embed_model=embed_model,
        )
        active_milvus_uri = "in-memory-test"
        row_count_after_insert = len(nodes)

    observability: dict[str, object] = {
        "provider": "FastEmbed",
        "model": embed_model.model_name,
        "dimension": 512,
        "model_load_seconds": round(embed_model.load_seconds, 3),
        "embedding_calls": embed_model.embed_calls,
        "embedded_text_count": embed_model.embedded_text_count,
        "embedding_seconds": round(embed_model.embedding_seconds, 3),
        "milvus_uri": active_milvus_uri,
        "collection_name": collection_name,
        "overwrite": use_milvus,
        "row_count_after_insert": row_count_after_insert,
    }

    return BuiltIndexes(
        nodes=nodes,
        vector_index=vector_index,
        bm25=ChineseBM25Retriever(nodes),
        storage_dir=storage_dir,
        milvus_uri=active_milvus_uri,
        collection_name=collection_name,
        observability=observability,
    )


def sync_incremental(
    corpus_dir: Path = DEFAULT_CORPUS,
    storage_dir: Path = DEFAULT_STORAGE,
    milvus_uri: str = DEFAULT_MILVUS_URI,
    collection_name: str = DEFAULT_COLLECTION,
) -> BuiltIndexes:
    """仅同步新增、修改、删除的源文件；未变化节点不重新生成向量。"""
    manifest_path = storage_dir / "manifest.json"
    snapshot_path = storage_dir / "nodes.json"
    if not manifest_path.exists() or not snapshot_path.exists():
        raise RuntimeError("缺少初始 manifest/nodes 快照；请先执行一次 rebuild_indexes()")

    documents = load_documents(corpus_dir)
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["documents"]
    old_hashes = {path: item["content_hash"] for path, item in old_manifest.items()}
    changes = diff_documents(documents, old_hashes)
    touched_paths = changes["added"] | changes["changed"] | changes["deleted"]
    documents_by_path = {str(document.metadata["source_path"]): document for document in documents}
    new_documents = [documents_by_path[path] for path in changes["added"] | changes["changed"]]
    new_nodes = build_nodes(new_documents)
    old_node_ids = [
        node_id
        for source_path in changes["changed"] | changes["deleted"]
        for node_id in old_manifest[source_path]["node_ids"]
    ]

    from llama_index.vector_stores.milvus import MilvusVectorStore

    embed_model = LocalBGEEmbedding()
    Settings.embed_model = embed_model
    vector_store = MilvusVectorStore(
        uri=milvus_uri,
        collection_name=collection_name,
        dim=512,
        overwrite=False,
        similarity_metric="COSINE",
    )
    vector_store.client.load_collection(collection_name=collection_name)
    vector_index = VectorStoreIndex.from_vector_store(vector_store, embed_model=embed_model)
    row_count_before = vector_store.client.get_collection_stats(collection_name=collection_name)["row_count"]
    if old_node_ids:
        vector_store.delete_nodes(node_ids=old_node_ids)
    if new_nodes:
        vector_index.insert_nodes(new_nodes)
    row_count_after = vector_store.client.get_collection_stats(collection_name=collection_name)["row_count"]

    old_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    unchanged_nodes = [
        TextNode(id_=item["node_id"], text=item["text"], metadata=item["metadata"])
        for item in old_snapshot
        if item["metadata"]["source_path"] not in touched_paths
    ]
    nodes = unchanged_nodes + new_nodes
    _write_node_snapshot(nodes, snapshot_path)
    _write_manifest(documents, nodes, manifest_path)

    observability: dict[str, object] = {
        "provider": "FastEmbed",
        "model": embed_model.model_name,
        "dimension": 512,
        "model_load_seconds": round(embed_model.load_seconds, 3),
        "embedding_calls": embed_model.embed_calls,
        "embedded_text_count": embed_model.embedded_text_count,
        "embedding_seconds": round(embed_model.embedding_seconds, 3),
        "milvus_uri": milvus_uri,
        "collection_name": collection_name,
        "overwrite": False,
        "row_count_before": row_count_before,
        "deleted_old_node_count": len(old_node_ids),
        "inserted_new_node_count": len(new_nodes),
        "unchanged_node_count": len(unchanged_nodes),
        "row_count_after_insert": row_count_after,
        "added_documents": len(changes["added"]),
        "changed_documents": len(changes["changed"]),
        "deleted_documents": len(changes["deleted"]),
        "unchanged_documents": len(changes["unchanged"]),
    }
    return BuiltIndexes(
        nodes=nodes,
        vector_index=vector_index,
        bm25=ChineseBM25Retriever(nodes),
        storage_dir=storage_dir,
        milvus_uri=milvus_uri,
        collection_name=collection_name,
        observability=observability,
    )


def retrieve_vector(
    indexes: BuiltIndexes, query: str, top_k: int = 5, login_only: bool = False
) -> list[NodeWithScore]:
    if indexes.vector_index is None:
        raise RuntimeError("向量索引不存在")
    candidate_count = len(indexes.nodes) if login_only else top_k
    raw_hits = indexes.vector_index.as_retriever(similarity_top_k=candidate_count).retrieve(query)
    if not login_only:
        return list(raw_hits)
    return [hit for hit in raw_hits if _is_current_login_node(hit.node)][:top_k]


def retrieve_hybrid(indexes: BuiltIndexes, query: str, top_k: int = 5) -> list[NodeWithScore]:
    vector_hits = retrieve_vector(indexes, query, top_k=top_k, login_only=True)
    bm25_hits = indexes.bm25.retrieve(query, top_k=top_k, login_only=True)
    return list(rrf_fuse([vector_hits, bm25_hits], top_k=top_k))


def format_build_observability(values: dict[str, object]) -> str:
    """将建库证据输出成稳定的键值日志，便于人和自动化脚本核对。"""
    fields = [
        "provider",
        "model",
        "dimension",
        "model_load_seconds",
        "embedding_calls",
        "embedded_text_count",
        "embedding_seconds",
        "milvus_uri",
        "collection_name",
        "overwrite",
        "row_count_after_insert",
    ]
    return "\n".join(f"{field}={values[field]}" for field in fields)


def format_citation_evidence(hits: Sequence[NodeWithScore]) -> str:
    """将模型答案中的 [编号] 显式映射到检索来源和原始节点文本。"""
    sections: list[str] = []
    for index, hit in enumerate(hits, start=1):
        source = hit.node.metadata["source_path"]
        sections.append(f"[{index}] {source}\n{hit.node.text.strip()}")
    return "\n\n".join(sections)


def format_hits(hits: Sequence[NodeWithScore]) -> str:
    lines: list[str] = []
    for rank, hit in enumerate(hits, start=1):
        source = hit.node.metadata["source_path"]
        preview = re.sub(r"\s+", " ", hit.node.text).strip()[:180]
        lines.append(f"{rank}. score={hit.score:.6f} source={source}\n   {preview}")
    return "\n".join(lines)


def answer_with_citations(question: str, hits: Sequence[NodeWithScore]) -> str:
    """将混合检索证据提交给真实 OpenAI-compatible Chat Model。"""
    from llama_index.core.llms import ChatMessage
    from llama_index.llms.openai_like import OpenAILike

    api_key = _deepseek_api_key()
    context = "\n\n".join(
        f"[{index}] 来源：{hit.node.metadata['source_path']}\n{hit.node.text.strip()}"
        for index, hit in enumerate(hits, start=1)
    )
    prompt = f"""你是一个 RAG 回答器。只能依据给定证据回答，不能使用证据以外的知识。
每个结论都必须在句末使用 [编号] 引用证据。若证据冲突，说明冲突和各自来源。

问题：{question}

证据：
{context}
"""
    llm = OpenAILike(
        model="deepseek-v4-flash",
        api_base="https://api.deepseek.com/v1",
        api_key=api_key,
        is_chat_model=True,
        temperature=0,
    )
    response = llm.chat([ChatMessage(role="user", content=prompt)])
    return response.message.content or ""


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    closing = text.find("\n---\n", 4)
    if closing < 0:
        return {}, text
    metadata: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata, text[closing + 5 :]


def _write_manifest(
    documents: Sequence[Document], nodes: Sequence[TextNode], path: Path
) -> None:
    documents_by_path = {
        str(document.metadata["source_path"]): document for document in documents
    }
    node_ids_by_path: dict[str, list[str]] = {source_path: [] for source_path in documents_by_path}
    for node in nodes:
        node_ids_by_path[str(node.metadata["source_path"])].append(node.node_id)
    payload = {
        "documents": {
            source_path: {
                "content_hash": document.metadata["content_hash"],
                "node_ids": node_ids_by_path[source_path],
            }
            for source_path, document in documents_by_path.items()
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_manifest_hashes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        source_path: item["content_hash"]
        for source_path, item in payload.get("documents", {}).items()
    }


def _write_node_snapshot(nodes: Sequence[TextNode], path: Path) -> None:
    payload = [
        {"node_id": node.node_id, "text": node.text, "metadata": node.metadata}
        for node in nodes
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _deepseek_api_key() -> str:
    import os

    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                key = line.split("=", 1)[1].strip()
                if key:
                    return key
    raise RuntimeError(
        "缺少 DeepSeek 官方 API 密钥：请将 DEEPSEEK_API_KEY 写入 rag/.env，"
        "或通过环境变量提供；可参考 rag/.env.example"
    )
