import json
import unittest

from solution import db as dbmod
from solution.db import connect
from solution.engine import Engine
from solution.errors import StateError
from solution.validation import validate_case

from common import ev, new_db_path, run_events


def bootstrap_events():
    """账号 + 2 受托人（不同组织/保管域）+ 3 证明 + 一份双证明配方。"""
    return [
        ev("register_account", 0, 1, "req-acct",
           {"account_id": "acct", "initial_authenticator": "auth-old",
            "security_contact": "contact-1"}),
        ev("register_trustee", 0, 2, "req-t1",
           {"trustee_id": "t1", "organization": "org-a", "custody_domain": "dom-1",
            "valid_from": 0, "valid_until": 1000}),
        ev("register_trustee", 0, 3, "req-t2",
           {"trustee_id": "t2", "organization": "org-b", "custody_domain": "dom-2",
            "valid_from": 0, "valid_until": 1000}),
        ev("issue_proof", 1, 1, "req-p1",
           {"proof_id": "p1", "account_id": "acct", "trustee_id": "t1",
            "proof_type": "key", "digest": "dig-p1", "issued_at": 1,
            "valid_until": 900, "cost": 10}),
        ev("issue_proof", 1, 2, "req-p2",
           {"proof_id": "p2", "account_id": "acct", "trustee_id": "t2",
            "proof_type": "key", "digest": "dig-p2", "issued_at": 1,
            "valid_until": 900, "cost": 20}),
        ev("issue_proof", 1, 3, "req-p3",
           {"proof_id": "p3", "account_id": "acct", "trustee_id": "t1",
            "proof_type": "seal", "digest": "dig-p3", "issued_at": 1,
            "valid_until": 900, "cost": 30}),
        ev("register_recipe", 2, 1, "req-recipe",
           {"recipe_id": "rp", "version": 1,
            "requirements": [
                {"proof_type": "key", "count": 2,
                 "distinct_organizations": True, "distinct_custody_domains": True},
            ],
            "active_from": 2}),
    ]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.path = new_db_path()
        self.conn = connect(self.path)
        run_events(self.conn, bootstrap_events())

    def tearDown(self):
        self.conn.close()

    def test_full_reserve_freeze_activate_generations(self):
        receipts = run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
            ev("activate", 21, 1, "req-act",
               {"app_request_id": "req-res"}),
        ])
        reserve, activated = receipts
        self.assertEqual(reserve["result"], "reserved")
        self.assertEqual(reserve["proof_ids"], ["p1", "p2"])
        self.assertEqual(activated["result"], "activated")
        self.assertEqual(activated["new_generation"], 2)
        self.assertEqual(activated["previous_generation"], 1)
        # 指纹把证明粘在账号/世代/配方版本/新认证器摘要的恢复事件上
        self.assertEqual({f["custody_domain"] for f in activated["proof_fingerprints"]},
                         {"dom-1", "dom-2"})

        acct = self.conn.execute(
            "SELECT * FROM accounts WHERE account_id = 'acct'").fetchone()
        self.assertEqual(acct["current_generation"], 2)
        self.assertEqual(acct["active_authenticator"], "auth-new")
        old_gen = self.conn.execute(
            "SELECT * FROM generations WHERE account_id='acct' AND generation=1").fetchone()
        self.assertEqual(old_gen["superseded"], 1)
        # 旧认证器不得成为新世代有效凭证
        self.assertNotEqual(acct["active_authenticator"], "auth-old")

    def test_activate_before_freeze_end_rejected(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("activate", 20, 1, "req-act", {"app_request_id": "req-res"}),
            ])
        self.assertIn("冻结窗口尚未结束", ctx.exception.message)

    def test_cancelled_app_locks_its_proofs_permanently(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res-1",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-x", "freeze_until": 20}),
            ev("cancel", 11, 1, "req-cancel",
               {"app_request_id": "req-res-1", "reason": "user abort"}),
        ])
        terminal = self.conn.execute(
            "SELECT terminal_receipt_json FROM applications WHERE app_id='app:req-res-1'"
        ).fetchone()
        receipt = json.loads(terminal[0])
        self.assertEqual(receipt["status"], "CANCELLED")
        self.assertEqual(receipt["result"], "cancelled")

        # 第二次预留：p1/p2 被锁，只剩 p3，配方要求 2 把 key，无法满足
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("reserve", 12, 1, "req-res-2",
                   {"account_id": "acct", "generation": 1,
                    "new_authenticator_digest": "auth-y", "freeze_until": 30}),
            ])
        self.assertIn("没有任何预先批准配方", ctx.exception.message)

    def test_revoked_proof_during_freeze_forces_new_reservation(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
            ev("revoke_proof", 15, 1, "req-rev",
               {"proof_id": "p1", "revoked_at": 15}),
        ])
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
            ])
        self.assertEqual(ctx.exception.details["reason"], "revoked")
        self.assertIn("重新预留", ctx.exception.message)

    def test_parallel_apps_one_generation_single_success_supersedes_rest(self):
        # 新增 p4（key, org-b/dom-2 不够独立），先注册一个只要求 1 把 key 的配方
        run_events(self.conn, [
            ev("register_recipe", 3, 1, "req-recipe2",
               {"recipe_id": "rp2", "version": 1,
                "requirements": [{"proof_type": "key", "count": 1}],
                "active_from": 3}),
        ])
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res-a",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-a", "freeze_until": 20}),
            ev("reserve", 10, 2, "req-res-b",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-b", "freeze_until": 20}),
        ])
        # 两个并行申请各锁了不同的最小证明集（条数决胜，rp2 只需 1 条）
        apps = self.conn.execute(
            "SELECT app_id, proof_ids_json FROM applications ORDER BY request_id"
        ).fetchall()
        self.assertEqual(len(apps), 2)
        chosen_sets = {row[0]: json.loads(row[1]) for row in apps}
        self.assertNotEqual(
            chosen_sets["app:req-res-a"], chosen_sets["app:req-res-b"]
        )

        run_events(self.conn, [
            ev("activate", 21, 1, "req-act-a", {"app_request_id": "req-res-a"}),
        ])
        sib = self.conn.execute(
            "SELECT status, terminal_receipt_json FROM applications WHERE app_id='app:req-res-b'"
        ).fetchone()
        self.assertEqual(sib["status"], "SUPERSEDED")
        sib_receipt = json.loads(sib["terminal_receipt_json"])
        self.assertEqual(sib_receipt["result"], "cancelled_by_supersession")

        # 同世代第二个申请再激活必须失败
        with self.assertRaises(StateError):
            run_events(self.conn, [
                ev("activate", 22, 1, "req-act-b", {"app_request_id": "req-res-b"}),
            ])

        # 未被选中的认证器 auth-b 不得变成新世代凭证
        acct = self.conn.execute(
            "SELECT active_authenticator FROM accounts WHERE account_id='acct'").fetchone()
        self.assertEqual(acct["active_authenticator"], "auth-a")

    def test_stale_generation_application_rejected(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
            ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
        ])
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("reserve", 30, 1, "req-res-old",
                   {"account_id": "acct", "generation": 1,
                    "new_authenticator_digest": "auth-z", "freeze_until": 40}),
            ])
        self.assertIn("不是当前世代", ctx.exception.message)

    def test_trustee_window_change_invalidates_proof(self):
        # 受托人 t1 变更为 t=50 之后才有效，t=12 签发证明时会找不到有效受托人
        run_events(self.conn, [
            ev("update_trustee", 5, 1, "req-t1-upd",
               {"trustee_id": "t1", "organization": "org-a", "custody_domain": "dom-9",
                "valid_from": 50, "valid_until": 1000}),
        ])
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("issue_proof", 12, 1, "req-p9",
                   {"proof_id": "p9", "account_id": "acct", "trustee_id": "t1",
                    "proof_type": "key", "digest": "dig-p9", "issued_at": 12,
                    "valid_until": 900, "cost": 5}),
            ])
        self.assertIn("不处于有效保管期", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
