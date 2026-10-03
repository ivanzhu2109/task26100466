"""Exception types for the fxsettle library."""


class FxError(Exception):
    """Base class for all fxsettle errors."""


class ConflictError(FxError):
    """Same idempotency key was reused with a different request."""


class RejectedError(FxError):
    """The exchange was rejected by business rules (persisted as the result).

    Carries the stable error code stored alongside the receipt.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)
