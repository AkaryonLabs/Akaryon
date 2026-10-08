class AkaryonError(Exception):
    """Base class for expected runtime errors."""


class ProviderError(AkaryonError):
    pass


class ToolsDisabledForTurnError(ProviderError):
    """A provider attempted an action after the operator disabled tools."""


class PermissionDenied(AkaryonError):
    pass


class ApprovalRequired(AkaryonError):
    pass


class ToolError(AkaryonError):
    pass


class TaskCancelled(AkaryonError):
    """Raised when cooperative cancellation is observed between task steps."""
