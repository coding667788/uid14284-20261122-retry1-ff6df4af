"""测试共用工具：内存/临时 SQLite、事件构造、整批执行。"""

from __future__ import annotations

import os
import tempfile

from solution import db as dbmod
from solution.engine import Engine
from solution.validation import validate_case


def ev(cmd, at, seq, request_id, payload):
    return {"cmd": cmd, "at": at, "seq": seq, "request_id": request_id, "payload": payload}


def run_events(conn, events):
    """先做结构校验，再在单事务内整批执行。"""
    clean = validate_case({"events": events})
    with dbmod.transaction(conn):
        return Engine(conn).run_batch(clean)


def new_db_path():
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="recovery_")
    os.close(fd)
    os.unlink(path)  # connect 会自行建库
    return path
