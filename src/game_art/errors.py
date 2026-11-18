"""错误类型与退出码。"""
from __future__ import annotations

from typing import Any


class GAError(Exception):
    exit_code: int = 1

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class InputError(GAError):
    exit_code = 3


class UpstreamError(GAError):
    exit_code = 4


class TimeoutError_(GAError):
    exit_code = 5


class ExecutionError(GAError):
    exit_code = 6


class BudgetExceeded(GAError):
    exit_code = 9
