from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


@contextmanager
def open_checkpointer() -> Iterator[SqliteSaver]:
    db_path = Path(os.getenv("CHECKPOINT_DB", "./checkpoints_04.sqlite"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(db_path)) as saver:
        yield saver
