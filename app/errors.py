"""Exceções de domínio: carregam um Diagnostic e o status HTTP correspondente."""
from __future__ import annotations

from .diagnostics.codes import DiagCode, Diagnostic, make


class OmniLinkError(Exception):
    status = 400

    def __init__(self, code: str | DiagCode, message: str | None = None, *, status: int | None = None,
                 suggestion: str | None = None, retry_after: float | None = None, **detail):
        self.diagnostic: Diagnostic = make(code, message=message, suggestion=suggestion, **detail)
        if status is not None:
            self.status = status
        self.retry_after = retry_after
        super().__init__(f"{self.diagnostic.code}: {self.diagnostic.message}")

    @property
    def code(self) -> str:
        return self.diagnostic.code


class ValidationFailure(OmniLinkError):
    status = 400


class AuthError(OmniLinkError):
    status = 401


class Forbidden(OmniLinkError):
    status = 403


class NotFound(OmniLinkError):
    status = 404


class Unprocessable(OmniLinkError):
    status = 422


class TooLarge(OmniLinkError):
    status = 413


class RateLimited(OmniLinkError):
    status = 429
