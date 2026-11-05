"""Read-only interface to a Unity Catalog business glossary (UC Pages)."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Optional


class GlossaryConnector(ABC):
    """Look up glossary terms (definition + synonyms) by their reference.

    Implementations never write to Unity Catalog. ``fetch_terms`` maps every
    requested reference to ``{"definition": str, "synonyms": [str], "url": str}``
    or to ``None`` when the term no longer exists. It is only called when
    :meth:`is_available` is ``True``.
    """

    name = "abstract"

    @abstractmethod
    def is_available(self) -> bool:
        """``True`` when the glossary API can be queried on this workspace."""

    @abstractmethod
    def fetch_terms(self, refs: Iterable[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """Return the current term content for every reference in *refs*."""
