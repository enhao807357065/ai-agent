import os

from smolagents import OpenAIModel


def main() -> None:
    model = OpenAIModel(
        model_id=os.environ["DEEPSEEK_MODEL_ID"],
        api_base=os.environ["DEEPSEEK_BASE_URL"],
        api_key=os.environ["DEEPSEEK_API_KEY"],
        temperature=float(os.environ.get("SMOLAGENTS_TEMPERATURE", "0")),
    )

    result = model([
        {
            "role": "user",
            "content": "只回答：deepseek-official-api-ok",
        }
    ])
    print(result.content)


if __name__ == "__main__":
    main()