import unittest

from solution.errors import ValidationError
from solution.validation import canonical_content, validate_case, validate_simulate


class ValidationTests(unittest.TestCase):
    def test_missing_field_reports_jsonpath(self):
        bad = {"events": [{"cmd": "register_account", "at": 1, "seq": 1,
                           "request_id": "r1", "payload": {"account_id": "acct-a"}}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_case(bad)
        self.assertEqual(ctx.exception.code, "E_VALIDATION")
        self.assertEqual(ctx.exception.field, "events[0].payload.initial_authenticator")

    def test_wrong_type_reports_kind_and_path(self):
        bad = {"events": [{"cmd": "reserve", "at": "x", "seq": 1,
                           "request_id": "r1", "payload": {}}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_case(bad)
        self.assertEqual(ctx.exception.field, "events[0].at")
        self.assertIn("整数", ctx.exception.message)

    def test_unknown_command(self):
        bad = {"events": [{"cmd": "explode", "at": 1, "seq": 1, "payload": {}}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_case(bad)
        self.assertEqual(ctx.exception.field, "events[0].cmd")

    def test_stateful_command_requires_request_id(self):
        bad = {"events": [{"cmd": "cancel", "at": 1, "seq": 1,
                           "payload": {"app_request_id": "r0"}}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_case(bad)
        self.assertEqual(ctx.exception.field, "events[0].request_id")

    def test_duplicate_proof_type_in_recipe(self):
        bad = {
            "account_id": "a",
            "candidates": [
                {"proof_id": "p1", "organization": "o1", "custody_domain": "d1",
                 "proof_type": "t1", "cost": 1},
            ],
            "recipes": [{
                "recipe_id": "rp", "version": 1,
                "requirements": [
                    {"proof_type": "t1", "count": 1},
                    {"proof_type": "t1", "count": 1},
                ],
            }],
        }
        with self.assertRaises(ValidationError) as ctx:
            validate_simulate(bad)
        self.assertIn("重复", ctx.exception.message)

    def test_simulate_domain_cap_and_duplicate_ids(self):
        base = {
            "account_id": "a",
            "candidates": [
                {"proof_id": "p1", "organization": "o1", "custody_domain": "d1",
                 "proof_type": "t1", "cost": 1},
                {"proof_id": "p1", "organization": "o1", "custody_domain": "d2",
                 "proof_type": "t1", "cost": 1},
            ],
            "recipes": [{"recipe_id": "rp", "version": 1,
                         "requirements": [{"proof_type": "t1", "count": 1}]}],
        }
        with self.assertRaises(ValidationError) as ctx:
            validate_simulate(base)
        self.assertIn("唯一", ctx.exception.message)

        nine = {
            "account_id": "a",
            "candidates": [
                {"proof_id": f"p{i}", "organization": "o", "custody_domain": f"d{i}",
                 "proof_type": "t1", "cost": 1}
                for i in range(9)
            ],
            "recipes": [{"recipe_id": "rp", "version": 1,
                         "requirements": [{"proof_type": "t1", "count": 1}]}],
        }
        with self.assertRaises(ValidationError) as ctx:
            validate_simulate(nine)
        self.assertIn("8", ctx.exception.message)

    def test_canonical_content_ignores_seq_only(self):
        e1 = {"cmd": "cancel", "at": 1, "seq": 1, "request_id": "r",
              "payload": {"app_request_id": "a", "reason": ""}}
        e2 = dict(e1, seq=99)
        self.assertEqual(canonical_content(e1), canonical_content(e2))
        e3 = dict(e1, at=2)
        self.assertNotEqual(canonical_content(e1), canonical_content(e3))

    def test_valid_window_must_be_strict(self):
        bad = {"events": [{
            "cmd": "register_trustee", "at": 1, "seq": 1, "request_id": "r",
            "payload": {"trustee_id": "t1", "organization": "o",
                        "custody_domain": "d", "valid_from": 5, "valid_until": 5},
        }]}
        with self.assertRaises(ValidationError) as ctx:
            validate_case(bad)
        self.assertEqual(ctx.exception.field, "events[0].payload.valid_until")


if __name__ == "__main__":
    unittest.main()
