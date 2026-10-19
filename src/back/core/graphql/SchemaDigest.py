"""Compact, LLM-oriented digest of a generated GraphQL schema.

The full SDL of a 30-type ontology is ~15-20 KB and the Graph Chat agent used
to re-fetch it on every turn. The digest lists, per type, only what an agent
needs to write a query without introspection: the root field names, data
attributes, forward relationships (with their ontology labels) and the
generated inverse fields. It is rendered once per schema build and memoised
on :class:`SchemaMetadata`, so it is cached per domain alongside the schema.
"""

from typing import TYPE_CHECKING, List, NamedTuple, Optional

from back.core.graphql.constants import MAX_DIGEST_CHARS, MAX_LIST_LIMIT
from back.core.logging import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from back.core.graphql.SchemaMetadata import SchemaMetadata
    from back.core.graphql.models import TypeInfo

# Appended when even the most compact level exceeds the size budget.
_TRUNCATION_MARKER = "\n[digest truncated; call get_graphql_schema for the rest]"


class _DigestLevel(NamedTuple):
    """One detail level tried by :meth:`SchemaDigest.render`."""

    max_attrs: Optional[int]  # attributes listed per type; None = all
    with_labels: bool  # include relationship ontology labels


class SchemaDigest:
    """Render a :class:`SchemaMetadata` registry as a short text digest."""

    # Most to least detailed; the first level that fits the budget wins.
    # Relationship labels go before attributes: a missing attribute name
    # reads as "field does not exist" to the agent.
    _LEVELS = (
        _DigestLevel(None, True),
        _DigestLevel(None, False),
        _DigestLevel(8, True),
        _DigestLevel(4, False),
        _DigestLevel(0, False),
    )

    @classmethod
    def render(
        cls, metadata: "SchemaMetadata", max_chars: int = MAX_DIGEST_CHARS
    ) -> str:
        """Return the digest, shrinking detail until it fits *max_chars*."""
        if not getattr(metadata, "types", None):
            return ""
        text = ""
        for level in cls._LEVELS:
            text = cls._render(metadata, level.max_attrs, level.with_labels)
            if len(text) <= max_chars:
                cls._log_level(level, len(text), max_chars)
                return text
        logger.warning(
            "Schema digest truncated at %d chars (cap %d): the agent sees partial "
            "types and may report existing fields as missing",
            len(text),
            max_chars,
        )
        return text[: max_chars - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER

    @staticmethod
    def _log_level(level: "_DigestLevel", chars: int, max_chars: int) -> None:
        """Log the chosen detail level; hidden attributes get a warning."""
        if level.max_attrs is None:
            logger.info(
                "Schema digest: all attributes, labels=%s, %d chars (cap %d)",
                level.with_labels,
                chars,
                max_chars,
            )
        else:
            logger.warning(
                "Schema digest: attributes cut to %d per type, labels=%s, %d chars "
                "(cap %d) -- raise MAX_DIGEST_CHARS",
                level.max_attrs,
                level.with_labels,
                chars,
                max_chars,
            )

    @classmethod
    def _render(
        cls,
        metadata: "SchemaMetadata",
        max_attrs: Optional[int],
        with_labels: bool,
    ) -> str:
        lines: List[str] = [
            "Every type has: id, uri, label. Root fields per type: "
            f"<list>(limit<={MAX_LIST_LIMIT}, offset, search, "
            "filters:[{field, value}]), <list>Count(search, filters, distinctBy), "
            "<single>(id). Relationship / inverse fields return lists.",
        ]
        for name in sorted(metadata.types):
            info = metadata.types[name]
            lines.extend(cls._type_lines(info, max_attrs, with_labels))
        return "\n".join(lines)

    @classmethod
    def _type_lines(
        cls, info: "TypeInfo", max_attrs: Optional[int], with_labels: bool
    ) -> List[str]:
        label = f' "{info.label}"' if info.label and info.label != info.name else ""
        roots = ", ".join(
            f for f in (info.list_field, info.count_field, info.single_field) if f
        )
        out = [f"- {info.name}{label} [{roots}]"]

        attrs = [info.gql_name(f) for f in info.data_fields()]
        if max_attrs is None or len(attrs) <= max_attrs:
            shown = attrs
        else:
            shown = attrs[:max_attrs] + [f"+{len(attrs) - max_attrs} more"]
        if shown:
            out.append("    attrs: " + ", ".join(shown))

        rels = []
        for field_name, rel in info.relationships.items():
            text = f"{info.gql_name(field_name)}->{rel.target_type}"
            rel_label = info.relationship_labels.get(field_name, "")
            if with_labels and rel_label and rel_label != field_name:
                text += f' "{rel_label}"'
            rels.append(text)
        if rels:
            out.append("    rels: " + ", ".join(rels))

        inverse = [
            f"{info.gql_name(field_name)}<-{inverse.source_type}"
            for field_name, inverse in info.inverse_relationships.items()
        ]
        if inverse:
            out.append("    inverse: " + ", ".join(inverse))
        return out
