from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from graph import build_graph


async def run_pipeline(
    ticket_id: str, service: str, version: str, patch_diff: str
) -> AsyncIterator[dict[str, Any]]:
    async for update in build_graph().astream(
        {
            "ticket_id": ticket_id,
            "service": service,
            "version": version,
            "patch_diff": patch_diff,
        },
        config={"configurable": {"thread_id": ticket_id}},
        stream_mode="updates",
    ):
        yield update


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_id")
    parser.add_argument("service")
    parser.add_argument("version")
    parser.add_argument("patch_diff")
    args = parser.parse_args()
    async for update in run_pipeline(
        args.ticket_id, args.service, args.version, args.patch_diff
    ):
        print(json.dumps(update, ensure_ascii=False, default=str))


if __name__ == "__main__":
    asyncio.run(main())
