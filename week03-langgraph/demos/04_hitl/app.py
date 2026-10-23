from __future__ import annotations

import argparse
import json

from langgraph.types import Command

from checkpointer import open_checkpointer
from graph import build_graph


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("ticket_id")
    submit.add_argument("service")
    submit.add_argument("version")
    submit.add_argument("patch_diff")
    submit.add_argument("--thread-id", required=True)
    status = commands.add_parser("status")
    status.add_argument("thread_id")
    approve = commands.add_parser("approve")
    approve.add_argument("thread_id")
    approve.add_argument("action", choices=["approve", "reject"])
    approve.add_argument("actor")
    approve.add_argument("feedback")
    args = parser.parse_args()
    with open_checkpointer() as saver:
        graph = build_graph(saver)
        if args.command == "submit":
            graph.invoke(
                {"ticket_id": args.ticket_id, "service": args.service, "version": args.version, "patch_diff": args.patch_diff},
                config(args.thread_id),
            )
        elif args.command == "approve":
            graph.invoke(
                Command(resume={"action": args.action, "actor": args.actor, "feedback": args.feedback}),
                config(args.thread_id),
            )
        snapshot = graph.get_state(config(args.thread_id))
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    print(json.dumps({"next": snapshot.next, "interrupts": pending, "values": snapshot.values}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
