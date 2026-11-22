import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, cwd=None):
    # -I 隔离模式会忽略 PYTHONPATH/cwd，用实体 runner 文件作为入口，
    # 既保持隔离（忽略用户站点包、环境注入的 json.py 等），又避开 -c 引号问题。
    runner = str(ROOT / "solution" / "runner.py")
    proc = subprocess.run(
        [sys.executable, "-I", runner, *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return proc


def write_case(tmpdir: Path, name: str, payload) -> str:
    path = tmpdir / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.db = str(self.tmpdir / "state.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self, events, db=None):
        case = write_case(self.tmpdir, "case.json", {"events": events})
        proc = run_cli("ingest", case, "--db", db or self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        return json.loads(proc.stdout)

    def test_deterministic_output_is_byte_stable(self):
        events = [{
            "cmd": "register_account", "at": 0, "seq": 1, "request_id": "r1",
            "payload": {"account_id": "acct", "initial_authenticator": "old",
                        "security_contact": "sc"},
        }]
        case = write_case(self.tmpdir, "c.json", {"events": events})
        out1 = run_cli("ingest", case, "--db", str(self.tmpdir / "a.sqlite")).stdout
        out2 = run_cli("ingest", case, "--db", str(self.tmpdir / "b.sqlite")).stdout
        self.assertEqual(out1, out2)
        self.assertTrue(out1.endswith("\n"))
        parsed = json.loads(out1)
        self.assertTrue(parsed["ok"])

    def test_validation_error_exit_code_2_and_field_location(self):
        case = write_case(self.tmpdir, "bad.json", {"events": [
            {"cmd": "register_account", "at": 0, "seq": 1, "request_id": "r",
             "payload": {"account_id": "acct"}},
        ]})
        proc = run_cli("ingest", case, "--db", self.db)
        self.assertEqual(proc.returncode, 2)
        err = json.loads(proc.stdout)["error"]
        self.assertEqual(err["code"], "E_VALIDATION")
        self.assertEqual(err["field"], "events[0].payload.initial_authenticator")

    def test_state_error_exit_code_1(self):
        self._ingest([{
            "cmd": "register_account", "at": 0, "seq": 1, "request_id": "r1",
            "payload": {"account_id": "acct", "initial_authenticator": "old",
                        "security_contact": "sc"},
        }])
        case = write_case(self.tmpdir, "dup.json", {"events": [{
            "cmd": "register_account", "at": 1, "seq": 1, "request_id": "r2",
            "payload": {"account_id": "acct", "initial_authenticator": "x",
                        "security_contact": "sc"},
        }]})
        proc = run_cli("ingest", case, "--db", self.db)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "E_STATE")

    def test_single_event_command_must_match_cli(self):
        case = write_case(self.tmpdir, "one.json", {
            "cmd": "register_account", "at": 0, "seq": 1, "request_id": "r1",
            "payload": {"account_id": "acct", "initial_authenticator": "old",
                        "security_contact": "sc"},
        })
        proc = run_cli("register_account", case, "--db", self.db)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["result"], "account_registered")

        mismatch = write_case(self.tmpdir, "mm.json", {
            "cmd": "cancel", "at": 1, "seq": 1, "request_id": "rx",
            "payload": {"app_request_id": "r1"},
        })
        proc = run_cli("register_account", mismatch, "--db", self.db)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "E_USAGE")

    def test_simulate_loss_is_offline_and_deterministic(self):
        payload = {
            "account_id": "acct",
            "candidates": [
                {"proof_id": "p1", "organization": "o1", "custody_domain": "d1",
                 "proof_type": "key", "cost": 10},
                {"proof_id": "p2", "organization": "o2", "custody_domain": "d2",
                 "proof_type": "key", "cost": 10},
            ],
            "recipes": [{"recipe_id": "rp", "version": 1,
                         "requirements": [{"proof_type": "key", "count": 1}]}],
        }
        case = write_case(self.tmpdir, "sim.json", payload)
        proc1 = run_cli("simulate_loss", case)
        proc2 = run_cli("simulate_loss", case)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertEqual(proc1.stdout, proc2.stdout)
        out = json.loads(proc1.stdout)
        self.assertEqual(out["result"], "prepared_list")
        # 两个保管域各一条 key：任一域失效都必须还剩一条，固定清单含两条
        self.assertEqual(out["prepared_count"], 2)

    def test_simulate_no_plan_returns_exit_1_with_diagnosis(self):
        payload = {
            "account_id": "acct",
            "candidates": [
                {"proof_id": "p1", "organization": "o1", "custody_domain": "d1",
                 "proof_type": "key", "cost": 10},
            ],
            "recipes": [{"recipe_id": "rp", "version": 1,
                         "requirements": [{"proof_type": "key", "count": 2}]}],
            "custody_domains": ["d1", "d2"],
        }
        case = write_case(self.tmpdir, "noplan.json", payload)
        proc = run_cli("simulate_loss", case)
        self.assertEqual(proc.returncode, 1)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"]["code"], "E_NO_RECOVERY_PLAN")
        self.assertIn("first_failed_scenario", out["error"]["details"])

    def test_crash_before_commit_then_clean_retry(self):
        self._ingest([{
            "cmd": "register_account", "at": 0, "seq": 1, "request_id": "r1",
            "payload": {"account_id": "acct", "initial_authenticator": "old",
                        "security_contact": "sc"},
        }])
        reserve = write_case(self.tmpdir, "res.json", {
            "cmd": "reserve", "at": 5, "seq": 1, "request_id": "r-res",
            "payload": {"account_id": "acct", "generation": 1,
                        "new_authenticator_digest": "new", "freeze_until": 10},
        })
        # 无可用配方会先报 E_STATE；这里先补齐受托人/证明/配方再测崩溃
        setup = write_case(self.tmpdir, "setup.json", {"events": [
            {"cmd": "register_trustee", "at": 0, "seq": 1, "request_id": "rt",
             "payload": {"trustee_id": "t1", "organization": "o1",
                         "custody_domain": "d1", "valid_from": 0, "valid_until": 100}},
            {"cmd": "issue_proof", "at": 1, "seq": 1, "request_id": "rp1",
             "payload": {"proof_id": "p1", "account_id": "acct", "trustee_id": "t1",
                         "proof_type": "key", "digest": "g1", "issued_at": 1,
                         "valid_until": 90, "cost": 1}},
            {"cmd": "register_recipe", "at": 2, "seq": 1, "request_id": "rr",
             "payload": {"recipe_id": "rp", "version": 1,
                         "requirements": [{"proof_type": "key", "count": 1}],
                         "active_from": 2}},
        ]})
        self.assertEqual(run_cli("ingest", setup, "--db", self.db).returncode, 0)

        crashed = run_cli("reserve", reserve, "--db", self.db,
                          "--crash-before-commit")
        self.assertEqual(crashed.returncode, 4)
        self.assertEqual(json.loads(crashed.stdout)["error"]["code"],
                         "E_SIMULATED_CRASH")

        # 崩溃后重跑同一请求：原请求未登记，干净成功
        ok = run_cli("reserve", reserve, "--db", self.db)
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)
        self.assertEqual(json.loads(ok.stdout)["result"], "reserved")

    def test_stateful_command_without_db_is_usage_error(self):
        case = write_case(self.tmpdir, "x.json", {
            "cmd": "get_receipt", "at": 1, "seq": 1,
            "payload": {"request_id": "r"},
        })
        proc = run_cli("get_receipt", case)
        self.assertEqual(proc.returncode, 3)

    def test_simulate_with_db_rejects_account_that_already_applied(self):
        # 先在库里给账号建出一条恢复申请
        self._ingest([{
            "cmd": "register_account", "at": 0, "seq": 1, "request_id": "r1",
            "payload": {"account_id": "acct", "initial_authenticator": "old",
                        "security_contact": "sc"},
        }])
        self._ingest([{
            "cmd": "register_trustee", "at": 0, "seq": 1, "request_id": "rt",
            "payload": {"trustee_id": "t1", "organization": "o1",
                        "custody_domain": "d1", "valid_from": 0, "valid_until": 100},
        }, {
            "cmd": "issue_proof", "at": 1, "seq": 1, "request_id": "rp",
            "payload": {"proof_id": "p1", "account_id": "acct", "trustee_id": "t1",
                        "proof_type": "key", "digest": "g", "issued_at": 1,
                        "valid_until": 90, "cost": 1},
        }, {
            "cmd": "register_recipe", "at": 2, "seq": 1, "request_id": "rr",
            "payload": {"recipe_id": "rpz", "version": 1,
                        "requirements": [{"proof_type": "key", "count": 1}],
                        "active_from": 2},
        }, {
            "cmd": "reserve", "at": 3, "seq": 1, "request_id": "rres",
            "payload": {"account_id": "acct", "generation": 1,
                        "new_authenticator_digest": "new", "freeze_until": 10},
        }])
        case = write_case(self.tmpdir, "sim2.json", {
            "account_id": "acct",
            "candidates": [
                {"proof_id": "x1", "organization": "o1", "custody_domain": "d1",
                 "proof_type": "key", "cost": 1},
            ],
            "recipes": [{"recipe_id": "rpz", "version": 1,
                         "requirements": [{"proof_type": "key", "count": 1}]}],
        })
        proc = run_cli("simulate_loss", case, "--db", self.db)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "E_STATE")


if __name__ == "__main__":
    unittest.main()
