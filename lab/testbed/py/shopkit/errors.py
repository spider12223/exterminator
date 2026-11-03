class UserNotFound(LookupError):
    """Raised when a user id does not exist."""


class SyncError(RuntimeError):
    """Raised when pushing data to an external system fails."""
