"""配方可行性与最小证明集选择。

两条决胜模式：

* ``mode="reserve"``：先比**证明条数最少**，再比升序 ID 列表（恢复申请的规则）。
* ``mode="cheapest"``：先比**费用总和最低**，再比条数，再比升序 ID 列表
  （simulate_loss 单情形证人选择，配合全局准备清单的决胜）。

独立性语义（配方要求级）：

* ``distinct_custody_domains``：所选证明必须分属不同保管域；
* ``distinct_organizations``：所选证明必须分属不同组织。

两条都置位时，“同组织但跨保管域”的证明因组织维度不独立而被拒绝——
这正是“同组织跨保管域但不独立”的夹具场景。
"""

from __future__ import annotations


def _smallest_combination(cands: list[dict], req: dict) -> list[dict] | None:
    """在同类候选中找满足独立性的、字典序最小的 count 元组；不可行返回 None。"""
    need = req["count"]
    distinct_org = req.get("distinct_organizations", False)
    distinct_dom = req.get("distinct_custody_domains", False)
    if len(cands) < need:
        return None

    picked: list[dict] = []
    used_orgs: set[str] = set()
    used_domains: set[str] = set()

    def dfs(start: int) -> bool:
        if len(picked) == need:
            return True
        # 剩余候选不足以凑满数量，剪枝
        if len(cands) - start < need - len(picked):
            return False
        for i in range(start, len(cands)):
            cand = cands[i]
            if distinct_org and cand["organization"] in used_orgs:
                continue
            if distinct_dom and cand["custody_domain"] in used_domains:
                continue
            used_orgs.add(cand["organization"])
            used_domains.add(cand["custody_domain"])
            picked.append(cand)
            if dfs(i + 1):
                return True
            picked.pop()
            used_domains.discard(cand["custody_domain"])
            used_orgs.discard(cand["organization"])
        return False

    return list(picked) if dfs(0) else None


def _cheapest_combination(cands: list[dict], req: dict) -> list[dict] | None:
    """在同类候选中找费用最低的 count 元组，平手取字典序最小。"""
    need = req["count"]
    distinct_org = req.get("distinct_organizations", False)
    distinct_dom = req.get("distinct_custody_domains", False)
    if len(cands) < need:
        return None

    best: list[dict] | None = None
    picked: list[dict] = []
    used_orgs: set[str] = set()
    used_domains: set[str] = set()

    def compare(chosen: list[dict]) -> bool:
        nonlocal best
        if best is None:
            return True
        c_cost = sum(c["cost"] for c in chosen)
        b_cost = sum(c["cost"] for c in best)
        if c_cost != b_cost:
            return c_cost < b_cost
        c_ids = tuple(c["proof_id"] for c in chosen)
        b_ids = tuple(c["proof_id"] for c in best)
        return c_ids < b_ids

    def dfs(start: int) -> None:
        nonlocal best
        if len(picked) == need:
            if compare(picked):
                best = list(picked)
            return
        if len(cands) - start < need - len(picked):
            return
        for i in range(start, len(cands)):
            cand = cands[i]
            if distinct_org and cand["organization"] in used_orgs:
                continue
            if distinct_dom and cand["custody_domain"] in used_domains:
                continue
            used_orgs.add(cand["organization"])
            used_domains.add(cand["custody_domain"])
            picked.append(cand)
            dfs(i + 1)
            picked.pop()
            used_domains.discard(cand["custody_domain"])
            used_orgs.discard(cand["organization"])

    dfs(0)
    return best


def _group(candidates: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for c in candidates:
        grouped.setdefault(c["proof_type"], []).append(c)
    for lst in grouped.values():
        lst.sort(key=lambda c: c["proof_id"])
    return grouped


def recipe_witness(
    recipe: dict, candidates: list[dict], *, mode: str = "reserve"
) -> dict | None:
    """返回该配方在给定候选集上的最优证人，或 None。

    返回结构::

        {"recipe_id", "version", "proof_ids": [...], "cost": int, "count": int}
    """
    grouped = _group(candidates)
    chosen_all: list[dict] = []
    for req in recipe["requirements"]:
        pool = grouped.get(req["proof_type"], [])
        if mode == "cheapest":
            comb = _cheapest_combination(pool, req)
        else:
            comb = _smallest_combination(pool, req)
        if comb is None:
            return None
        chosen_all.extend(comb)
    chosen_all.sort(key=lambda c: c["proof_id"])
    return {
        "recipe_id": recipe["recipe_id"],
        "version": recipe["version"],
        "proof_ids": [c["proof_id"] for c in chosen_all],
        "cost": sum(c["cost"] for c in chosen_all),
        "count": len(chosen_all),
    }


def _better(witness: dict, best: dict | None, *, mode: str) -> bool:
    if best is None:
        return True
    if mode == "cheapest":
        key_w = (witness["cost"], witness["count"], tuple(witness["proof_ids"]))
        key_b = (best["cost"], best["count"], tuple(best["proof_ids"]))
    else:  # reserve：条数优先，再升序 ID
        key_w = (witness["count"], tuple(witness["proof_ids"]))
        key_b = (best["count"], tuple(best["proof_ids"]))
    return key_w < key_b


def select_recipe(
    recipes: list[dict], candidates: list[dict], *, mode: str = "reserve"
) -> dict | None:
    """在所有预先批准的配方中挑选一份可行且最优的。"""
    best: dict | None = None
    for recipe in recipes:
        witness = recipe_witness(recipe, candidates, mode=mode)
        if witness is not None and _better(witness, best, mode=mode):
            best = witness
    return best


# --------------------------------------------------------------------------- 不可行诊断
def recipe_deficits(recipe: dict, candidates: list[dict]) -> list[dict]:
    """逐条列出配方无法满足的要求（用于 simulate_loss 的缺失要求输出）。"""
    grouped = _group(candidates)
    deficits: list[dict] = []
    for req in recipe["requirements"]:
        pool = grouped.get(req["proof_type"], [])
        comb = _smallest_combination(pool, req)
        if comb is not None:
            continue
        entry: dict = {
            "proof_type": req["proof_type"],
            "required_count": req["count"],
            "available_count": len(pool),
            "distinct_organizations": req.get("distinct_organizations", False),
            "distinct_custody_domains": req.get("distinct_custody_domains", False),
            "blocked_reason": "",
        }
        if len(pool) < req["count"]:
            entry["blocked_reason"] = "insufficient_count"
        elif req.get("distinct_organizations", False) and len({c["organization"] for c in pool}) < req["count"]:
            entry["blocked_reason"] = "distinct_organizations_unsatisfied"
        elif req.get("distinct_custody_domains", False) and len({c["custody_domain"] for c in pool}) < req["count"]:
            entry["blocked_reason"] = "distinct_custody_domains_unsatisfied"
        else:
            entry["blocked_reason"] = "combined_independence_unsatisfied"
        deficits.append(entry)
    return deficits
