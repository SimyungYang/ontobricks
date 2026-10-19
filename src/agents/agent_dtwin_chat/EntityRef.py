"""Grouping key record used by the ``analyze_graphql`` aggregation."""

from typing import NamedTuple


class EntityRef(NamedTuple):
    """A nested entity (or scalar) reduced to its grouping key, label and URI."""

    key: str
    display: str
    uri: str
