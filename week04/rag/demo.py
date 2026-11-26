from pathlib import Path

from llama_index.core import Document, VectorStoreIndex
from llama_index.llms.openai_like import OpenAILike
from llama_index.embeddings.openai_like import OpenAILikeEmbedding

print("正在解析文件...")
docs_dir = Path(__file__).parent.parent / "docs"
documents = [
    Document(
        text=doc_path.read_text(encoding="utf-8"),
        metadata={"filepath": str(doc_path.relative_to(docs_dir.parent))},
    )
    for doc_path in sorted(docs_dir.glob("*.md"))
]
print(f"文档个数：{len(documents)}")

print("正在创建索引...")
index = VectorStoreIndex.from_documents(
    documents,
    embed_model=OpenAILikeEmbedding(
        model_name="text-embedding-v4",
        api_base="http://ai-service.tal.com/openai-compatible/v1",
        api_key="xxx"
    )
)

print("正在创建提问引擎...")
query_engine = index.as_query_engine(
# 设置为流式输出
    streaming=True,
    # LLM 用 MiniMax-Text-01（OpenAI 形状接口，api_base / key 已在 build_llm 里配好）
    llm=OpenAILike(
        is_chat_model=True,
        model="gpt-5.6-terra",
        api_key="xxx",
        api_base="http://ai-service.tal.com/openai-compatible/v1",
    ),
)

print("正在生成回复...")
streaming_response = query_engine.query("登录会话的有效期是多久？到期边界应该怎么判定？")
# 采用流式输出
streaming_response.print_response_stream()
print()
