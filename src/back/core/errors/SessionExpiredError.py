"""The caller's forwarded credentials expired during an operation (401)."""

from __future__ import annotations

from back.core.errors.AuthorizationError import AuthorizationError


class SessionExpiredError(AuthorizationError):
    """The caller's forwarded credentials expired during an operation (401).

    Forwarded Databricks Apps user tokens are short-lived. Long-running work
    that captured one (e.g. a background Knowledge Store import) raises this
    so it can stop cleanly instead of falling back to the app identity.
    """

    def __init__(self, message: str = "Your sign-in has expired", **kw):
        super().__init__(message, **kw)
        self.status_code = 401
