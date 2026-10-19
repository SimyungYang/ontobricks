"""GraphQL schema record: :class:`RelTarget`."""

from typing import NamedTuple


class RelTarget(NamedTuple):
    """Forward relationship field: predicate and the target GraphQL type."""

    predicate: str
    target_type: str
