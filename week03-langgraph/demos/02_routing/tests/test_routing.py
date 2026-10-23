from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import run_pipeline


async def collect(patch_diff: str):
    return [
        update
        async for update in run_pipeline(
            "REL-001", "orders", "1.0.0", patch_diff
        )
    ]


def test_short_diff_routes_to_automatic_approval():
    updates = asyncio.run(collect("fix typo"))
    assert updates[1]["classify_change"]["change_level"] == "L"
    assert updates[2]["approval_auto_L"]["approver_count"] == 0
    assert updates[-1]["finalize"]["outcome"] == "released"


def test_long_diff_routes_to_lead_approval():
    updates = asyncio.run(collect("x" * 1200))
    assert updates[1]["classify_change"]["change_level"] == "M"
    assert updates[2]["approval_lead_M"]["approver_count"] == 1


def test_sensitive_diff_routes_to_dual_approval():
    updates = asyncio.run(collect("rotate auth token"))
    assert updates[1]["classify_change"]["change_level"] == "H"
    assert updates[2]["approval_dual_H"]["approver_count"] == 2


def test_all_levels_complete_every_release_stage():
    expected = ["staging", "smoke", "canary_10", "canary_50", "canary_100"]
    for patch_diff in ("fix typo", "x" * 1200, "auth token change"):
        updates = asyncio.run(collect(patch_diff))
        assert updates[-2]["canary_100"]["stages_completed"] == expected
