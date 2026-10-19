"""Generate a Strawberry GraphQL schema from OntoBricks ontology definitions.

Each ontology class becomes a GraphQL object type with:
- ``id: ID!`` — local name extracted from the subject URI
- ``uri: String!`` — full subject URI
- ``label: String`` — rdfs:label value
- One ``Optional[String]`` field per data property
- One ``Optional[List[TargetType]]`` field per object property
- One inverse ``Optional[List[SourceType]]`` field per object property on
  its range type, named ``<pluralOfDomain>Via<Property>`` (for example
  ``salesTransactionsViaSoldTo`` on ``Customer``)

Root query fields per type: ``<plural>(limit, offset, search, filters)``,
``<plural>Count(search, filters, distinctBy)`` and ``<singular>(id)``.

Types are created in topological order so that relationship targets
are always defined before the types that reference them.
"""

import hashlib
import json
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import strawberry
from strawberry.tools import create_type
from strawberry.utils.str_converters import to_camel_case

from back.core.graphql.ResolverFactory import ResolverFactory
from back.core.graphql.constants import MAX_LIST_LIMIT
from back.core.graphql.SchemaMetadata import SchemaMetadata
from back.core.graphql.InverseField import InverseField
from back.core.graphql.InverseSource import InverseSource
from back.core.graphql.models import TypeInfo
from back.core.graphql.RelEdge import RelEdge
from back.core.graphql.RelTarget import RelTarget
from back.core.logging import get_logger

logger = get_logger(__name__)


class GraphQLSchemaBuilder:
    """Build and cache Strawberry GraphQL schemas from ontology metadata.

    A single instance manages a per-domain cache so that repeated
    calls with the same ontology fingerprint return the same schema.

    Usage::

        builder = GraphQLSchemaBuilder()
        result  = builder.build_for_domain(classes, props, base_uri, "my_domain")
        if result:
            schema, metadata = result
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Tuple[Any, str, SchemaMetadata]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_for_domain(
        self,
        ontology_classes: List[Dict],
        ontology_properties: List[Dict],
        base_uri: str,
        domain_name: str = "",
    ) -> Optional[Tuple[strawberry.Schema, SchemaMetadata]]:
        """Build (or return cached) a Strawberry Schema from ontology metadata.

        Returns ``(schema, metadata)`` or ``None`` if the ontology is empty.
        """
        h = self._ontology_hash(ontology_classes, ontology_properties)

        if domain_name and domain_name in self._cache:
            cached_schema, cached_hash, cached_meta = self._cache[domain_name]
            if cached_hash == h:
                logger.debug("GraphQL schema cache hit for domain '%s'", domain_name)
                return cached_schema, cached_meta

        result = self._build(ontology_classes, ontology_properties, base_uri)
        if result and domain_name:
            schema, meta = result
            self._cache[domain_name] = (schema, h, meta)
        return result

    def invalidate_cache(self, domain_name: str) -> None:
        """Remove a domain's cached schema."""
        self._cache.pop(domain_name, None)

    # ------------------------------------------------------------------
    # Text / URI helpers (static)
    # ------------------------------------------------------------------

    @staticmethod
    def safe_name(raw: str) -> str:
        """Sanitise *raw* into a valid Python / GraphQL identifier."""
        from back.core.helpers import safe_identifier

        return safe_identifier(raw, prefix="_")

    @staticmethod
    def extract_local(uri: str) -> str:
        """Return the fragment / last path segment of *uri*."""
        from back.core.helpers import extract_local_name

        return extract_local_name(uri)

    @staticmethod
    def pluralize(name: str) -> str:
        """Naive English pluralisation for query-field names."""
        low = name[0].lower() + name[1:] if name else name
        if low.endswith(("s", "x", "z", "sh", "ch")):
            return low + "es"
        if low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
            return low[:-1] + "ies"
        return low + "s"

    @staticmethod
    def normalize_base(base_uri: str) -> str:
        """Normalise *base_uri* to always end with ``/``."""
        return base_uri.rstrip("/").rstrip("#") + "/"

    @staticmethod
    def ontology_hash(classes: List[Dict], properties: List[Dict]) -> str:
        """MD5 fingerprint of the ontology definition lists."""
        raw = json.dumps(classes, sort_keys=True) + json.dumps(
            properties, sort_keys=True
        )
        return hashlib.md5(raw.encode()).hexdigest()

    @staticmethod
    def topo_sort(names: Set[str], deps: Dict[str, Set[str]]) -> List[str]:
        """Topological sort (leaves first). Breaks cycles by skipping back-edges."""
        result: List[str] = []
        visited: Set[str] = set()
        in_stack: Set[str] = set()

        def visit(n: str) -> None:
            if n in visited or n in in_stack:
                return
            in_stack.add(n)
            for dep in deps.get(n, set()):
                visit(dep)
            in_stack.discard(n)
            visited.add(n)
            result.append(n)

        for n in sorted(names):
            visit(n)
        return result

    # ------------------------------------------------------------------
    # Private — kept as instance aliases for brevity in _build
    # ------------------------------------------------------------------

    _ontology_hash = staticmethod(ontology_hash.__func__)  # type: ignore[attr-defined]
    _safe_name = staticmethod(safe_name.__func__)  # type: ignore[attr-defined]
    _extract_local = staticmethod(extract_local.__func__)  # type: ignore[attr-defined]
    _pluralize = staticmethod(pluralize.__func__)  # type: ignore[attr-defined]
    _normalize_base = staticmethod(normalize_base.__func__)  # type: ignore[attr-defined]
    _topo_sort = staticmethod(topo_sort.__func__)  # type: ignore[attr-defined]

    @staticmethod
    def _ensure_uri(name: str, base_uri: str) -> str:
        return f"{GraphQLSchemaBuilder.normalize_base(base_uri)}{name}"

    @staticmethod
    def _get_data_properties(cls_data: Dict, base_uri: str) -> List[Tuple[str, str]]:
        norm = GraphQLSchemaBuilder.normalize_base(base_uri)
        results: List[Tuple[str, str]] = []
        for key in ("dataProperties", "properties", "attributes"):
            items = cls_data.get(key, [])
            if not isinstance(items, list) or not items:
                continue
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name", "")
                    uri = item.get("uri", "")
                elif isinstance(item, str):
                    name = item
                    uri = ""
                else:
                    continue
                if name:
                    if not uri or not uri.startswith(norm):
                        uri = GraphQLSchemaBuilder._ensure_uri(name, base_uri)
                    results.append((name, uri))
            if results:
                break
        return results

    # ------------------------------------------------------------------
    # Core schema builder
    # ------------------------------------------------------------------

    def _build(
        self,
        classes: List[Dict],
        properties: List[Dict],
        base_uri: str,
    ) -> Optional[Tuple[strawberry.Schema, SchemaMetadata]]:
        if not classes:
            logger.warning("No ontology classes — cannot build GraphQL schema")
            return None

        class_names, class_by_name, uri_to_name = self._index_classes(classes)
        rel_by_domain, dep_graph = self._index_relationships(
            properties,
            base_uri,
            class_names,
            uri_to_name,
        )
        order = self.topo_sort(class_names, dep_graph)

        py_classes = self._create_python_classes(order, class_by_name, base_uri)
        self._add_relationship_annotations(py_classes, rel_by_domain)
        inv_by_range = self._add_inverse_annotations(py_classes, rel_by_domain)

        gql_types = self._apply_strawberry_types(order, py_classes)
        if not gql_types:
            logger.warning("No GraphQL types could be created")
            return None

        metadata = self._build_metadata(
            order,
            gql_types,
            class_by_name,
            rel_by_domain,
            base_uri,
            inv_by_range=inv_by_range,
            rel_labels=self._property_labels(properties),
        )
        schema = self._build_query(order, gql_types, metadata)
        if not schema:
            return None

        logger.info(
            "GraphQL schema built: %d types, %d inverse fields",
            len(gql_types),
            sum(len(v) for v in inv_by_range.values()),
        )
        return schema, metadata

    # ------------------------------------------------------------------
    # Build sub-steps
    # ------------------------------------------------------------------

    @staticmethod
    def _index_classes(
        classes: List[Dict],
    ) -> Tuple[Set[str], Dict[str, Dict], Dict[str, str]]:
        class_names: Set[str] = set()
        class_by_name: Dict[str, Dict] = {}
        uri_to_name: Dict[str, str] = {}
        for cls in classes:
            name = cls.get("name", "")
            uri = cls.get("uri", "")
            if not name:
                continue
            class_names.add(name)
            class_by_name[name] = cls
            if uri:
                uri_to_name[uri] = name
        return class_names, class_by_name, uri_to_name

    def _index_relationships(
        self,
        properties: List[Dict],
        base_uri: str,
        class_names: Set[str],
        uri_to_name: Dict[str, str],
    ) -> Tuple[Dict[str, List[RelEdge]], Dict[str, Set[str]]]:
        rel_by_domain: Dict[str, List[RelEdge]] = defaultdict(list)
        dep_graph: Dict[str, Set[str]] = defaultdict(set)
        norm = self.normalize_base(base_uri)

        for prop in properties:
            domain_uri = prop.get("domain", "")
            range_uri = prop.get("range", "")
            prop_name = prop.get("name", self.extract_local(prop.get("uri", "")))
            prop_uri = prop.get("uri", "")
            if not prop_uri or not prop_uri.startswith(norm):
                if prop_name:
                    prop_uri = self._ensure_uri(prop_name, base_uri)

            domain_name = uri_to_name.get(domain_uri) or self.extract_local(domain_uri)
            range_name = uri_to_name.get(range_uri) or self.extract_local(range_uri)

            if domain_name in class_names and range_name in class_names and prop_name:
                rel_by_domain[domain_name].append(RelEdge(prop_name, range_name, prop_uri))
                if range_name != domain_name:
                    dep_graph[domain_name].add(range_name)

        return rel_by_domain, dep_graph

    def _create_python_classes(
        self,
        order: List[str],
        class_by_name: Dict[str, Dict],
        base_uri: str,
    ) -> Dict[str, type]:
        py_classes: Dict[str, type] = OrderedDict()
        for name in order:
            cls_data = class_by_name.get(name)
            if not cls_data:
                continue

            annots: Dict[str, Any] = OrderedDict()
            annots["id"] = strawberry.ID
            annots["uri"] = str
            annots["label"] = Optional[str]
            defaults: Dict[str, Any] = {"id": "", "uri": "", "label": None}

            for attr_name, _ in self._get_data_properties(cls_data, base_uri):
                safe = self.safe_name(attr_name)
                if safe not in annots:
                    annots[safe] = Optional[str]
                    defaults[safe] = None

            ns: Dict[str, Any] = {"__annotations__": annots, **defaults}
            py_classes[name] = type(name, (), ns)
        return py_classes

    @staticmethod
    def _add_relationship_annotations(
        py_classes: Dict[str, type],
        rel_by_domain: Dict[str, List[RelEdge]],
    ) -> None:
        for domain_name, rels in rel_by_domain.items():
            domain_cls = py_classes.get(domain_name)
            if not domain_cls:
                continue
            for edge in rels:
                range_cls = py_classes.get(edge.range_name)
                if not range_cls:
                    continue
                safe = GraphQLSchemaBuilder.safe_name(edge.prop_name)
                if safe not in domain_cls.__annotations__:
                    domain_cls.__annotations__[safe] = Optional[List[range_cls]]
                    setattr(domain_cls, safe, None)

    @staticmethod
    def inverse_field_name(domain_name: str, prop_name: str) -> str:
        """Deterministic inverse field name, e.g. ``salesTransactionsViaSoldTo``."""
        prop = prop_name[:1].upper() + prop_name[1:]
        return GraphQLSchemaBuilder.safe_name(
            f"{GraphQLSchemaBuilder.pluralize(domain_name)}Via{prop}"
        )

    @staticmethod
    def _add_inverse_annotations(
        py_classes: Dict[str, type],
        rel_by_domain: Dict[str, List[RelEdge]],
    ) -> Dict[str, List[InverseField]]:
        """Add one inverse list field per object property on its range class.

        Returns ``{range_name: [InverseField, ...]}``.
        A name that collides with an existing field (in Python or camelCase
        GraphQL form) is skipped. Self-references and cycles are fine: all
        Python classes exist before any annotation is added.
        """
        inv_by_range: Dict[str, List[InverseField]] = defaultdict(list)
        for domain_name in sorted(rel_by_domain):
            domain_cls = py_classes.get(domain_name)
            if not domain_cls:
                continue
            for edge in rel_by_domain[domain_name]:
                range_name = edge.range_name
                range_cls = py_classes.get(range_name)
                if not range_cls:
                    continue
                fname = GraphQLSchemaBuilder.inverse_field_name(
                    domain_name, edge.prop_name
                )
                taken = set(range_cls.__annotations__)
                taken |= {to_camel_case(n) for n in taken}
                if fname in taken or to_camel_case(fname) in taken:
                    logger.info(
                        "GraphQL inverse field %s.%s skipped (name collision)",
                        range_name,
                        fname,
                    )
                    continue
                range_cls.__annotations__[fname] = Optional[List[domain_cls]]
                setattr(range_cls, fname, None)
                inv_by_range[range_name].append(
                    InverseField(fname, edge.prop_uri, domain_name)
                )
        return inv_by_range

    def _property_labels(self, properties: List[Dict]) -> Dict[str, str]:
        """Map object-property name -> ontology label (for the schema digest)."""
        out: Dict[str, str] = {}
        for prop in properties:
            name = prop.get("name") or self.extract_local(prop.get("uri", ""))
            label = prop.get("label") or ""
            if name and label:
                out[name] = label
        return out

    @staticmethod
    def _apply_strawberry_types(
        order: List[str], py_classes: Dict[str, type]
    ) -> Dict[str, type]:
        gql_types: Dict[str, type] = OrderedDict()
        for name in order:
            if name not in py_classes:
                continue
            try:
                gql_types[name] = strawberry.type(py_classes[name])
            except Exception as exc:
                logger.warning("Failed to create GraphQL type for %s: %s", name, exc)
        return gql_types

    def _build_metadata(
        self,
        order: List[str],
        gql_types: Dict[str, type],
        class_by_name: Dict[str, Dict],
        rel_by_domain: Dict[str, List[RelEdge]],
        base_uri: str,
        inv_by_range: Dict[str, List[InverseField]],
        rel_labels: Dict[str, str],
    ) -> SchemaMetadata:
        metadata = SchemaMetadata(base_uri=base_uri)
        for name in order:
            if name not in gql_types:
                continue
            cls_data = class_by_name.get(name, {})
            cls_uri = cls_data.get("uri", self._ensure_uri(name, base_uri))

            pred_to_field: Dict[str, str] = {}
            field_to_pred: Dict[str, str] = {}

            for attr_name, attr_uri in self._get_data_properties(cls_data, base_uri):
                safe = self.safe_name(attr_name)
                pred_to_field[attr_uri] = safe
                field_to_pred[safe] = attr_uri

            rel_info: Dict[str, RelTarget] = {}
            rel_label_info: Dict[str, str] = {}
            for edge in rel_by_domain.get(name, []):
                safe = self.safe_name(edge.prop_name)
                if edge.range_name in gql_types:
                    pred_to_field[edge.prop_uri] = safe
                    field_to_pred[safe] = edge.prop_uri
                    rel_info[safe] = RelTarget(edge.prop_uri, edge.range_name)
                    if rel_labels.get(edge.prop_name):
                        rel_label_info[safe] = rel_labels[edge.prop_name]

            inverse_info: Dict[str, InverseSource] = {
                inv.field_name: InverseSource(inv.prop_uri, inv.domain_name)
                for inv in inv_by_range.get(name, [])
                if inv.domain_name in gql_types
            }
            all_fields = list(field_to_pred) + list(inverse_info)
            metadata.register(
                TypeInfo(
                    name=name,
                    cls_uri=cls_uri,
                    gql_type=gql_types[name],
                    predicate_to_field=pred_to_field,
                    field_to_predicate=field_to_pred,
                    relationships=rel_info,
                    inverse_relationships=inverse_info,
                    graphql_names={f: to_camel_case(f) for f in all_fields},
                    label=cls_data.get("label") or "",
                    relationship_labels=rel_label_info,
                )
            )
        return metadata

    @staticmethod
    def _root_fields(
        metadata: SchemaMetadata, name: str, gql_type: type
    ) -> List[Tuple[str, str, Any, str]]:
        """Root query fields of type *name* as
        ``(TypeInfo attribute, field name, resolver, description)``."""
        plural = GraphQLSchemaBuilder.pluralize(name)
        singular = name[0].lower() + name[1:] if name else name
        return [
            (
                "list_field",
                plural,
                ResolverFactory.make_list_resolver(metadata, name, gql_type),
                f"List {name} entities (limit <= {MAX_LIST_LIMIT}).",
            ),
            (
                "count_field",
                f"{plural}Count",
                ResolverFactory.make_count_resolver(metadata, name),
                f"Exact count of {name} matches (distinctBy: distinct field values).",
            ),
            (
                "single_field",
                singular,
                ResolverFactory.make_single_resolver(metadata, name, gql_type),
                f"Get a single {name} by ID.",
            ),
        ]

    @staticmethod
    def _build_query(
        order: List[str],
        gql_types: Dict[str, type],
        metadata: SchemaMetadata,
    ) -> Optional[strawberry.Schema]:
        query_fields = []
        used: Set[str] = set()
        for name in order:
            if name not in gql_types:
                continue
            info = metadata.types.get(name)
            for attr, field_name, resolver, description in (
                GraphQLSchemaBuilder._root_fields(metadata, name, gql_types[name])
            ):
                if field_name in used:
                    logger.warning(
                        "GraphQL root field %s skipped (name collision)", field_name
                    )
                    continue
                used.add(field_name)
                query_fields.append(
                    strawberry.field(
                        name=field_name, resolver=resolver, description=description
                    )
                )
                if info:
                    setattr(info, attr, field_name)
        # Root field names land on TypeInfo after register(); drop any digest
        # rendered in between so it lists them.
        metadata.reset_digest()
        if not query_fields:
            logger.warning("No query fields could be created")
            return None

        Query = create_type("Query", query_fields)
        return strawberry.Schema(query=Query)


# ------------------------------------------------------------------
# Module-level singleton + backward-compatible API
# ------------------------------------------------------------------

_default_builder = GraphQLSchemaBuilder()


def build_schema_for_domain(
    ontology_classes: List[Dict],
    ontology_properties: List[Dict],
    base_uri: str,
    domain_name: str = "",
) -> Optional[Tuple[strawberry.Schema, SchemaMetadata]]:
    """Module-level delegate — uses the default ``GraphQLSchemaBuilder``."""
    return _default_builder.build_for_domain(
        ontology_classes,
        ontology_properties,
        base_uri,
        domain_name,
    )


def invalidate_cache(domain_name: str) -> None:
    """Module-level delegate — invalidates on the default builder."""
    _default_builder.invalidate_cache(domain_name)


# Backward-compatible aliases for helpers used by tests
_safe_name = GraphQLSchemaBuilder.safe_name
_extract_local = GraphQLSchemaBuilder.extract_local
_pluralize = GraphQLSchemaBuilder.pluralize
_normalize_base = GraphQLSchemaBuilder.normalize_base
_topo_sort = GraphQLSchemaBuilder.topo_sort
