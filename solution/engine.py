"""恢复控制器状态机。

事件按 (逻辑时刻, 批内次序) 处理；唯一例外是**同截止异议优先**：
旧认证器在申请仍处于有效身份世代、且事件时刻恰好等于冻结截止点时发起的
异议，在同一时刻批次内排在所有其他事件之前。
"""

from __future__ import annotations

import json
import sqlite3

from . import selection
from .errors import ConflictError, StateError, ValidationError
from .jsonio import dumps
from .validation import STATEFUL_CMDS, canonical_content


class Engine:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ 小工具
    def _one(self, sql: str, params=()):
        cur = self.conn.execute(sql, params)
        return cur.fetchone()

    def _all(self, sql: str, params=()):
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def _trustee_effective(self, trustee_id: str, at: int) -> sqlite3.Row | None:
        """取时刻 at 生效的受托人版本。

        受托人变更以新记录整体替换旧登记（同时刻按 seq 取最后一条），
        不回退到历史版本；再校验该版本自身的保管有效期是否覆盖 at。
        """
        row = self._one(
            """
            SELECT * FROM trustees
            WHERE trustee_id = ? AND updated_at <= ?
            ORDER BY updated_at DESC, seq DESC, id DESC
            LIMIT 1
            """,
            (trustee_id, at),
        )
        if row is not None and row["valid_from"] <= at < row["valid_until"]:
            return row
        return None

    def _proof_effective(self, proof_row: sqlite3.Row, at: int) -> tuple[bool, str | None]:
        """证明在时刻 at 是否仍然有效（未撤销、未过期、受托人在有效保管期内）。"""
        if proof_row["revoked_at"] is not None and proof_row["revoked_at"] <= at:
            return False, "revoked"
        if at >= proof_row["valid_until"]:
            return False, "expired"
        trustee = self._trustee_effective(proof_row["trustee_id"], at)
        if trustee is None:
            return False, "trustee_not_effective"
        return True, None

    def _is_frozen(self, proof_id: str) -> bool:
        return self._one(
            "SELECT 1 FROM reservations WHERE proof_id = ? LIMIT 1", (proof_id,)
        ) is not None

    def _store_request(self, event: dict, receipt: dict) -> None:
        rid = event["request_id"]
        content = canonical_content(event)
        existing = self._one(
            "SELECT content_hash FROM requests WHERE request_id = ?", (rid,)
        )
        if existing is not None:
            # run_batch 已对重放/冲突做短路；走到这里说明调用方绕过了批处理入口
            if existing["content_hash"] != content:
                raise ConflictError(
                    f"request_id {rid!r} 已用于不同内容的请求，整批拒绝",
                    "request_id",
                    {"request_id": rid},
                )
            return
        self.conn.execute(
            "INSERT INTO requests(request_id, content_hash, kind, receipt_json) VALUES (?,?,?,?)",
            (rid, content, event["cmd"], dumps(receipt)),
        )

    def _stored_receipt(self, request_id: str) -> dict:
        row = self._one(
            "SELECT receipt_json FROM requests WHERE request_id = ?", (request_id,)
        )
        if row is None:
            raise StateError(f"request_id {request_id!r} 不存在", "request_id")
        return json.loads(row["receipt_json"])

    # ------------------------------------------------------------ 批处理入口
    def run_batch(self, events: list[dict]) -> list[dict]:
        ordered = self._order_events(events)
        results: list[dict] = []
        for event in ordered:
            if event["cmd"] in STATEFUL_CMDS:
                rid = event["request_id"]
                existing = self._one(
                    "SELECT receipt_json, content_hash FROM requests WHERE request_id = ?", (rid,)
                )
                if existing is not None:
                    if existing["content_hash"] != canonical_content(event):
                        raise ConflictError(
                            f"request_id {rid!r} 已用于不同内容的请求，整批拒绝",
                            "request_id",
                            {"request_id": rid},
                        )
                    results.append(json.loads(existing["receipt_json"]))
                    continue
                receipt = self._dispatch(event)
                self._store_request(event, receipt)
                results.append(receipt)
            elif event["cmd"] == "get_receipt":
                results.append(self._stored_receipt(event["payload"]["request_id"]))
            else:  # simulate_loss 不允许出现在 ingest 批处理中
                raise ValidationError(
                    "simulate_loss 是只读演练命令，不能放进 ingest 事件批", "cmd"
                )
        return results

    def _order_events(self, events: list[dict]) -> list[dict]:
        """按时刻分组；同组先判定同截止异议，再 (cutoff优先, seq) 排序。"""
        groups: dict[int, list[dict]] = {}
        for ev in events:
            groups.setdefault(ev["at"], []).append(ev)

        ordered: list[dict] = []
        for at in sorted(groups):
            batch = sorted(groups[at], key=lambda e: e["seq"])
            decorated = [(0 if self._is_cutoff_challenge(ev, at) else 1, ev["seq"], i, ev)
                         for i, ev in enumerate(batch)]
            decorated.sort(key=lambda t: (t[0], t[1]))
            ordered.extend(t[3] for t in decorated)
        return ordered

    def _is_cutoff_challenge(self, event: dict, at: int) -> bool:
        if event["cmd"] != "challenge" or event["payload"]["challenger"] != "old_authenticator":
            return False
        app_request_id = event["payload"].get("app_request_id")
        if not app_request_id:
            return False
        row = self._one(
            """
            SELECT a.*, ac.current_generation
            FROM applications a JOIN accounts ac ON ac.account_id = a.account_id
            WHERE a.request_id = ?
            """,
            (app_request_id,),
        )
        if row is None or row["status"] != "FROZEN":
            return False
        # 旧认证器、有效世代、时刻恰在冻结截止点
        return (
            row["current_generation"] == row["generation"]
            and at == row["freeze_until"]
        )

    # ------------------------------------------------------------ 分发
    def _dispatch(self, event: dict) -> dict:
        handler = getattr(self, f"_on_{event['cmd']}")
        return handler(event)

    # ------------------------------------------------------------ 注册类
    def _on_register_account(self, event: dict) -> dict:
        p = event["payload"]
        if self._one("SELECT 1 FROM accounts WHERE account_id = ?", (p["account_id"],)):
            raise StateError(f"账号 {p['account_id']} 已存在", "payload.account_id")
        self.conn.execute(
            "INSERT INTO accounts(account_id, current_generation, active_authenticator, security_contact)"
            " VALUES (?,?,?,?)",
            (p["account_id"], 1, p["initial_authenticator"], p["security_contact"]),
        )
        self.conn.execute(
            "INSERT INTO generations(account_id, generation, authenticator, superseded) VALUES (?,?,?,0)",
            (p["account_id"], 1, p["initial_authenticator"]),
        )
        return {
            "request_id": event["request_id"],
            "result": "account_registered",
            "account_id": p["account_id"],
            "generation": 1,
            "at": event["at"],
        }

    def _on_register_trustee(self, event: dict) -> dict:
        return self._upsert_trustee(event, first=True)

    def _on_update_trustee(self, event: dict) -> dict:
        return self._upsert_trustee(event, first=False)

    def _upsert_trustee(self, event: dict, *, first: bool) -> dict:
        p = event["payload"]
        exists = self._one(
            "SELECT 1 FROM trustees WHERE trustee_id = ? LIMIT 1", (p["trustee_id"],)
        )
        if first and exists:
            raise StateError(f"受托人 {p['trustee_id']} 已登记", "payload.trustee_id")
        if not first and not exists:
            raise StateError(f"受托人 {p['trustee_id']} 不存在，无法变更", "payload.trustee_id")
        self.conn.execute(
            "INSERT INTO trustees(trustee_id, organization, custody_domain, valid_from, valid_until,"
            " updated_at, seq) VALUES (?,?,?,?,?,?,?)",
            (
                p["trustee_id"],
                p["organization"],
                p["custody_domain"],
                p["valid_from"],
                p["valid_until"],
                event["at"],
                event["seq"],
            ),
        )
        return {
            "request_id": event["request_id"],
            "result": "trustee_registered" if first else "trustee_updated",
            "trustee_id": p["trustee_id"],
            "organization": p["organization"],
            "custody_domain": p["custody_domain"],
            "valid_from": p["valid_from"],
            "valid_until": p["valid_until"],
            "at": event["at"],
        }

    def _on_issue_proof(self, event: dict) -> dict:
        p = event["payload"]
        if self._one("SELECT 1 FROM proofs WHERE proof_id = ?", (p["proof_id"],)):
            raise StateError(f"证明 {p['proof_id']} 已存在", "payload.proof_id")
        if not self._one("SELECT 1 FROM accounts WHERE account_id = ?", (p["account_id"],)):
            raise StateError(f"账号 {p['account_id']} 不存在", "payload.account_id")
        trustee = self._trustee_effective(p["trustee_id"], p["issued_at"])
        if trustee is None:
            raise StateError(
                f"受托人 {p['trustee_id']} 在签发时刻 {p['issued_at']} 不处于有效保管期",
                "payload.trustee_id",
            )
        self.conn.execute(
            "INSERT INTO proofs(proof_id, account_id, trustee_id, proof_type, digest, issued_at,"
            " valid_until, cost, revoked_at) VALUES (?,?,?,?,?,?,?,?,NULL)",
            (
                p["proof_id"],
                p["account_id"],
                p["trustee_id"],
                p["proof_type"],
                p["digest"],
                p["issued_at"],
                p["valid_until"],
                p["cost"],
            ),
        )
        return {
            "request_id": event["request_id"],
            "result": "proof_issued",
            "proof_id": p["proof_id"],
            "account_id": p["account_id"],
            "proof_type": p["proof_type"],
            "custody_domain": trustee["custody_domain"],
            "organization": trustee["organization"],
            "at": event["at"],
        }

    def _on_revoke_proof(self, event: dict) -> dict:
        p = event["payload"]
        row = self._one("SELECT * FROM proofs WHERE proof_id = ?", (p["proof_id"],))
        if row is None:
            raise StateError(f"证明 {p['proof_id']} 不存在", "payload.proof_id")
        if row["revoked_at"] is not None:
            raise StateError(
                f"证明 {p['proof_id']} 已在时刻 {row['revoked_at']} 撤销",
                "payload.proof_id",
            )
        self.conn.execute(
            "UPDATE proofs SET revoked_at = ? WHERE proof_id = ?",
            (p["revoked_at"], p["proof_id"]),
        )
        return {
            "request_id": event["request_id"],
            "result": "proof_revoked",
            "proof_id": p["proof_id"],
            "revoked_at": p["revoked_at"],
            "at": event["at"],
        }

    def _on_register_recipe(self, event: dict) -> dict:
        p = event["payload"]
        if self._one(
            "SELECT 1 FROM recipes WHERE recipe_id = ? AND version = ?",
            (p["recipe_id"], p["version"]),
        ):
            raise StateError(
                f"配方 {p['recipe_id']} 版本 {p['version']} 已登记",
                "payload.recipe_id",
            )
        self.conn.execute(
            "INSERT INTO recipes(recipe_id, version, requirements, active_from) VALUES (?,?,?,?)",
            (p["recipe_id"], p["version"], dumps(p["requirements"]), p["active_from"]),
        )
        return {
            "request_id": event["request_id"],
            "result": "recipe_registered",
            "recipe_id": p["recipe_id"],
            "version": p["version"],
            "at": event["at"],
        }

    # ------------------------------------------------------------ 恢复申请
    def _load_active_recipes(self, at: int, pin: dict | None) -> list[dict]:
        if pin is not None:
            row = self._one(
                "SELECT * FROM recipes WHERE recipe_id = ? AND version = ?",
                (pin["recipe_id"], pin["recipe_version"]),
            )
            if row is None:
                raise StateError(
                    f"配方 {pin['recipe_id']} 版本 {pin['recipe_version']} 不存在",
                    "payload.recipe_id",
                )
            if row["active_from"] > at:
                raise StateError(
                    f"配方在时刻 {at} 尚未生效（active_from={row['active_from']}）",
                    "payload.recipe_id",
                )
            return [self._recipe_row_to_dict(row)]
        rows = self._all("SELECT * FROM recipes WHERE active_from <= ? ORDER BY recipe_id, version", (at,))
        return [self._recipe_row_to_dict(r) for r in rows]

    @staticmethod
    def _recipe_row_to_dict(row: sqlite3.Row) -> dict:
        return {
            "recipe_id": row["recipe_id"],
            "version": row["version"],
            "requirements": json.loads(row["requirements"]),
        }

    def _on_reserve(self, event: dict) -> dict:
        p = event["payload"]
        at = event["at"]
        account = self._one("SELECT * FROM accounts WHERE account_id = ?", (p["account_id"],))
        if account is None:
            raise StateError(f"账号 {p['account_id']} 不存在", "payload.account_id")
        if p["generation"] != account["current_generation"]:
            raise StateError(
                f"申请世代 {p['generation']} 不是当前世代 {account['current_generation']}",
                "payload.generation",
            )
        if p["freeze_until"] <= at:
            raise StateError(
                "freeze_until 必须严格晚于申请时刻", "payload.freeze_until"
            )

        # 候选证明：属于本账号、未冻结给任何预留、当前仍然有效
        proof_rows = self._all(
            "SELECT * FROM proofs WHERE account_id = ? ORDER BY proof_id",
            (p["account_id"],),
        )
        candidates: list[dict] = []
        for row in proof_rows:
            if self._is_frozen(row["proof_id"]):
                continue
            ok, _reason = self._proof_effective(row, at)
            if not ok:
                continue
            trustee = self._trustee_effective(row["trustee_id"], at)
            candidates.append(
                {
                    "proof_id": row["proof_id"],
                    "proof_type": row["proof_type"],
                    "organization": trustee["organization"],
                    "custody_domain": trustee["custody_domain"],
                    "cost": row["cost"],
                }
            )

        pin = None
        if p["recipe_id"] is not None:
            pin = {"recipe_id": p["recipe_id"], "recipe_version": p["recipe_version"]}
        recipes = self._load_active_recipes(at, pin)
        witness = selection.select_recipe(recipes, candidates, mode="reserve")
        if witness is None:
            raise StateError(
                "当前没有任何预先批准配方可由有效且未冻结的证明满足",
                "payload.account_id",
                {"available_proof_ids": sorted(c["proof_id"] for c in candidates)},
            )

        app_id = f"app:{event['request_id']}"
        proof_ids = witness["proof_ids"]
        self.conn.execute(
            "INSERT INTO applications(app_id, account_id, generation, request_id, recipe_id,"
            " recipe_version, new_authenticator_digest, proof_ids_json, freeze_until, status,"
            " created_at, terminal_receipt_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                app_id,
                p["account_id"],
                p["generation"],
                event["request_id"],
                witness["recipe_id"],
                witness["version"],
                p["new_authenticator_digest"],
                dumps(proof_ids),
                p["freeze_until"],
                "FROZEN",
                at,
            ),
        )
        for pid in proof_ids:
            self.conn.execute(
                "INSERT INTO reservations(proof_id, app_id, frozen_at) VALUES (?,?,?)",
                (pid, app_id, at),
            )
        return {
            "request_id": event["request_id"],
            "app_id": app_id,
            "result": "reserved",
            "account_id": p["account_id"],
            "generation": p["generation"],
            "recipe_id": witness["recipe_id"],
            "recipe_version": witness["version"],
            "proof_ids": proof_ids,
            "new_authenticator_digest": p["new_authenticator_digest"],
            "freeze_until": p["freeze_until"],
            "status": "FROZEN",
            "at": at,
        }

    def _load_app_for_challenge(self, event: dict) -> sqlite3.Row:
        # challenge 用 request_id 关联目标申请：payload.app_request_id
        app_request_id = event["payload"].get("app_request_id")
        if not app_request_id:
            raise ValidationError(
                "challenge 必须在 payload.app_request_id 指定目标申请",
                "payload.app_request_id",
            )
        row = self._one(
            "SELECT * FROM applications WHERE request_id = ?", (app_request_id,)
        )
        if row is None:
            raise StateError(f"申请 {app_request_id} 不存在", "payload.app_request_id")
        return row

    def _on_challenge(self, event: dict) -> dict:
        at = event["at"]
        p = event["payload"]
        app = self._load_app_for_challenge(event)
        if app["status"] not in ("FROZEN", "CHALLENGED"):
            raise StateError(
                f"申请 {app['app_id']} 已处于终态 {app['status']}，不可异议",
                "payload.app_request_id",
            )
        if not (at <= app["freeze_until"]):
            raise StateError(
                f"异议时刻 {at} 晚于冻结截止点 {app['freeze_until']}",
                "at",
            )
        account = self._one(
            "SELECT * FROM accounts WHERE account_id = ?", (app["account_id"],)
        )
        if p["challenger"] == "old_authenticator":
            if account["current_generation"] != app["generation"]:
                raise StateError(
                    "申请世代已不再是有效世代，旧认证器异议被拒绝",
                    "payload.challenger",
                )
        # security_contact 的身份由登记联系人代表，控制器只校验联系人仍登记在账号上
        elif account["security_contact"] is None:
            raise StateError("账号未登记安全联系人", "payload.challenger")

        self.conn.execute(
            "INSERT INTO challenges(app_id, challenger, at, seq, request_id) VALUES (?,?,?,?,?)",
            (app["app_id"], p["challenger"], at, event["seq"], event["request_id"]),
        )
        receipt = {
            "request_id": event["request_id"],
            "app_id": app["app_id"],
            "result": "challenged",
            "challenger": p["challenger"],
            "at": at,
            "status": "CHALLENGED",
        }
        self.conn.execute(
            "UPDATE applications SET status = 'CHALLENGED', terminal_receipt_json = ? WHERE app_id = ?",
            (dumps(receipt), app["app_id"]),
        )
        return receipt

    def _on_cancel(self, event: dict) -> dict:
        app = self._load_app_for_challenge(event)
        if app["status"] != "FROZEN":
            raise StateError(
                f"申请 {app['app_id']} 已处于终态 {app['status']}，不可取消",
                "payload.app_request_id",
            )
        receipt = {
            "request_id": event["request_id"],
            "app_id": app["app_id"],
            "result": "cancelled",
            "reason": event["payload"].get("reason", ""),
            "at": event["at"],
            "status": "CANCELLED",
        }
        self.conn.execute(
            "UPDATE applications SET status = 'CANCELLED', terminal_receipt_json = ? WHERE app_id = ?",
            (dumps(receipt), app["app_id"]),
        )
        return receipt

    def _on_activate(self, event: dict) -> dict:
        at = event["at"]
        app = self._load_app_for_challenge(event)
        account = self._one(
            "SELECT * FROM accounts WHERE account_id = ?", (app["account_id"],)
        )

        # ---- 单事务内的完整复核（任一项失败即回滚，无部分提交）----
        challenge = self._one(
            "SELECT 1 FROM challenges WHERE app_id = ? LIMIT 1", (app["app_id"],)
        )
        if challenge is not None:
            raise StateError(
                f"申请 {app['app_id']} 存在已登记异议（异议优先于激活），无法激活",
                "payload.app_request_id",
            )
        if app["status"] != "FROZEN":
            raise StateError(
                f"申请 {app['app_id']} 已处于终态 {app['status']}，无法激活",
                "payload.app_request_id",
            )
        if account["current_generation"] != app["generation"]:
            raise StateError(
                f"申请世代 {app['generation']} 已不是当前世代 "
                f"{account['current_generation']}",
                "payload.app_request_id",
            )
        if at <= app["freeze_until"]:
            raise StateError(
                f"冻结窗口尚未结束（截止点 {app['freeze_until']}，当前时刻 {at}）",
                "at",
            )

        proof_ids = json.loads(app["proof_ids_json"])
        # 绑定复核：证明粘在账号/申请/世代/配方版本/新认证器摘要上
        bound_fingerprints: list[dict] = []
        for pid in proof_ids:
            row = self._one("SELECT * FROM proofs WHERE proof_id = ?", (pid,))
            if row is None:
                raise StateError(f"预留证明 {pid} 不存在", "payload.app_request_id")
            if row["account_id"] != app["account_id"]:
                raise StateError(f"证明 {pid} 不属于申请账号", "payload.app_request_id")
            ok, reason = self._proof_effective(row, at)
            if not ok:
                raise StateError(
                    f"预留证明 {pid} 已失效（{reason}），必须重新预留并重走完整冻结窗口",
                    "payload.app_request_id",
                    {"proof_id": pid, "reason": reason},
                )
            res = self._one(
                "SELECT 1 FROM reservations WHERE proof_id = ? AND app_id = ?",
                (pid, app["app_id"]),
            )
            if res is None:
                raise StateError(f"证明 {pid} 未绑定到本申请预留", "payload.app_request_id")
            trustee = self._trustee_effective(row["trustee_id"], at)
            bound_fingerprints.append(
                {
                    "proof_id": pid,
                    "proof_type": row["proof_type"],
                    "digest": row["digest"],
                    "organization": trustee["organization"],
                    "custody_domain": trustee["custody_domain"],
                }
            )

        # ---- 提交新身份世代 ----
        new_generation = app["generation"] + 1
        self.conn.execute(
            "INSERT INTO generations(account_id, generation, authenticator, superseded) VALUES (?,?,?,0)",
            (app["account_id"], new_generation, app["new_authenticator_digest"]),
        )
        self.conn.execute(
            "UPDATE generations SET superseded = 1 WHERE account_id = ? AND generation = ?",
            (app["account_id"], app["generation"]),
        )
        self.conn.execute(
            "UPDATE accounts SET current_generation = ?, active_authenticator = ? WHERE account_id = ?",
            (new_generation, app["new_authenticator_digest"], app["account_id"]),
        )

        # 同世代其余申请原子取消；其预留证明保持锁定，不可再用于后续预留
        siblings = self._all(
            "SELECT * FROM applications WHERE account_id = ? AND generation = ? AND app_id <> ?",
            (app["account_id"], app["generation"], app["app_id"]),
        )
        cancelled_siblings: list[str] = []
        for sib in siblings:
            # 只原子取消仍在推进中的申请；已 CANCELLED/CHALLENGED 的终态保持不变
            if sib["status"] in ("FROZEN",):
                sib_receipt = {
                    "request_id": sib["request_id"],
                    "app_id": sib["app_id"],
                    "result": "cancelled_by_supersession",
                    "at": at,
                    "status": "SUPERSEDED",
                }
                self.conn.execute(
                    "UPDATE applications SET status = 'SUPERSEDED', terminal_receipt_json = ? WHERE app_id = ?",
                    (dumps(sib_receipt), sib["app_id"]),
                )
                cancelled_siblings.append(sib["app_id"])

        # 证明消费：本次选中的证明标记 consumed，永久绑定本次恢复
        for pid in proof_ids:
            self.conn.execute(
                "UPDATE proofs SET revoked_at = COALESCE(revoked_at, ?) WHERE proof_id = ?",
                (at, pid),
            )

        receipt = {
            "request_id": event["request_id"],
            "app_id": app["app_id"],
            "result": "activated",
            "account_id": app["account_id"],
            "previous_generation": app["generation"],
            "new_generation": new_generation,
            "recipe_id": app["recipe_id"],
            "recipe_version": app["recipe_version"],
            "proof_ids": proof_ids,
            "proof_fingerprints": bound_fingerprints,
            "new_authenticator_digest": app["new_authenticator_digest"],
            "superseded_applications": cancelled_siblings,
            "at": at,
            "status": "ACTIVATED",
        }
        self.conn.execute(
            "UPDATE applications SET status = 'ACTIVATED', terminal_receipt_json = ? WHERE app_id = ?",
            (dumps(receipt), app["app_id"]),
        )
        return receipt
