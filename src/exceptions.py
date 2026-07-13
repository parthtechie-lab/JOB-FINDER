"""
Career Raider - Custom Exception Hierarchy
Provides structured metadata and contextual traces for robust self-healing.
"""
from typing import Any, Dict, Optional


class CareerRaiderError(Exception):
    """
    Base exception for all domain-specific errors.
    Automatically captures structured metadata for incident logging.
    """
    def __init__(self, message: str, extra_context: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.extra_context = extra_context or {}

    def __str__(self):
        if self.extra_context:
            return f"{self.message} (Context: {self.extra_context})"
        return self.message


class DatabaseError(CareerRaiderError):
    """Raised when SQL/ORM operations fail (e.g. connection drops, constraints)."""
    pass


class IngestionError(CareerRaiderError):
    """Raised during the scraping pipeline (e.g. rate limits, bad DOM, network issues)."""
    pass


class AIProcessingError(CareerRaiderError):
    """Raised when LLM calls fail or when structured output doesn't match the schema."""
    pass


class ConfigurationError(CareerRaiderError):
    """Raised when critical configuration or environment variables are missing."""
    pass
