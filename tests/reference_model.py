"""独立小参考模型（测试专用，刻意与生产代码风格/实现不同）。

用途：用朴素枚举独立回放两件事，供断言测试交叉验证生产实现：

1. ``enumerate_prepared_sets``：枚举 simulate_loss 的全部准备清单，
   按 (费用, 条数, ID 列表) 找全局最优，与分支限界求解器对照；
2. ``brute_force_witness``：对单份配方枚举证明组合，验证选型模块的
   最小/最便宜证人。

该模块不 import 生产包的任何业务模块，只复用标准库。
"""

from __future__ import annotations

import itertools


def _independence_ok(chosen, req):
    if req.get("distinct_organizations", False) and len({c["organization"] for c in chosen}) != len(chosen):
        return False
    if req.get("distinct_custody_domains", False) and len({c["custody_domain"] for c in chosen}) != len(chosen):
        return False
    return True


def brute_force_witness(recipe, candidates, *, cheapest=False):
    """朴素枚举：返回 (proof_ids, cost) 或 None。"""
    by_type = {}
    for c in candidates:
        by_type.setdefault(c["proof_type"], []).append(c)

    options_per_req = []
    for req in recipe["requirements"]:
        pool = sorted(by_type.get(req["proof_type"], []), key=lambda c: c["proof_id"])
        feasible = [
            list(combo)
            for combo in itertools.combinations(pool, req["count"])
            if _independence_ok(combo, req)
        ]
        if not feasible:
            return None
        options_per_req.append(feasible)

    best = None
    for parts in itertools.product(*options_per_req):
        flat = sorted((c for part in parts for c in part), key=lambda c: c["proof_id"])
        ids = tuple(c["proof_id"] for c in flat)
        cost = sum(c["cost"] for c in flat)
        key = (cost, len(ids), ids) if cheapest else (len(ids), ids)
        if best is None or key < best[0]:
            best = (key, ids, cost)
    return list(best[1]), best[2]


def _any_recipe_works(recipes, pool):
    for recipe in recipes:
        witness = brute_force_witness(recipe, pool, cheapest=True)
        if witness is not None:
            return True
    return False


def enumerate_prepared_sets(spec):
    """枚举所有可行准备清单，返回 (最优ids列表, 费用)；全部不可行返回 None。"""
    drill_time = spec["drill_time"]
    recipes = spec["recipes"]
    candidates = [
        c
        for c in spec["candidates"]
        if c["valid_from"] <= drill_time < c["valid_until"]
    ]
    candidates = sorted(candidates, key=lambda c: c["proof_id"])
    scenarios = [None] + list(spec["domains"])

    best = None
    # 2^26 ≈ 6700 万，仅在测试用的小目录（<= 14 条）上调用
    for mask in range(1, 1 << len(candidates)):
        prepared = [candidates[i] for i in range(len(candidates)) if mask & (1 << i)]
        ok_all = True
        for failed in scenarios:
            pool = prepared if failed is None else [
                c for c in prepared if c["custody_domain"] != failed
            ]
            if not _any_recipe_works(recipes, pool):
                ok_all = False
                break
        if not ok_all:
            continue
        ids = tuple(c["proof_id"] for c in prepared)
        cost = sum(c["cost"] for c in prepared)
        key = (cost, len(ids), ids)
        if best is None or key < best[0]:
            best = (key, list(ids), cost)
    if best is None:
        return None
    return best[1], best[2]
