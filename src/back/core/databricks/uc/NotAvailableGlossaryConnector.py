"""Glossary connector used while no UC Pages API is available."""

from typing import Any, Dict, Iterable, Optional

from back.core.logging import get_logger

from .GlossaryConnector import GlossaryConnector

logger = get_logger(__name__)


class NotAvailableGlossaryConnector(GlossaryConnector):
    """Report the UC Pages API as unavailable so callers keep their last values.

    ``databricks-sdk`` (0.82.0, see ``uv.lock``) exposes no Pages / business
    glossary endpoint, and no verified REST shape exists yet. Swap in a real
    :class:`GlossaryConnector` once the API is published.
    """

    name = "not_available"

    def is_available(self) -> bool:
        logger.info("UC Pages API not available; glossary references keep their last value")
        return False

    def fetch_terms(self, refs: Iterable[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        return {}
