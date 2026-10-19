"""Error raised for invalid ``analyze_graphql`` arguments."""

from back.core.errors import ValidationError


class AnalysisError(ValidationError):
    """Invalid analyze_graphql arguments; the message is shown to the LLM."""
