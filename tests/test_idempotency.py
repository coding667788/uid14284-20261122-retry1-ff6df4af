import unittest

from solution.db import connect
from solution.errors import ConflictError, StateError

from common import ev, new_db_path, run_events
from test_lifecycle import bootstrap_events


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.path = new_db_path()
        self.conn = connect(self.path)
        run_events(self.conn, bootstrap_events())

    def tearDown(self):
        self.conn.close()

    def test_same_id_same_content_replays_stored_receipt(self):
        payload = {"account_id": "acct", "generation": 1,
                   "new_authenticator_digest": "auth-new", "freeze_until": 20}
        first = run_events(self.conn, [ev("reserve", 10, 1, "req-res", payload)])
        # 重放：seq 不同也不影响内容指纹（seq 不参与），返回只读原回执
        replay = run_events(self.conn, [
            ev("reserve", 10, 7, "req-res", dict(payload)),
        ])
        self.assertEqual(first[0], replay[0])
        # 没有产生第二条申请
        count = self.conn.execute(
            "SELECT COUNT(*) c FROM applications WHERE request_id='req-res'").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_same_id_different_content_fails_whole_batch(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])
        before = self.conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
        with self.assertRaises(ConflictError) as ctx:
            run_events(self.conn, [
                # 同 ID，但冻结截止点不同
                ev("reserve", 10, 2, "req-res",
                   {"account_id": "acct", "generation": 1,
                    "new_authenticator_digest": "auth-new", "freeze_until": 99}),
            ])
        self.assertEqual(ctx.exception.code, "E_IDEMPOTENCY_CONFLICT")
        after = self.conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
        self.assertEqual(before, after)

    def test_batch_with_conflict_rolls_back_everything(self):
        # 批次里前面还有一个正常的新请求；冲突时整批回滚，连正常请求也不落库
        with self.assertRaises(ConflictError):
            run_events(self.conn, [
                ev("revoke_proof", 12, 1, "req-rev-new",
                   {"proof_id": "p3", "revoked_at": 12}),
                ev("reserve", 10, 2, "req-rev-new",
                   {"account_id": "acct", "generation": 1,
                    "new_authenticator_digest": "auth-new", "freeze_until": 99}),
            ])
        # req-rev-new 没有任何登记
        row = self.conn.execute(
            "SELECT 1 FROM requests WHERE request_id='req-rev-new'").fetchone()
        self.assertIsNone(row)

    def test_get_receipt_reads_stable_terminal_receipt(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
            ev("cancel", 11, 1, "req-cancel",
               {"app_request_id": "req-res", "reason": "abort"}),
        ])
        got = run_events(self.conn, [
            ev("get_receipt", 30, 1, None, {"request_id": "req-cancel"}),
        ])
        self.assertEqual(got[0]["status"], "CANCELLED")
        self.assertEqual(got[0]["app_id"], "app:req-res")

        with self.assertRaises(StateError):
            run_events(self.conn, [
                ev("get_receipt", 30, 1, None, {"request_id": "never-existed"}),
            ])


if __name__ == "__main__":
    unittest.main()
