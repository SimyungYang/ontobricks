"""Graph database backend abstraction.

The single triple-store / graph DB layer for OntoBricks.  All backends operate
on the ``(subject, predicate, object)`` model and subclass
:class:`GraphDBBackend`.  Engines are pluggable under
``back/core/graphdb/<engine>/`` — the default is Lakebase Postgres
(``lakebase``), with a Databricks Delta engine (``delta``) and a raw read-only
view store (``view``).  See ``_starter_kit/`` for a copy-paste template.
"""

from back.core.graphdb.GraphDBBackend import GraphDBBackend  # noqa: F401
from back.core.graphdb.GraphDBFactory import GraphDBFactory  # noqa: F401
from back.core.graphdb.constants import (  # noqa: F401
    RDF_TYPE,
    RDFS_LABEL,
    REVERSE_LOOKUP_MAX_ROWS,
)
from back.core.graphdb.SubjectFilter import SubjectFilter  # noqa: F401
from back.core.graphdb.SubjectFilterKind import SubjectFilterKind  # noqa: F401

get_graphdb = GraphDBFactory.get_graphdb
GRAPHDB_AVAILABLE = GraphDBFactory.LAKEBASE_AVAILABLE

__all__ = [
    "GraphDBBackend",
    "GraphDBFactory",
    "SubjectFilter",
    "SubjectFilterKind",
    "GRAPHDB_AVAILABLE",
    "get_graphdb",
    "RDF_TYPE",
    "RDFS_LABEL",
    "REVERSE_LOOKUP_MAX_ROWS",
]
