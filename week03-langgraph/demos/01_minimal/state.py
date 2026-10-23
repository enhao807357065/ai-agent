from __future__ import annotations

from typing import NotRequired, TypedDict

# typing.NotRequired 用于给 TypedDict 中的字段标记为可选键：这个键可以根本不存在，而不仅仅是值可以为 None。
# 与 Optional 的区别：from typing import Optional, TypedDict
#
#   class User(TypedDict):
#       nickname: Optional[str]
#
#   这表示 nickname 必须有这个键，但值可以是 None：


class ReleaseState(TypedDict):
    ticket_id: str
    service: str
    version: str
    checks_passed: int
    stages_completed: NotRequired[list[str]]
    outcome: NotRequired[str]
    report_path: NotRequired[str]
