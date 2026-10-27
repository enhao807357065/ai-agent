from __future__ import annotations

import argparse
import json

from langgraph.types import Command

from checkpointer import open_checkpointer
from graph import build_graph
from visualize import export_mermaid, mermaid_text, stream_nodes


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def initial(args) -> dict:
    return {"ticket_id": args.ticket_id, "service": args.service, "version": args.version, "patch_diff": args.patch_diff, "revision_count": 0, "max_revisions": args.max_revisions, "rollback_count": 0, "max_rollbacks": args.max_rollbacks, "canary_cursor": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("ticket_id")
    submit.add_argument("service")
    submit.add_argument("version")
    submit.add_argument("patch_diff")
    submit.add_argument("--thread-id", required=True)
    submit.add_argument("--max-revisions", type=int, default=1)
    submit.add_argument("--max-rollbacks", type=int, default=2)
    status = commands.add_parser("status")
    status.add_argument("thread_id")
    approve = commands.add_parser("approve")
    approve.add_argument("thread_id")
    approve.add_argument("action", choices=["approve", "reject", "revise", "abort"])
    approve.add_argument("actor")
    approve.add_argument("feedback")
    visualize = commands.add_parser("visualize")
    visualize.add_argument("--out-dir", default="visualizations")
    watch = commands.add_parser("watch")
    watch.add_argument("thread_id")
    watch.add_argument("ticket_id")
    watch.add_argument("service")
    watch.add_argument("version")
    watch.add_argument("patch_diff")
    watch.add_argument("--max-revisions", type=int, default=1)
    watch.add_argument("--max-rollbacks", type=int, default=2)
    args = parser.parse_args()
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        if args.command == "visualize":
            print(mermaid_text(graph))
            print(json.dumps(export_mermaid(graph, args.out_dir), ensure_ascii=False))
            return
        if args.command == "watch":
            print(" → ".join(stream_nodes(graph, initial(args), config(args.thread_id))))
            return
        if args.command == "submit":
            graph.invoke(initial(args), config(args.thread_id))
        elif args.command == "approve":
            graph.invoke(Command(resume={"action": args.action, "actor": args.actor, "feedback": args.feedback}), config(args.thread_id))
        snapshot = graph.get_state(config(args.thread_id))
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    print(json.dumps({"next": snapshot.next, "interrupts": pending, "values": snapshot.values}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
