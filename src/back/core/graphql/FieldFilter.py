"""GraphQL input type for relationship / attribute filters on list queries."""

import strawberry


@strawberry.input(
    description=(
        "AND-combined filter. On a relationship field it matches when the "
        "target's label contains `value` (case-insensitive) or the target id "
        "equals it; on a data field it is a case-insensitive contains; "
        "field `id` matches the entity id exactly."
    )
)
class FieldFilter:
    """One ``{field, value}`` condition of a ``filters`` argument."""

    field: str
    value: str
