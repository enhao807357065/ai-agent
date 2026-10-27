from __future__ import annotations

from pathlib import Path
from typing import Any


def mermaid_text(graph: Any) -> str:
    return graph.get_graph().draw_mermaid()


def export_mermaid(graph: Any, out_dir: str | Path) -> dict[str, str]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    mmd_path = directory / "release_06.mmd"
    png_path = directory / "release_06.png"
    mmd_path.write_text(mermaid_text(graph), encoding="utf-8")
    info = {"mmd_path": str(mmd_path), "png_path": str(png_path), "png_status": "written"}
    try:
        png_path.write_bytes(graph.get_graph().draw_mermaid_png())
    except Exception as exc:
        info["png_status"] = str(exc)
    return info


def stream_nodes(graph: Any, initial: dict, config: dict) -> list[str]:
    nodes: list[str] = []
    for event in graph.stream(initial, config, stream_mode="updates"):
        for name, update in event.items():
            if name == "__interrupt__":
                for intr in update:
                    nodes.append(f"⏸ {intr.value['interrupt_id']}")
            else:
                nodes.append(name)
    return nodes
