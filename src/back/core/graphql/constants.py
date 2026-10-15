"""GraphQL package constants."""

from back.core.graphdb.constants import (  # noqa: F401
    RDF_TYPE,
    RDFS_LABEL,
    REVERSE_LOOKUP_MAX_ROWS,
)

DEFAULT_DEPTH = 2
MAX_DEPTH = 5

# Root list queries: default page size and hard ceiling. The ceiling is high
# enough for a filtered fact list (~100-500 rows) to come back in one call.
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 1000

# Total reverse-edge rows fetched for one inverse field in one query level.
INVERSE_MAX_ROWS = REVERSE_LOOKUP_MAX_ROWS

# Upper bound (chars) for the schema digest injected into agent prompts.
MAX_DIGEST_CHARS = 9000
