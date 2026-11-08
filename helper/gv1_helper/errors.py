"""Service-specific exceptions."""


class HelperError(RuntimeError):
    """Base class for expected, user-facing helper failures."""


class JobCancelled(HelperError):
    """Raised when a job cancellation request is observed."""


class ProcessFailure(HelperError):
    """Raised when an external native process exits unsuccessfully."""

    def __init__(self, program: str, returncode: int, detail: str = "") -> None:
        message = f"{program} exited with status {returncode}"
        if detail:
            message += f": {detail}"
        super().__init__(message)
        self.program = program
        self.returncode = returncode
        self.detail = detail


class ValidationFailure(HelperError):
    """Raised when input or generated media violates a required invariant."""
