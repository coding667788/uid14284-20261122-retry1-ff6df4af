"""输入结构校验。

只检查“结构与基本取值”，所有跨实体/跨事件的逻辑规则在 :mod:`solution.engine`
里判定。每个错误都带 JSONPath 风格字段位置，例如
``events[3].payload.freeze_until``。
"""

from __future__ import annotations

from .errors import ValidationError
from .jsonio import dumps

# 有状态、需要幂等登记的命令
STATEFUL_CMDS = frozenset(
    {
        "register_account",
        "register_trustee",
        "update_trustee",
        "issue_proof",
        "revoke_proof",
        "register_recipe",
        "reserve",
        "challenge",
        "cancel",
        "activate",
    }
)
READONLY_CMDS = frozenset({"get_receipt"})
COMPUTE_CMDS = frozenset({"simulate_loss"})
ALL_CMDS = STATEFUL_CMDS | READONLY_CMDS | COMPUTE_CMDS

CHALLENGERS = frozenset({"old_authenticator", "security_contact"})


# --------------------------------------------------------------------------- 基础取字段
def _kind(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def require_obj(value, path: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"字段 {path} 必须是对象，实际为 {_kind(value)}", path)
    return value


def require_list(value, path: str) -> list:
    if not isinstance(value, list):
        raise ValidationError(f"字段 {path} 必须是数组，实际为 {_kind(value)}", path)
    return value


def require_str(value, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"字段 {path} 必须是字符串，实际为 {_kind(value)}", path)
    if nonempty and value == "":
        raise ValidationError(f"字段 {path} 不能为空字符串", path)
    return value


def require_int(value, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"字段 {path} 必须是整数，实际为 {_kind(value)}", path)
    if minimum is not None and value < minimum:
        raise ValidationError(f"字段 {path} 必须 >= {minimum}，实际为 {value}", path)
    return value


def require_bool(value, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"字段 {path} 必须是布尔值，实际为 {_kind(value)}", path)
    return value


def field(obj: dict, key: str, path: str, required: bool = True, default=None):
    if key not in obj or obj[key] is None:
        if required:
            raise ValidationError(f"缺少必填字段 {path}.{key}", f"{path}.{key}")
        return default
    return obj[key]


def optional_int(obj: dict, key: str, path: str, minimum: int | None = None):
    if key not in obj or obj[key] is None:
        return None
    return require_int(obj[key], f"{path}.{key}", minimum=minimum)


# --------------------------------------------------------------------------- 配方
def validate_requirement(req, path: str) -> dict:
    require_obj(req, path)
    distinct_org = req.get("distinct_organizations", False)
    distinct_dom = req.get("distinct_custody_domains", False)
    return {
        "proof_type": require_str(field(req, "proof_type", path), f"{path}.proof_type"),
        "count": require_int(field(req, "count", path), f"{path}.count", minimum=1),
        "distinct_organizations": require_bool(
            False if distinct_org is None else distinct_org,
            f"{path}.distinct_organizations",
        ),
        "distinct_custody_domains": require_bool(
            False if distinct_dom is None else distinct_dom,
            f"{path}.distinct_custody_domains",
        ),
    }


def validate_requirements(value, path: str) -> list[dict]:
    reqs = require_list(value, path)
    if not reqs:
        raise ValidationError(f"字段 {path} 至少包含一项要求", path)
    out: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(reqs):
        r = validate_requirement(raw, f"{path}[{i}]")
        if r["proof_type"] in seen:
            raise ValidationError(
                f"配方中证明类型 {r['proof_type']} 重复，应合并为单条 count 要求",
                f"{path}[{i}].proof_type",
            )
        seen.add(r["proof_type"])
        out.append(r)
    return out


# --------------------------------------------------------------------------- 各命令 payload
def _v_register_account(p: dict, path: str) -> dict:
    return {
        "account_id": require_str(field(p, "account_id", path), f"{path}.account_id"),
        "initial_authenticator": require_str(
            field(p, "initial_authenticator", path), f"{path}.initial_authenticator"
        ),
        "security_contact": require_str(
            field(p, "security_contact", path), f"{path}.security_contact"
        ),
    }


def _v_trustee_common(p: dict, path: str) -> dict:
    vf = require_int(field(p, "valid_from", path), f"{path}.valid_from", minimum=0)
    vu = require_int(field(p, "valid_until", path), f"{path}.valid_until", minimum=0)
    if vu <= vf:
        raise ValidationError(
            f"字段 {path}.valid_until 必须严格大于 valid_from", f"{path}.valid_until"
        )
    return {
        "trustee_id": require_str(field(p, "trustee_id", path), f"{path}.trustee_id"),
        "organization": require_str(field(p, "organization", path), f"{path}.organization"),
        "custody_domain": require_str(
            field(p, "custody_domain", path), f"{path}.custody_domain"
        ),
        "valid_from": vf,
        "valid_until": vu,
    }


def _v_issue_proof(p: dict, path: str) -> dict:
    issued = require_int(field(p, "issued_at", path), f"{path}.issued_at", minimum=0)
    until = require_int(field(p, "valid_until", path), f"{path}.valid_until", minimum=0)
    if until <= issued:
        raise ValidationError(
            f"字段 {path}.valid_until 必须严格大于 issued_at", f"{path}.valid_until"
        )
    return {
        "proof_id": require_str(field(p, "proof_id", path), f"{path}.proof_id"),
        "account_id": require_str(field(p, "account_id", path), f"{path}.account_id"),
        "trustee_id": require_str(field(p, "trustee_id", path), f"{path}.trustee_id"),
        "proof_type": require_str(field(p, "proof_type", path), f"{path}.proof_type"),
        "digest": require_str(field(p, "digest", path), f"{path}.digest"),
        "issued_at": issued,
        "valid_until": until,
        "cost": require_int(field(p, "cost", path), f"{path}.cost", minimum=1),
    }


def _v_register_recipe(p: dict, path: str) -> dict:
    return {
        "recipe_id": require_str(field(p, "recipe_id", path), f"{path}.recipe_id"),
        "version": require_int(field(p, "version", path), f"{path}.version", minimum=1),
        "requirements": validate_requirements(
            field(p, "requirements", path), f"{path}.requirements"
        ),
        "active_from": require_int(
            field(p, "active_from", path), f"{path}.active_from", minimum=0
        ),
    }


def _v_reserve(p: dict, path: str) -> dict:
    out = {
        "account_id": require_str(field(p, "account_id", path), f"{path}.account_id"),
        "generation": require_int(
            field(p, "generation", path), f"{path}.generation", minimum=1
        ),
        "new_authenticator_digest": require_str(
            field(p, "new_authenticator_digest", path),
            f"{path}.new_authenticator_digest",
        ),
        "freeze_until": require_int(
            field(p, "freeze_until", path), f"{path}.freeze_until", minimum=0
        ),
        "recipe_id": None,
        "recipe_version": None,
    }
    if "recipe_id" in p and p["recipe_id"] is not None:
        out["recipe_id"] = require_str(p["recipe_id"], f"{path}.recipe_id")
        out["recipe_version"] = require_int(
            field(p, "recipe_version", path), f"{path}.recipe_version", minimum=1
        )
    return out


def _v_challenge(p: dict, path: str) -> dict:
    who = require_str(field(p, "challenger", path), f"{path}.challenger")
    if who not in CHALLENGERS:
        raise ValidationError(
            f"字段 {path}.challenger 只能是 old_authenticator 或 security_contact，实际为 {who!r}",
            f"{path}.challenger",
        )
    return {
        "challenger": who,
        "app_request_id": require_str(
            field(p, "app_request_id", path), f"{path}.app_request_id"
        ),
    }


def _v_cancel(p: dict, path: str) -> dict:
    out: dict = {
        "app_request_id": require_str(
            field(p, "app_request_id", path), f"{path}.app_request_id"
        )
    }
    if "reason" in p and p["reason"] is not None:
        out["reason"] = require_str(p["reason"], f"{path}.reason", nonempty=False)
    else:
        out["reason"] = ""
    return out


_PAYLOAD_VALIDATORS = {
    "register_account": _v_register_account,
    "register_trustee": _v_trustee_common,
    "update_trustee": _v_trustee_common,
    "issue_proof": _v_issue_proof,
    "revoke_proof": lambda p, path: {
        "proof_id": require_str(field(p, "proof_id", path), f"{path}.proof_id"),
        "revoked_at": require_int(
            field(p, "revoked_at", path), f"{path}.revoked_at", minimum=0
        ),
    },
    "register_recipe": _v_register_recipe,
    "reserve": _v_reserve,
    "challenge": _v_challenge,
    "cancel": _v_cancel,
    "activate": lambda p, path: {
        "app_request_id": require_str(
            field(p, "app_request_id", path), f"{path}.app_request_id"
        )
    },
    "get_receipt": lambda p, path: {
        "request_id": require_str(field(p, "request_id", path), f"{path}.request_id")
    },
    "simulate_loss": lambda p, path: {},
}


# --------------------------------------------------------------------------- 事件与用例
def validate_event(raw, index: int | None = None) -> dict:
    """校验单个事件，返回规范化事件。"""
    base = "$" if index is None else f"events[{index}]"
    require_obj(raw, base)
    cmd = require_str(field(raw, "cmd", base), f"{base}.cmd")
    if cmd not in ALL_CMDS:
        raise ValidationError(
            f"未知命令 {cmd!r}，允许：{sorted(ALL_CMDS)}", f"{base}.cmd"
        )
    at = require_int(field(raw, "at", base), f"{base}.at", minimum=0)
    seq = require_int(field(raw, "seq", base), f"{base}.seq", minimum=1)
    request_id = None
    if "request_id" in raw and raw["request_id"] is not None:
        request_id = require_str(raw["request_id"], f"{base}.request_id")
    if cmd in STATEFUL_CMDS and request_id is None:
        raise ValidationError(
            f"状态命令 {cmd} 必须携带 request_id 以支持幂等", f"{base}.request_id"
        )
    payload = require_obj(field(raw, "payload", base, default={}), f"{base}.payload")
    clean_payload = _PAYLOAD_VALIDATORS[cmd](payload, f"{base}.payload")
    return {
        "cmd": cmd,
        "at": at,
        "seq": seq,
        "request_id": request_id,
        "payload": clean_payload,
    }


def validate_case(raw) -> list[dict]:
    """校验 ingest 用例（``{"events": [...]}`` 或单个事件对象）。"""
    require_obj(raw, "$")
    if "events" in raw:
        events = require_list(raw["events"], "$.events")
    elif "cmd" in raw:
        events = [raw]
    else:
        raise ValidationError("用例根对象必须包含 events 数组，或本身是单个事件对象", "$")
    return [validate_event(ev, i) for i, ev in enumerate(events)]


def canonical_content(event: dict) -> str:
    """幂等内容指纹：去掉排序游标 seq，其余字段全部参与。"""
    slim = {k: v for k, v in event.items() if k != "seq"}
    return dumps(slim)


# --------------------------------------------------------------------------- simulate_loss
def validate_candidate(raw, index: int) -> dict:
    path = f"candidates[{index}]"
    require_obj(raw, path)
    vf = optional_int(raw, "valid_from", path, minimum=0)
    vu = optional_int(raw, "valid_until", path, minimum=0)
    if vf is not None and vu is not None and vu <= vf:
        raise ValidationError(
            f"字段 {path}.valid_until 必须严格大于 valid_from", f"{path}.valid_until"
        )
    return {
        "proof_id": require_str(field(raw, "proof_id", path), f"{path}.proof_id"),
        "organization": require_str(
            field(raw, "organization", path), f"{path}.organization"
        ),
        "custody_domain": require_str(
            field(raw, "custody_domain", path), f"{path}.custody_domain"
        ),
        "proof_type": require_str(
            field(raw, "proof_type", path), f"{path}.proof_type"
        ),
        "cost": require_int(field(raw, "cost", path), f"{path}.cost", minimum=1),
        "valid_from": vf if vf is not None else 0,
        "valid_until": vu if vu is not None else 2**62,
    }


def validate_simulate(raw) -> dict:
    require_obj(raw, "$")
    account_id = require_str(field(raw, "account_id", "$"), "$.account_id")
    candidates_raw = require_list(field(raw, "candidates", "$"), "$.candidates")
    if not candidates_raw:
        raise ValidationError("字段 $.candidates 至少包含一条候选证明", "$.candidates")
    candidates = [validate_candidate(c, i) for i, c in enumerate(candidates_raw)]
    ids = [c["proof_id"] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValidationError("candidates 中 proof_id 必须唯一", "$.candidates")
    domains = sorted({c["custody_domain"] for c in candidates})
    if len(domains) > 8:
        raise ValidationError(
            f"保管域数量最多 8 个，实际为 {len(domains)}", "$.candidates"
        )
    if "custody_domains" in raw and raw["custody_domains"] is not None:
        extra = require_list(raw["custody_domains"], "$.custody_domains")
        domains = sorted({require_str(d, f"$.custody_domains[{i}]") for i, d in enumerate(extra)})
    recipes_raw = require_list(field(raw, "recipes", "$"), "$.recipes")
    if not recipes_raw:
        raise ValidationError("字段 $.recipes 至少包含一份配方", "$.recipes")
    recipes: list[dict] = []
    seen_recipe: set[tuple[str, int]] = set()
    for i, rr in enumerate(recipes_raw):
        require_obj(rr, f"$.recipes[{i}]")
        rid = require_str(field(rr, "recipe_id", f"$.recipes[{i}]"), f"$.recipes[{i}].recipe_id")
        version = require_int(
            field(rr, "version", f"$.recipes[{i}]"), f"$.recipes[{i}].version", minimum=1
        )
        if (rid, version) in seen_recipe:
            raise ValidationError(
                f"配方 {rid} 版本 {version} 重复", f"$.recipes[{i}]"
            )
        seen_recipe.add((rid, version))
        reqs = validate_requirements(
            field(rr, "requirements", f"$.recipes[{i}]"),
            f"$.recipes[{i}].requirements",
        )
        recipes.append({"recipe_id": rid, "version": version, "requirements": reqs})
    drill_time = require_int(
        raw.get("drill_time", 0) if raw.get("drill_time") is not None else 0,
        "$.drill_time",
        minimum=0,
    )
    return {
        "account_id": account_id,
        "candidates": candidates,
        "domains": domains,
        "recipes": recipes,
        "drill_time": drill_time,
    }
