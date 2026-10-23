from __future__ import annotations

import json

from langchain_core.tools import tool


@tool
def build_service(service: str, version: str) -> str:
    """Build a service release artifact."""
    return json.dumps({"name": "build_service", "passed": True})


@tool
def run_unit_tests(service: str) -> str:
    """Run unit tests for a service."""
    return json.dumps({"name": "run_unit_tests", "passed": True})


@tool
def security_scan(diff: str) -> str:
    """Run a security scan for a patch diff."""
    return json.dumps({"name": "security_scan", "passed": True})


@tool
def dependency_audit(service: str) -> str:
    """Audit a service dependency tree."""
    return json.dumps({"name": "dependency_audit", "passed": True})


CHECK_TOOLS = [build_service, run_unit_tests, security_scan, dependency_audit]
