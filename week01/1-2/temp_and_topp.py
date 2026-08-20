from collections import Counter

from openai import OpenAI
from dotenv import load_dotenv
import os

"""
两者都控制"发散程度"，但作用层不同

作用时机
• Temperature: 在 softmax 之前缩放 logits
• Top-P (Nucleus Sampling): 在 softmax 之后截断候选集

机制
• Temperature: logits / temperature，值越大分布越平坦
• Top-P (Nucleus Sampling): 按概率降序累加，只保留累积概率达到 P 的 token
"""

PROMPT = (
    "为一个“把模型错误统一归一化”的 Python 函数起更短的名字。"
    "允许使用缩写、同义词或不同动词形式。"
    "只输出 1 个 snake_case 名称，不要解释。"
)

load_dotenv()
apikey = os.getenv("LLM_API_KEY")

client = OpenAI(
    base_url="http://ai-service.tal.com/openai-compatible/v1",
    api_key=apikey,
    timeout=30.0,
    max_retries=0
)

def sample(*, temperature: float, top_p: float) -> str:
    response = client.chat.completions.create(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": PROMPT}],
        temperature=temperature,
        top_p=top_p,
        max_tokens=16,
        extra_body={"thinking": {"type": "disabled"}}
    )
    return response.choices[0].message.content.strip()

def run_case(*, label: str, temperature: float, top_p: float):
    values = [sample(temperature=temperature, top_p=top_p) for _ in range(10)]
    counts = Counter(values)

    print(f"\n[{label}] temperature={temperature}, top_p={top_p}")
    print(f"样本: {values}")
    print(f"去重数: {len(counts)}/{10}")
    print("Top 频次:", counts.most_common(5))

def main():
    print("=== 固定 top_p=1.0，只观察 temperature ===")
    for temperature in [0.0, 0.3, 0.8, 1.3]:
        run_case(
            label="temperature 对照组",
            temperature=temperature,
            top_p=1.0,
        )

    print("\n=== 固定 temperature=1.0，只观察 top_p ===")
    for top_p in [1.0, 0.7, 0.3, 0.1]:
        run_case(
            label="top_p 对照组",
            temperature=1.0,
            top_p=top_p,
        )

if __name__ == '__main__':
    main()