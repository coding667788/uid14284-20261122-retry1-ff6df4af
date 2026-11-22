"""确定性 UTF-8 JSON 读写。

输出规则固定：``ensure_ascii=False``（中文原样输出）、``sort_keys=True``、
分隔符 ``, `` / ``: ``，并以单个换行结尾，保证任意机器上字节级可重现。
"""

from __future__ import annotations

import json
from typing import Any


def dumps(obj: Any) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def dump_bytes(obj: Any) -> bytes:
    return (dumps(obj) + "\n").encode("utf-8")


def loads(text: str | bytes):
    return json.loads(text)
