"""Bounded, machine-readable refusals shared by the loopback API managers."""

from __future__ import annotations


class ApiRefusal(Exception):
    """A request that is rejected before it can produce the requested effect."""

    def __init__(
        self,
        code: str,
        status_code: int = 400,
        *,
        message: str | None = None,
        **detail: object,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.detail = dict(detail)

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {"error": self.code, **self.detail}
        if self.message is not None:
            body["message"] = self.message
        return body
