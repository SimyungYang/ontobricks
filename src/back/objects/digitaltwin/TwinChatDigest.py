"""Compact GraphQL schema digest of a domain for the Graph Chat prompt."""

from __future__ import annotations

from typing import Any

from back.core.errors import OntoBricksError
from back.core.logging import get_logger

logger = get_logger(__name__)


class TwinChatDigest:
    """Builds the schema digest the chat agent receives in its system prompt."""

    @staticmethod
    def build(domain: Any, domain_name: str) -> str:
        """Compact GraphQL schema digest of the domain for the chat prompt.

        Built from the same cached schema as ``/dtwin/graphql/*``: the
        builder caches per *domain_name* + ontology hash and the digest is
        memoised on the metadata, so *domain_name* must be the same display
        name those routes use. Returns ``""`` when the ontology cannot back
        a schema so the agent falls back to ``get_graphql_schema``.
        """
        from graphql import GraphQLError
        from strawberry.exceptions import StrawberryException

        from back.core.graphql import build_schema_for_domain
        from shared.config.constants import DEFAULT_BASE_URI

        ontology = getattr(domain, "ontology", None) or {}
        classes = ontology.get("classes", []) or []
        if not classes:
            return ""
        try:
            built = build_schema_for_domain(
                classes,
                ontology.get("properties", []) or [],
                ontology.get("base_uri", DEFAULT_BASE_URI),
                domain_name,
            )
        except (
            OntoBricksError,
            StrawberryException,
            GraphQLError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            # The digest is an optional prompt hint: a malformed ontology
            # must not fail the chat request.
            logger.warning("GraphChat: schema digest unavailable: %s", exc)
            return ""
        if not built:
            return ""
        _schema, metadata = built
        return metadata.digest()
