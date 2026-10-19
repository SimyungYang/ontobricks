"""GraphQL resolver factory functions."""

from typing import Any, Callable, Dict, Iterable, List, Optional

import strawberry
from strawberry.types import Info
from strawberry.types.nodes import FragmentSpread, InlineFragment, SelectedField

from back.core.graphql.FieldFilter import FieldFilter
from back.core.graphql.SchemaMetadata import SchemaMetadata
from back.core.graphql.constants import DEFAULT_LIST_LIMIT
from back.core.logging import get_logger

logger = get_logger(__name__)

# Argument docs live once on the FieldFilter input type and in the schema
# digest; per-field argument descriptions would repeat in the SDL for every
# type and bloat the get_graphql_schema output.
_FiltersArg = Optional[List[FieldFilter]]


class ResolverFactory:
    """Factory for creating GraphQL resolvers from schema metadata."""

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def selection_tree(info: Info) -> Optional[Dict[str, dict]]:
        """Return the sub-selection tree of the field being resolved.

        Keys are GraphQL field names; values are nested trees (``{}`` for
        scalars). Fragments are merged in. Returns ``None`` when strawberry
        exposes no selection info, so callers fall back to eager resolution.
        """
        try:
            fields = info.selected_fields
        except (AttributeError, KeyError, TypeError) as exc:
            # strawberry's convert_selections raises TypeError on an unknown
            # node type, KeyError on an undefined fragment and AttributeError
            # on an inline fragment without type condition (or a non-strawberry
            # ``info``). Selection info is optional: fall back to eager loading.
            logger.debug("GraphQL selection info unavailable: %s", exc)
            return None
        if not fields:
            return None
        tree: Dict[str, dict] = {}
        for root in fields:
            if isinstance(root, SelectedField):
                ResolverFactory._merge_into(tree, root.selections)
        return tree

    @staticmethod
    def _merge_into(tree: Dict[str, dict], selections: Iterable[Any]) -> None:
        """Merge strawberry *selections* into *tree* in place.

        Each selected field becomes ``tree[name]`` (its own sub-tree, ``{}``
        for scalars); repeated fields merge. Fragment spreads and inline
        fragments are flattened into the current level (type conditions are
        ignored -- every generated type is concrete).
        """
        for sel in selections or []:
            if isinstance(sel, SelectedField):
                sub = tree.setdefault(sel.name, {})
                ResolverFactory._merge_into(sub, sel.selections)
            elif isinstance(sel, (FragmentSpread, InlineFragment)):
                ResolverFactory._merge_into(tree, sel.selections)

    # ------------------------------------------------------------------
    # Root resolvers
    # ------------------------------------------------------------------

    @staticmethod
    def make_list_resolver(
        metadata: SchemaMetadata,
        type_name: str,
        gql_type: type,
    ) -> Callable:
        """Create a root list resolver for a given entity type."""

        def resolver(
            info: Info,
            limit: int = DEFAULT_LIST_LIMIT,
            offset: int = 0,
            search: Optional[str] = None,
            filters: _FiltersArg = None,
        ) -> List[gql_type]:  # type: ignore[valid-type]
            store = info.context["triplestore"]
            table = info.context["table_name"]
            depth = info.context.get("depth")
            return metadata.resolve_list(
                store,
                table,
                type_name,
                limit,
                offset,
                search,
                depth=depth,
                filters=filters,
                selection=ResolverFactory.selection_tree(info),
            )

        resolver.__name__ = f"resolve_{type_name}_list"
        return resolver

    @staticmethod
    def make_count_resolver(
        metadata: SchemaMetadata,
        type_name: str,
    ) -> Callable:
        """Create a root count resolver (``<plural>Count``) for a type."""

        def resolver(
            info: Info,
            search: Optional[str] = None,
            filters: _FiltersArg = None,
            distinct_by: Optional[str] = None,
        ) -> int:
            store = info.context["triplestore"]
            table = info.context["table_name"]
            return metadata.resolve_count(
                store,
                table,
                type_name,
                search=search,
                filters=filters,
                distinct_by=distinct_by,
            )

        resolver.__name__ = f"resolve_{type_name}_count"
        return resolver

    @staticmethod
    def make_single_resolver(
        metadata: SchemaMetadata,
        type_name: str,
        gql_type: type,
    ) -> Callable:
        """Create a root single-entity resolver for a given type."""

        def resolver(
            info: Info,
            id: strawberry.ID,
        ) -> Optional[gql_type]:  # type: ignore[valid-type]
            store = info.context["triplestore"]
            table = info.context["table_name"]
            depth = info.context.get("depth")
            return metadata.resolve_single(
                store,
                table,
                type_name,
                str(id),
                depth=depth,
                selection=ResolverFactory.selection_tree(info),
            )

        resolver.__name__ = f"resolve_{type_name}_single"
        return resolver
