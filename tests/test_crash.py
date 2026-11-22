import json
import unittest

from solution import db as dbmod
from solution.db import connect
from solution.engine import Engine
from solution.validation import validate_case

from common import ev, new_db_path, run_events
from test_lifecycle import bootstrap_events


class CrashAndResponseLossTests(unittest.TestCase):
    def setUp(self):
        self.path = new_db_path()
        self.conn = connect(self.path)
        run_events(self.conn, bootstrap_events())

    def tearDown(self):
        self.conn.close()

    def test_process_exit_before_commit_leaves_no_partial_state(self):
        events = validate_case({"events": [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ]})
        before_apps = self.conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
        before_reqs = self.conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"]

        with self.assertRaises(RuntimeError):
            with dbmod.transaction(self.conn, crash_before_commit=True):
                Engine(self.conn).run_batch(events)

        # 事务已回滚：申请、预留、幂等登记全部不存在，证明未被冻结
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"],
            before_apps,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"],
            before_reqs,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM reservations").fetchone()["c"], 0
        )
        # 回滚后同一请求可以全新执行成功
        receipts = run_events(self.conn, events)
        self.assertEqual(receipts[0]["result"], "reserved")

    def test_response_lost_after_commit_replay_returns_same_receipt(self):
        # 首次提交成功但响应丢失（客户端只知道 request_id）
        first = run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])
        # 重放：只读原回执，不产生第二次预留
        replay = run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])
        self.assertEqual(first[0], replay[0])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) c FROM applications WHERE request_id='req-res'"
            ).fetchone()["c"],
            1,
        )

    def test_activate_replay_after_response_loss_is_stable(self):
        run_events(self.conn, [
            ev("reserve", 10, 1, "req-res",
               {"account_id": "acct", "generation": 1,
                "new_authenticator_digest": "auth-new", "freeze_until": 20}),
        ])
        first = run_events(self.conn, [
            ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
        ])
        # activate 重放：原终态回执被读出，世代不会再推进
        replay = run_events(self.conn, [
            ev("activate", 21, 1, "req-act", {"app_request_id": "req-res"}),
        ])
        self.assertEqual(first[0], replay[0])
        gen = self.conn.execute(
            "SELECT current_generation FROM accounts WHERE account_id='acct'").fetchone()
        self.assertEqual(gen["current_generation"], 2)


if __name__ == "__main__":
    unittest.main()
