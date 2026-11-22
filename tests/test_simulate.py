import time
import unittest

from solution import simulate as simulate_mod
from solution.validation import validate_simulate

from reference_model import brute_force_witness, enumerate_prepared_sets
from solution import selection


def cand(pid, org, dom, ptype, cost, vf=0, vu=1000):
    return {"proof_id": pid, "organization": org, "custody_domain": dom,
            "proof_type": ptype, "cost": cost, "valid_from": vf, "valid_until": vu}


def recipe(rid, reqs, version=1):
    return {"recipe_id": rid, "version": version, "requirements": reqs}


def spec(candidates, recipes, domains=None, drill_time=0, account="acct"):
    if domains is None:
        domains = sorted({c["custody_domain"] for c in candidates})
    return validate_simulate({
        "account_id": account,
        "candidates": candidates,
        "recipes": recipes,
        "custody_domains": domains,
        "drill_time": drill_time,
    })


class SimulateBasicTests(unittest.TestCase):
    def test_no_failure_scenario_and_domain_failure_all_covered(self):
        candidates = [
            cand("p1", "org-a", "d1", "key", 10),
            cand("p2", "org-b", "d2", "key", 10),
        ]
        s = spec(candidates, [recipe("rp", [{"proof_type": "key", "count": 1}])])
        out = simulate_mod.solve(s)
        self.assertEqual(out["result"], "prepared_list")
        # d1 失效时必须还有 d2 的 key，反之亦然：费用决胜下两条都要准备
        self.assertEqual(out["prepared_count"], 2)
        self.assertEqual(set(out["prepared_proof_ids"]), {"p1", "p2"})
        self.assertEqual(len(out["scenario_plans"]), 3)  # S0 + 2 个域

    def test_same_organization_cross_domain_is_not_independent(self):
        # 同组织两条证明分布在两个保管域；另一个组织也有两域备份。
        # 配方要求 2 把 key 且组织、保管域双独立：
        # 同组织的 p1/p2 永远不能同时入选（“跨保管域但不独立”）。
        candidates = [
            cand("p1", "org-a", "d1", "key", 10),
            cand("p2", "org-a", "d2", "key", 10),
            cand("p3", "org-b", "d3", "key", 10),
            cand("p4", "org-b", "d4", "key", 10),
        ]
        s = spec(candidates, [
            recipe("rp", [{"proof_type": "key", "count": 2,
                           "distinct_organizations": True,
                           "distinct_custody_domains": True}]),
        ], domains=["d1", "d2", "d3", "d4"])
        out = simulate_mod.solve(s)
        # 每个组织都要扛住自己两个域分别失效，四条全部进入固定清单
        self.assertEqual(set(out["prepared_proof_ids"]), {"p1", "p2", "p3", "p4"})
        # 任一情形的证人都必须横跨两个组织
        for plan in out["scenario_plans"]:
            orgs = {next(c["organization"] for c in candidates
                         if c["proof_id"] == pid) for pid in plan["proof_ids"]}
            self.assertEqual(orgs, {"org-a", "org-b"})

        # 只有同组织两条证明时：即使跨了保管域也不满足组织独立 -> 无解诊断。
        # 无故障情形 S0 本身就无法满足，因此第一个失败情形是 S0。
        s_bad = spec(candidates[:2], [
            recipe("rp", [{"proof_type": "key", "count": 2,
                           "distinct_organizations": True}]),
        ], domains=["d1", "d2"])
        bad = simulate_mod.solve(s_bad)
        self.assertEqual(bad["result"], "no_recovery_plan")
        self.assertEqual(bad["first_failed_scenario"], "S0")
        self.assertEqual(
            bad["recipe_failures"][0]["missing_requirements"][0]["blocked_reason"],
            "distinct_organizations_unsatisfied",
        )

    def test_cheapest_recipe_fails_backup_recipe_works(self):
        candidates = [
            # 便宜配方需要 d1 的 seal；d1 失效时它不可用
            cand("seal1", "org-a", "d1", "seal", 1),
            # 备用配方：两把 d2/d3 的 key，更贵但能扛 d1 失效
            cand("k1", "org-b", "d2", "key", 40),
            cand("k2", "org-c", "d3", "key", 40),
        ]
        s = spec(candidates, [
            recipe("cheap", [{"proof_type": "seal", "count": 1}]),
            recipe("backup", [{"proof_type": "key", "count": 2,
                               "distinct_custody_domains": True}]),
        ])
        out = simulate_mod.solve(s)
        self.assertEqual(out["result"], "prepared_list")
        prepared = set(out["prepared_proof_ids"])
        self.assertEqual(prepared, {"seal1", "k1", "k2"})
        plans = {p["scenario"]: p["recipe_id"] for p in out["scenario_plans"]}
        self.assertEqual(plans["S0"], "cheap")
        self.assertEqual(plans["S:d1"], "backup")  # 最低成本配方失效，备用顶上

    def test_prepared_list_uses_proof_shared_by_two_recipes(self):
        # r1 要求 2 把不同保管域的 key；r2 只要 1 把 key + 1 把 seal。
        # key 只分布在 d1/d2：d1 或 d2 失效后 r1 凑不齐两个域，必须切换到 r2。
        # 因此固定清单既要为 r1 准备两把 key，也要为 r2 准备 seal；
        # 而 key 证明同时是 r1 与 r2 的共同需要，在多情形证人里被复用。
        candidates = [
            cand("k1", "o1", "d1", "key", 5),
            cand("k2", "o2", "d2", "key", 5),
            cand("s1", "o4", "d3", "seal", 5),
            cand("s2", "o5", "d4", "seal", 5),
        ]
        s = spec(candidates, [
            recipe("r1", [{"proof_type": "key", "count": 2,
                           "distinct_custody_domains": True}]),
            recipe("r2", [{"proof_type": "key", "count": 1},
                          {"proof_type": "seal", "count": 1}]),
        ], domains=["d1", "d2", "d3", "d4"])
        out = simulate_mod.solve(s)
        self.assertEqual(out["result"], "prepared_list")
        prepared = set(out["prepared_proof_ids"])
        self.assertTrue(any(pid.startswith("k") for pid in prepared))
        self.assertTrue(any(pid.startswith("s") for pid in prepared))
        # 至少一条 key 证明被多个情形的配方共同需要（两配方共享 key 的复用）
        key_reuse = {
            pid: names for pid, names in out["reuse"]["shared_proofs"].items()
            if pid.startswith("k")
        }
        self.assertTrue(key_reuse, "应存在被多情形复用的 key 证明")
        pid = next(iter(key_reuse))
        self.assertGreaterEqual(len(key_reuse[pid]), 2)
        # 情形计划在两份配方间切换：无故障用 r1，d1/d2 失效改用 r2
        chosen_recipes = {p["recipe_id"] for p in out["scenario_plans"]}
        self.assertEqual(chosen_recipes, {"r1", "r2"})

    def test_tie_break_cost_then_count_then_ids(self):
        # 三域各一条同费 key：要扛住每个域失效，至少准备两条；
        # 费用/条数相同的组合中，取升序 ID 列表最小的 [a, b]
        candidates = [
            cand("a", "o1", "d1", "key", 5),
            cand("b", "o2", "d2", "key", 5),
            cand("c", "o3", "d3", "key", 5),
        ]
        s = spec(candidates, [recipe("rp", [{"proof_type": "key", "count": 1}])])
        out = simulate_mod.solve(s)
        self.assertEqual(out["prepared_proof_ids"], ["a", "b"])


class SimulateReferenceCrossCheck(unittest.TestCase):
    """用独立枚举小模型交叉验证分支限界求解器。"""

    def _cross_check(self, candidates, recipes, domains):
        s = spec(candidates, recipes, domains=domains)
        fast = simulate_mod.solve(s)
        brute = enumerate_prepared_sets(s)
        if brute is None:
            self.assertEqual(fast["result"], "no_recovery_plan")
            return
        ids, cost = brute
        self.assertEqual(fast["result"], "prepared_list")
        self.assertEqual(fast["prepared_proof_ids"], ids)
        self.assertEqual(fast["total_cost"], cost)

    def test_cross_check_randomish_fixtures(self):
        self._cross_check(
            [cand(f"p{i}", f"o{i % 3}", f"d{i % 4}", ("key", "seal")[i % 2],
                  1 + (i * 7) % 11) for i in range(10)],
            [recipe("r1", [{"proof_type": "key", "count": 2,
                            "distinct_custody_domains": True}]),
             recipe("r2", [{"proof_type": "seal", "count": 1}])],
            [f"d{i}" for i in range(4)],
        )

    def test_cross_check_independence_heavy(self):
        self._cross_check(
            [cand(f"p{i}", f"o{i % 2}", f"d{i % 5}", "key", 1 + i % 3)
             for i in range(12)],
            [recipe("r1", [{"proof_type": "key", "count": 3,
                            "distinct_organizations": True,
                            "distinct_custody_domains": True}])],
            [f"d{i}" for i in range(5)],
        )

    def test_witness_matches_brute_force(self):
        candidates = [
            cand("p1", "o1", "d1", "key", 3),
            cand("p2", "o2", "d2", "key", 1),
            cand("p3", "o3", "d3", "key", 2),
        ]
        rp = recipe("r", [{"proof_type": "key", "count": 2}])
        fast = selection.recipe_witness(rp, candidates, mode="cheapest")
        brute_ids, brute_cost = brute_force_witness(rp, candidates, cheapest=True)
        self.assertEqual(fast["proof_ids"], brute_ids)
        self.assertEqual(fast["cost"], brute_cost)

        fast_min = selection.recipe_witness(rp, candidates, mode="reserve")
        brute_min, _ = brute_force_witness(rp, candidates, cheapest=False)
        self.assertEqual(fast_min["proof_ids"], brute_min)


class SimulateScaleTests(unittest.TestCase):
    def test_26_candidates_8_domains_runs_fast(self):
        candidates = []
        for i in range(26):
            candidates.append(cand(
                f"p{i:02d}", f"org{i % 6}", f"dom{i % 8}",
                ("key", "seal", "shard")[i % 3],
                cost=1 + (i * 3) % 9,
            ))
        s = spec(candidates, [
            recipe("r-multi", [
                {"proof_type": "key", "count": 2,
                 "distinct_organizations": True, "distinct_custody_domains": True},
                {"proof_type": "seal", "count": 2,
                 "distinct_custody_domains": True},
            ]),
            recipe("r-shard", [{"proof_type": "shard", "count": 1}]),
        ], domains=[f"dom{i}" for i in range(8)])
        start = time.perf_counter()
        out = simulate_mod.solve(s)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0, f"求解耗时 {elapsed:.2f}s 超过 5 秒预算")
        self.assertEqual(out["result"], "prepared_list")
        # 固定清单必须让全部 9 个情形都有证人
        self.assertEqual(len(out["scenario_plans"]), 9)
        for plan in out["scenario_plans"]:
            self.assertTrue(plan["proof_ids"])

    def test_drill_time_filters_not_yet_valid_candidate(self):
        candidates = [
            cand("p1", "o1", "d1", "key", 1, vf=10, vu=20),
            cand("p2", "o2", "d2", "key", 1, vf=0, vu=20),
            cand("p3", "o3", "d3", "key", 1, vf=0, vu=20),
        ]
        s = spec(candidates, [recipe("r", [{"proof_type": "key", "count": 1}])],
                 domains=["d1", "d2", "d3"], drill_time=5)
        out = simulate_mod.solve(s)
        # p1 在演练时刻尚未生效；可用于覆盖失效情形的只有 p2/p3，
        # 要扛住 d2、d3 分别失效，两者都必须进固定清单
        self.assertEqual(set(out["prepared_proof_ids"]), {"p2", "p3"})


if __name__ == "__main__":
    unittest.main()
