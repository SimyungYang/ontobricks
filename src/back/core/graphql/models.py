"""GraphQL data transfer objects.

The small ``NamedTuple`` records below replace anonymous tuples; being
tuples, they still unpack positionally and compare equal to plain tuples.
"""

from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple


class RelEdge(NamedTuple):
    """One object property seen from its domain class (schema build input)."""

    prop_name: str
    range_name: str
    prop_uri: str


class InverseField(NamedTuple):
    """One generated inverse field on a range class (schema build output)."""

    field_name: str
    prop_uri: str
    domain_name: str


class RelTarget(NamedTuple):
    """Forward relationship field: predicate and the target GraphQL type."""

    predicate: str
    target_type: str


class InverseSource(NamedTuple):
    """Inverse relationship field: predicate and the source GraphQL type."""

    predicate: str
    source_type: str


@dataclass
class TypeInfo:
    """Metadata for a single GraphQL entity type.

    ``relationships`` maps a forward field to a :class:`RelTarget`;
    ``inverse_relationships`` maps an inverse field on this type to an
    :class:`InverseSource` -- the source entities point at this one.
    ``graphql_names`` maps Python field names to their GraphQL (camelCase)
    names so query selections can be matched to fields.
    """

    name: str
    cls_uri: str
    gql_type: type
    predicate_to_field: Dict[str, str] = field(default_factory=dict)
    field_to_predicate: Dict[str, str] = field(default_factory=dict)
    relationships: Dict[str, RelTarget] = field(default_factory=dict)
    inverse_relationships: Dict[str, InverseSource] = field(default_factory=dict)
    graphql_names: Dict[str, str] = field(default_factory=dict)
    label: str = ""
    relationship_labels: Dict[str, str] = field(default_factory=dict)
    list_field: str = ""
    count_field: str = ""
    single_field: str = ""

    def gql_name(self, python_name: str) -> str:
        """GraphQL name for a Python field name (identity when unknown)."""
        return self.graphql_names.get(python_name, python_name)

    def data_fields(self) -> List[str]:
        """Python names of the data-property fields, in declaration order."""
        return [
            f for f in self.field_to_predicate if f not in self.relationships
        ]

    def python_field(self, name: str) -> str:
        """Resolve a GraphQL or Python field *name* to the Python field name."""
        if name in self.field_to_predicate or name in self.inverse_relationships:
            return name
        for py_name, gql in self.graphql_names.items():
            if gql == name:
                return py_name
        return ""
