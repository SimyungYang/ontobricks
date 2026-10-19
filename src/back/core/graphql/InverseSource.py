"""GraphQL schema record: :class:`InverseSource`."""

from typing import NamedTuple


class InverseSource(NamedTuple):
    """Inverse relationship field: predicate and the source GraphQL type."""

    predicate: str
    source_type: str
