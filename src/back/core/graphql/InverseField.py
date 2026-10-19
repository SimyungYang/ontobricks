"""GraphQL schema record: :class:`InverseField`."""

from typing import NamedTuple


class InverseField(NamedTuple):
    """One generated inverse field on a range class (schema build output)."""

    field_name: str
    prop_uri: str
    domain_name: str
