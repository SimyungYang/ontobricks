"""Emit UC definition references and their synced text into an OWL graph."""

from typing import Any, Callable, Dict, List, Optional

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from shared.config.constants import ONTOBRICKS_NS

# Annotation properties declared once when at least one reference exists.
DEFINITION_ANNOTATION_PROPERTIES = (
    ONTOBRICKS_NS.derivedFrom,
    ONTOBRICKS_NS.glossaryRef,
    ONTOBRICKS_NS.syncedAt,
    ONTOBRICKS_NS.definitionSource,
    ONTOBRICKS_NS.metricExpression,
    ONTOBRICKS_NS.definitionWarning,
)


class OntologyDefinitionAnnotations:
    """Annotate referenced classes / datatype properties of a generated OWL graph.

    Each annotation dict carries ``class_uri``, ``attribute`` (``""`` for a
    class), optional ``derived_from`` / ``glossary_ref`` / ``warning`` and
    ``effective`` -- the synced UC text (``comment``, ``alt_labels``, ``expr``,
    ``sources``, ``synced_at``) or ``None`` before the first sync. Once synced,
    the subject's ``rdfs:comment`` and ``skos:altLabel`` are replaced by the UC
    values (UC is the source of truth); items without a reference are untouched.
    """

    def __init__(self, graph: Graph, resolve: Callable[[str], Optional[URIRef]]) -> None:
        self._graph = graph
        self._resolve = resolve

    def apply(self, annotations: Optional[List[Dict[str, Any]]], classes: List[Dict]) -> int:
        """Annotate every resolvable item; return the number of annotated subjects."""
        if not annotations:
            return 0
        class_names = {c.get("uri"): c.get("name") for c in classes or [] if c.get("uri")}
        for prop in DEFINITION_ANNOTATION_PROPERTIES:
            self._graph.add((prop, RDF.type, OWL.AnnotationProperty))
        annotated = 0
        for item in annotations:
            subject = self._subject(item, class_names)
            if subject is None:
                continue
            self._annotate(subject, item, item.get("effective"))
            annotated += 1
        return annotated

    def _subject(self, item: Dict[str, Any], class_names: Dict[str, str]) -> Optional[URIRef]:
        name = item.get("attribute") or class_names.get(item.get("class_uri"))
        subject = self._resolve(name) if name else None
        if subject is None or (subject, RDF.type, None) not in self._graph:
            return None
        return subject

    def _annotate(
        self, subject: URIRef, item: Dict[str, Any], values: Optional[Dict[str, Any]]
    ) -> None:
        g = self._graph
        target = item.get("derived_from") or {}
        if target.get("view"):
            derived = f"{target['view']}#{target.get('column', '')}"
            g.add((subject, ONTOBRICKS_NS.derivedFrom, Literal(derived)))
        ref = item.get("glossary_ref")
        if ref:
            g.add((subject, ONTOBRICKS_NS.glossaryRef, Literal(ref)))
            if ref.startswith(("http://", "https://")):
                g.add((subject, RDFS.seeAlso, URIRef(ref)))
        if item.get("warning"):
            g.add((subject, ONTOBRICKS_NS.definitionWarning, Literal(item["warning"])))
        if values is None:
            return
        g.remove((subject, RDFS.comment, None))
        g.remove((subject, SKOS.altLabel, None))
        if values["comment"]:
            g.add((subject, RDFS.comment, Literal(values["comment"])))
        for label in values["alt_labels"]:
            g.add((subject, SKOS.altLabel, Literal(label)))
        if values["expr"]:
            g.add((subject, ONTOBRICKS_NS.metricExpression, Literal(values["expr"])))
        for source in values["sources"]:
            g.add((subject, ONTOBRICKS_NS.definitionSource, Literal(source)))
        if values["synced_at"]:
            stamp = Literal(values["synced_at"], datatype=XSD.dateTime)
            g.add((subject, ONTOBRICKS_NS.syncedAt, stamp))
