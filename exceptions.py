"""Shared exception taxonomy for the Family Agent Platform.

All custom errors inherit from AppError so that global handlers can route
alerts, retries, and fallbacks consistently.
"""


class AppError(Exception):
    """Base class for all application-specific errors."""

    def __init__(self, message: str, *, component: str = "unknown", context: dict | None = None):
        super().__init__(message)
        self.message = message
        self.component = component
        self.context = context or {}


class ConfigError(AppError):
    """Missing or invalid configuration / environment variables."""


class DataError(AppError):
    """Database, file, or external data source failure."""


class ModelError(AppError):
    """AI/ML model load, inference, or validation failure."""


class NetworkError(AppError):
    """Outbound network or API timeout failure."""


class AgentError(AppError):
    """Remote agent connector or cross-agent protocol failure."""


class ValidationError(AppError):
    """User input or request validation failure."""


class SecurityError(AppError):
    """Authentication, authorization, or signature failure."""
