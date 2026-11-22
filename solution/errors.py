"""错误类型。

所有可预期的失败都抛出 :class:`AppError`，携带机器可读错误码与字段位置
（字段位置使用 JSONPath 风格，例如 ``events[3].payload.account_id``）。
"""

from __future__ import annotations


class AppError(Exception):
    """业务/结构错误的统一载体。"""

    def __init__(self, code: str, message: str, field: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = details or {}

    def to_dict(self) -> dict:
        out: dict = {"error": {"code": self.code, "message": self.message}}
        if self.field is not None:
            out["error"]["field"] = self.field
        if self.details:
            out["error"]["details"] = self.details
        return out


# 结构错误（输入不符合接口约定），CLI 退出码 2
class ValidationError(AppError):
    def __init__(self, message: str, field: str | None = None, details: dict | None = None):
        super().__init__("E_VALIDATION", message, field, details)


# 业务状态错误（结构合法但当前逻辑状态不允许），CLI 退出码 1
class StateError(AppError):
    def __init__(self, message: str, field: str | None = None, details: dict | None = None):
        super().__init__("E_STATE", message, field, details)


# 幂等冲突：相同 request_id 但内容不同
class ConflictError(AppError):
    def __init__(self, message: str, field: str | None = None, details: dict | None = None):
        super().__init__("E_IDEMPOTENCY_CONFLICT", message, field, details)


# simulate_loss 无可行准备清单（这是正常的计算结果，不是进程失败）
class NoRecoveryPlanError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__("E_NO_RECOVERY_PLAN", message, None, details)
