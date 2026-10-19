"""Backend-neutral subject filter used by typed entity lookups."""

from dataclasses import dataclass

from back.core.graphdb.SubjectFilterKind import SubjectFilterKind


@dataclass(frozen=True)
class SubjectFilter:
    """One AND-combined condition on the subjects of a typed lookup.

    ``kind`` (a :class:`SubjectFilterKind` or its string value) selects the
    matching rule:

    * ``data``         -- a literal of ``predicate`` contains ``value``
      (case-insensitive).
    * ``relationship`` -- the subject points through ``predicate`` at a target
      whose ``rdfs:label`` contains ``value`` (case-insensitive), or whose URI
      / local name equals ``value``.
    * ``id``           -- the subject's URI or local name equals ``value``
      (``predicate`` is ignored).

    Backends raise :class:`ValidationError` for any other kind.
    """

    kind: str
    value: str
    predicate: str = ""

    # Backward-compatible aliases (``SubjectFilter.DATA`` etc.).
    DATA = SubjectFilterKind.DATA
    RELATIONSHIP = SubjectFilterKind.RELATIONSHIP
    ID = SubjectFilterKind.ID
