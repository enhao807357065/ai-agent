from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from app import run_pipeline


def run_updates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def collect_updates():
        return [
            update
            async for update in run_pipeline("REL-001", "orders", "1.0.0")
        ]

    return asyncio.run(collect_updates())


def test_outcome_is_released(tmp_path, monkeypatch):
    updates = run_updates(tmp_path, monkeypatch)
    assert updates[-1]["finalize"]["outcome"] == "released"


def test_stages_complete_in_order(tmp_path, monkeypatch):
    updates = run_updates(tmp_path, monkeypatch)
    assert updates[-2]["canary_100"]["stages_completed"] == [
        "staging",
        "smoke",
        "canary_10",
        "canary_50",
        "canary_100",
    ]


def test_all_checks_passed(tmp_path, monkeypatch):
    updates = run_updates(tmp_path, monkeypatch)
    assert updates[0]["intake"]["checks_passed"] == 4


def test_report_exists_and_loads(tmp_path, monkeypatch):
    updates = run_updates(tmp_path, monkeypatch)
    report_path = updates[-1]["finalize"]["report_path"]
    with open(report_path, encoding="utf-8") as report_file:
        report = json.load(report_file)
    assert report["ticket_id"] == "REL-001"
