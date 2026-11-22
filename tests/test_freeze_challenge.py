import unittest

from solution.db import connect
from solution.errors import StateError

from common import ev, new_db_path, run_events
from test_lifecycle import bootstrap_events


class FreezeChallengeTests(unittest.TestCase):
    def setUp(self):
        self.path = new_db_path()
        self.conn = connect(self.path)
        run_events(self.conn, bootstrap_events())
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])

    def tearDown(self):
        self.conn.close()

    def test_cutoff_old_authenticator_challenge_has_priority_in_same_batch(self):
        # 同一逻辑时刻 20（=冻结截止点）：seq=1 是撤销事件，seq=2 是旧认证器异议。
        # 规则要求同截止异议优先，因此即使异议 seq 更大也先落库；
        # 随后 t=21 的 activate 必须被异议挡住。
        run_events(self.conn, [
            ev("revoke_proof", 20, 1, "req-rev",
               {"proof_id": "p3", "revoked_at": 20}),
            ev("challenge", 20, 2, "req-chal",
               {"challenger": "old_authenticator", "app_request_id": "req-res"}),
        ])
        # 校验实际处理次序：异议在撤销之后仍被优先处理（状态已是 CHALLENGED）
        app = self.conn.execute(
            "SELECT status FROM applications WHERE app_id='app:req-res'").fetchone()
        self.assertEqual(app["status"], "CHALLENGED")
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
            ])
        self.assertIn("异议", ctx.exception.message)
    def test_cutoff_priority_does_not_apply_when_generation_stale(self):
        # 世代已推进时，同刻异议不享受优先，且本身应被业务拒绝
        self.conn.execute("UPDATE accounts SET current_generation=2 WHERE account_id='acct'")
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("challenge", 20, 2, "req-chal",
                   {"challenger": "old_authenticator", "app_request_id": "req-res"}),
            ])
        self.assertIn("有效世代", ctx.exception.message)

    def test_security_contact_challenge_blocks_activate(self):
        run_events(self.conn, [
            ev("challenge", 18, 1, "req-chal",
               {"challenger": "security_contact", "app_request_id": "req-res"}),
        ])
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
            ])
        self.assertIn("异议", ctx.exception.message)

    def test_challenge_after_cutoff_rejected(self):
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("challenge", 21, 1, "req-chal-late",
                   {"challenger": "old_authenticator", "app_request_id": "req-res"}),
            ])
        self.assertIn("晚于冻结截止点", ctx.exception.message)

    def test_old_authenticator_challenge_on_stale_generation_rejected(self):
        # 直接把账号世代推进，使既有申请的世代不再是有效世代；
        # 旧认证器在截止点的异议既不享受优先，也被业务拒绝。
        self.conn.execute("UPDATE accounts SET current_generation=2 WHERE account_id='acct'")
        with self.assertRaises(StateError) as ctx:
            run_events(self.conn, [
                ev("challenge", 15, 1, "req-chal-stale",
                   {"challenger": "old_authenticator", "app_request_id": "req-res"}),
            ])
        self.assertIn("有效世代", ctx.exception.message)
        # 没有任何异议落库
        count = self.conn.execute(
            "SELECT COUNT(*) c FROM challenges WHERE app_id='app:req-res'").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_non_cutoff_concurrent_events_follow_input_seq(self):
        # 时刻 19（早于截止点 20），seq=1 的安全联系人异议先处理；
        # 即使存在同刻旧认证器异议，也严格按 seq。
        run_events(self.conn, [
            ev("challenge", 19, 1, "req-chal-sc",
               {"challenger": "security_contact", "app_request_id": "req-res"}),
            ev("challenge", 19, 2, "req-chal-oa",
               {"challenger": "old_authenticator", "app_request_id": "req-res"}),
        ])
        rows = self.conn.execute(
            "SELECT challenger FROM challenges WHERE app_id='app:req-res' ORDER BY id").fetchall()
        self.assertEqual([r["challenger"] for r in rows],
                         ["security_contact", "old_authenticator"])

    def test_multiple_cutoff_challenges_all_precede_normal_events(self):
        # 截止点同刻：两条异议（seq=2/3）都要排在普通撤销事件（seq=1）之前，
        # 异议彼此之间仍按 seq 决胜。
        run_events(self.conn, [
            ev("revoke_proof", 20, 1, "req-rev",
               {"proof_id": "p3", "revoked_at": 20}),
            ev("challenge", 20, 2, "req-chal-oa",
               {"challenger": "old_authenticator", "app_request_id": "req-res"}),
            ev("challenge", 20, 3, "req-chal-sc",
               {"challenger": "security_contact", "app_request_id": "req-res"}),
        ])
        rows = self.conn.execute(
            "SELECT challenger FROM challenges WHERE app_id='app:req-res' ORDER BY id").fetchall()
        self.assertEqual([r["challenger"] for r in rows],
                         ["old_authenticator", "security_contact"])
        # 撤销事件也已执行，但申请确定进入异议终态
        revoked = self.conn.execute(
            "SELECT revoked_at FROM proofs WHERE proof_id='p3'").fetchone()
        self.assertEqual(revoked["revoked_at"], 20)
        app = self.conn.execute(
            "SELECT status FROM applications WHERE app_id='app:req-res'").fetchone()
        self.assertEqual(app["status"], "CHALLENGED")


if __name__ == "__main__":
    unittest.main()
