"""Stable error codes shared by the controller, CLI and end-to-end verifier."""


class CloudRuntimeError(Exception):
    """Base failure with a machine-readable reason and an actionable hint."""

    reason = "runtime_error"
    exit_code = 1

    def __init__(self, message, *, actionable=None):
        super().__init__(message)
        self.actionable = actionable

    def to_dict(self):
        data = {"reason": self.reason, "message": str(self)}
        if self.actionable:
            data["actionable"] = self.actionable
        return data


class ConfigurationError(CloudRuntimeError):
    reason = "configuration_error"


class AuthorizationError(CloudRuntimeError):
    """Cloud account rejected the credentials or lacks the required permission."""

    reason = "authorization_failed"


class ProvisionTimeout(CloudRuntimeError):
    reason = "provision_timeout"


class BootstrapTimeout(CloudRuntimeError):
    reason = "bootstrap_timeout"


class RuntimeUnavailable(CloudRuntimeError):
    """Raised when a cloud runtime is not ready; local silent fallback is forbidden."""

    reason = "runtime_unavailable"


class OrphanResourceError(CloudRuntimeError):
    reason = "orphan_resource"
