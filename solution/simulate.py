"""simulate_loss：离线演练的最小准备清单求解。

给定尚未申请恢复的账号、一份未来可签发的合成证明目录与全部预先批准配方，
求一份**预先固定**的准备清单 P，使下列每个情形都能从 P 中选出一份可行配方：

* ``S0`` 无故障：P 全部可用；
* ``S(d)`` 保管域 d 失效：P 中属于 d 的证明全部不可用，其余可用。

每个情形允许选择不同配方，但 P 本身只有一份。决胜顺序：
总费用 → 准备条数 → 升序 ID 列表。

算法（26 候选 / 8 保管域规模下 CPU 足够）：

1. 先用**全部候选**逐情形验证可行性；任一情形连全部候选都无法满足，
   直接返回第一个失败情形与逐配方缺失要求（无需搜索）。
2. 搜索宇宙收缩为“全部情形全候选证人”的并集——任何最优清单都不会包含
   并集之外的证明（没有任何情形的证人用到它）。
3. 以各情形最便宜证人的并集作为初始上界，再做包含/排除 DFS 分支限界，
   每节点用“当前选择 ∪ 剩余候选”对全部情形做可行性剪枝与费用剪枝。
"""

from __future__ import annotations

from . import selection


def _pool(candidates: list[dict], failed_domain: str | None) -> list[dict]:
    if failed_domain is None:
        return list(candidates)
    return [c for c in candidates if c["custody_domain"] != failed_domain]


def _set_cost(ids: set[str], by_id: dict[str, dict]) -> int:
    return sum(by_id[i]["cost"] for i in ids)


def _better(candidate: set[str], best: set[str], by_id: dict[str, dict]) -> bool:
    key_c = (_set_cost(candidate, by_id), len(candidate), tuple(sorted(candidate)))
    key_b = (_set_cost(best, by_id), len(best), tuple(sorted(best)))
    return key_c < key_b


def solve(spec: dict) -> dict:
    drill_time = spec["drill_time"]
    recipes = spec["recipes"]
    candidates = [
        c
        for c in spec["candidates"]
        if c["valid_from"] <= drill_time < c["valid_until"]
    ]
    candidates.sort(key=lambda c: c["proof_id"])
    by_id = {c["proof_id"]: c for c in candidates}

    # 情形顺序固定：无故障在前，随后保管域按字典序，保证“第一个失败情形”可重现
    scenarios: list[tuple[str, str | None]] = [("S0", None)] + [
        (f"S:{d}", d) for d in spec["domains"]
    ]

    # ---- 步骤 1：全候选可行性，失败直接给诊断 ----
    full_witnesses: dict[str, dict] = {}
    for name, failed in scenarios:
        witness = selection.select_recipe(
            recipes, _pool(candidates, failed), mode="cheapest"
        )
        if witness is None:
            return _no_plan(name, failed, recipes, _pool(candidates, failed))
        full_witnesses[name] = witness

    # ---- 步骤 2：收缩搜索宇宙 ----
    used_ids: set[str] = set()
    for witness in full_witnesses.values():
        used_ids.update(witness["proof_ids"])
    universe = [c for c in candidates if c["proof_id"] in used_ids]

    # 启发式排序：在越多全候选证人中出现的证明越先决策，尽早收紧上界
    frequency: dict[str, int] = {}
    for witness in full_witnesses.values():
        for pid in witness["proof_ids"]:
            frequency[pid] = frequency.get(pid, 0) + 1
    universe.sort(key=lambda c: (-frequency[c["proof_id"]], c["proof_id"]))

    def covers(chosen: set[str], remaining: list[dict]) -> bool:
        """chosen ∪ remaining 是否覆盖全部情形。"""
        avail = [by_id[i] for i in chosen] + remaining
        for _name, failed in scenarios:
            pool = avail if failed is None else [
                c for c in avail if c["custody_domain"] != failed
            ]
            if selection.select_recipe(recipes, pool, mode="cheapest") is None:
                return False
        return True

    def covers_with_chosen_only(chosen: set[str]) -> bool:
        avail = [by_id[i] for i in chosen]
        for _name, failed in scenarios:
            pool = avail if failed is None else [
                c for c in avail if c["custody_domain"] != failed
            ]
            if selection.select_recipe(recipes, pool, mode="cheapest") is None:
                return False
        return True

    # ---- 步骤 3：初始上界 = 各情形最便宜证人的并集 ----
    best = set(used_ids)
    n = len(universe)
    chosen: set[str] = set()
    stats = {"nodes": 0, "pruned_feasibility": 0, "pruned_cost": 0}

    def dfs(i: int) -> None:
        nonlocal best
        stats["nodes"] += 1
        # 当前选择已独立覆盖全部情形：登记为可行解并停止扩张
        if covers_with_chosen_only(chosen):
            if _better(chosen, best, by_id):
                best = set(chosen)
            return
        if i == n:
            return
        # 费用/条数剪枝：继续只会增加费用与条数
        cur_cost = _set_cost(chosen, by_id)
        if cur_cost > _set_cost(best, by_id):
            stats["pruned_cost"] += 1
            return
        if cur_cost == _set_cost(best, by_id) and len(chosen) >= len(best):
            stats["pruned_cost"] += 1
            return
        remaining = universe[i:]
        # 可行性剪枝：chosen ∪ 剩余候选必须仍能覆盖全部情形
        if not covers(chosen, remaining):
            stats["pruned_feasibility"] += 1
            return

        cand = universe[i]
        # 先尝试包含，尽快拿到更紧上界
        chosen.add(cand["proof_id"])
        dfs(i + 1)
        chosen.discard(cand["proof_id"])
        dfs(i + 1)

    dfs(0)

    prepared = sorted(best)
    # ---- 各情形对固定清单的实际配方选择与复用说明 ----
    scenario_plans = []
    reuse: dict[str, list[str]] = {}
    total_cost = _set_cost(best, by_id)
    for name, failed in scenarios:
        pool = _pool([by_id[i] for i in prepared], failed)
        witness = selection.select_recipe(recipes, pool, mode="cheapest")
        # 搜索阶段已保证固定清单覆盖全部情形，这里属于不应到达的防御分支
        if witness is None:  # pragma: no cover - 由搜索不变量保证
            raise RuntimeError(f"情形 {name} 在固定清单上意外无解")
        scenario_plans.append(
            {
                "scenario": name,
                "failed_custody_domain": failed,
                "recipe_id": witness["recipe_id"],
                "recipe_version": witness["version"],
                "proof_ids": witness["proof_ids"],
                "cost": witness["cost"],
            }
        )
        for pid in witness["proof_ids"]:
            reuse.setdefault(pid, []).append(name)

    shared = {
        pid: sorted(names)
        for pid, names in sorted(reuse.items())
        if len(names) > 1
    }
    return {
        "result": "prepared_list",
        "account_id": spec["account_id"],
        "prepared_proof_ids": prepared,
        "prepared_count": len(prepared),
        "total_cost": total_cost,
        "scenario_plans": scenario_plans,
        "reuse": {
            "shared_proofs": shared,
            "note": "shared_proofs 中的证明被多个失效情形的配方共同需要，"
            "准备一次即可跨情形复用；各情形可选择不同配方但准备清单固定。",
        },
        "scenarios_evaluated": [name for name, _ in scenarios],
        "search_stats": {
            "candidate_count": len(candidates),
            "universe_count": n,
            "scenario_count": len(scenarios),
            **stats,
        },
    }


def _no_plan(
    scenario: str,
    failed: str | None,
    recipes: list[dict],
    pool: list[dict],
) -> dict:
    recipe_failures = []
    for recipe in recipes:
        deficits = selection.recipe_deficits(recipe, pool)
        if deficits:
            recipe_failures.append(
                {
                    "recipe_id": recipe["recipe_id"],
                    "recipe_version": recipe["version"],
                    "missing_requirements": deficits,
                }
            )
    return {
        "result": "no_recovery_plan",
        "first_failed_scenario": scenario,
        "failed_custody_domain": failed,
        "recipe_failures": recipe_failures,
    }
