"""GraphQL schema record: :class:`RelEdge`."""

from typing import NamedTuple


class RelEdge(NamedTuple):
    """One object property seen from its domain class (schema build input)."""

    prop_name: str
    range_name: str
    prop_uri: str
