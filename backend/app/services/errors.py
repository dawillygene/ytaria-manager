from __future__ import annotations


class ServiceError(Exception):
    """Domain error carrying an HTTP status and a stable machine-readable code."""

    status_code = 400

    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if status_code is not None:
            self.status_code = status_code


class NotFound(ServiceError):
    status_code = 404

    def __init__(self, message: str = "Not found.") -> None:
        super().__init__("not_found", message)


class Conflict(ServiceError):
    status_code = 409
