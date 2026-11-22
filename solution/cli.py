"""命令行入口：``python -m solution <命令> CASE.json [--db DB.sqlite]``。

退出码：
* 0 成功；
* 1 业务状态错误 / 幂等冲突 / simulate_loss 无解；
* 2 输入结构错误；
* 3 用法或 IO 错误。

stdout 始终输出确定性 UTF-8 JSON（成功或错误统一信封）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db as dbmod
from . import simulate as simulate_mod
from .engine import Engine
from .errors import AppError
from .jsonio import dump_bytes, loads
from .validation import (
    ALL_CMDS,
    READONLY_CMDS,
    STATEFUL_CMDS,
    validate_case,
    validate_event,
    validate_simulate,
)

SINGLE_EVENT_CMDS = STATEFUL_CMDS | READONLY_CMDS


def _read_case(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return loads(fh.read())
    except FileNotFoundError:
        raise AppError("E_IO", f"输入文件不存在：{path}", "case")
    except json.JSONDecodeError as exc:
        raise AppError(
            "E_JSON",
            f"JSON 解析失败：{exc.msg}",
            f"line {exc.lineno}, column {exc.colno}",
        )


def _emit(payload: dict, ok: bool) -> int:
    envelope = {"ok": ok}
    envelope.update(payload)
    sys.stdout.buffer.write(dump_bytes(envelope))
    sys.stdout.buffer.flush()
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="企业账号离线恢复控制器（标准库、SQLite、确定性 JSON）",
    )
    parser.add_argument("command", choices=sorted(ALL_CMDS | {"ingest"}))
    parser.add_argument("case", help="用例 JSON 文件路径")
    parser.add_argument("--db", help="SQLite 数据库路径（状态类命令必填）")
    parser.add_argument(
        "--crash-before-commit",
        action="store_true",
        help="在事务 COMMIT 前模拟进程退出（自动回滚，仅用于恢复演练测试）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command

    try:
        raw = _read_case(args.case)

        if command == "simulate_loss":
            if args.db:
                # 演练是纯计算；给了 --db 时顺带校验账号尚未申请恢复
                spec = validate_simulate(raw)
                conn = dbmod.connect(args.db)
                try:
                    row = conn.execute(
                        "SELECT 1 FROM applications WHERE account_id = ? LIMIT 1",
                        (spec["account_id"],),
                    ).fetchone()
                    if row is not None:
                        raise AppError(
                            "E_STATE",
                            f"账号 {spec['account_id']} 已存在恢复申请，simulate_loss 仅面向尚未申请恢复的账号",
                            "$.account_id",
                        )
                finally:
                    conn.close()
            else:
                spec = validate_simulate(raw)
            result = simulate_mod.solve(spec)
            if result.get("result") == "no_recovery_plan":
                # 无解是正常计算结果：走统一错误信封，退出码 1
                envelope = {
                    "ok": False,
                    "error": {
                        "code": "E_NO_RECOVERY_PLAN",
                        "message": (
                            f"失效情形 {result['first_failed_scenario']} 下"
                            "即使使用全部候选证明也无法满足任何配方"
                        ),
                        "details": result,
                    },
                }
                sys.stdout.buffer.write(dump_bytes(envelope))
                sys.stdout.buffer.flush()
                return 1
            return _emit(result, True)

        if not args.db:
            raise AppError("E_USAGE", f"状态命令 {command} 必须提供 --db DB.sqlite", "--db")

        if command == "ingest":
            events = validate_case(raw)
        else:
            event = validate_event(raw)
            if event["cmd"] != command:
                raise AppError(
                    "E_USAGE",
                    f"CLI 命令 {command!r} 与用例 cmd={event['cmd']!r} 不一致",
                    "$.cmd",
                )
            events = [event]

        conn = dbmod.connect(args.db)
        try:
            engine = Engine(conn)
            with dbmod.transaction(conn, crash_before_commit=args.crash_before_commit):
                receipts = engine.run_batch(events)
        finally:
            conn.close()

        if command == "ingest":
            return _emit({"results": receipts}, True)
        return _emit(receipts[0], True)

    except AppError as exc:
        out = _emit(exc.to_dict(), False)
        # 结构错误使用退出码 2，其余业务错误退出码 1
        if exc.code == "E_VALIDATION" or exc.code == "E_JSON":
            return 2
        if exc.code == "E_USAGE":
            return 3
        return out
    except RuntimeError as exc:
        # 模拟崩溃：事务已回滚，输出可识别的崩溃回执，退出码 4
        _emit(
            {"error": {"code": "E_SIMULATED_CRASH", "message": str(exc)}},
            False,
        )
        return 4


if __name__ == "__main__":
    sys.exit(main())
