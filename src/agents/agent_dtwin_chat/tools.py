"""
Graph Chat Agent -- tool definitions and handlers.

Each tool issues an HTTP request over loopback to the running OntoBricks
app, mirroring the tools exposed by the MCP server (see
``src/mcp-server/server/app.py``).  The selected domain is resolved from
the active user session and injected as a query parameter, so the LLM
never has to call ``list_domains`` / ``select_domain``.

All tools call *internal*, session-aware routes under ``/dtwin/...``
so they keep working against domains that exist only in the active
session and have never been published to the registry (the public
``/api/v1/digitaltwin/*`` routes require a published version and would
404 with ``not_found: No versions found for domain "<name>"``).

Tools:
    * ``list_entity_types``     -- GET  /dtwin/sync/stats
    * ``describe_entity``       -- GET  /dtwin/triples/find
    * ``get_status``            -- GET  /dtwin/sync/status
    * ``get_graphql_schema``    -- GET  /dtwin/graphql/schema
    * ``query_graphql``         -- POST /dtwin/graphql/execute
    * ``get_entity_context``    -- GET  /dtwin/nodes/context
    * ``request_entity_action`` -- POST /dtwin/nodes/action/request

``list_entity_types`` and ``describe_entity`` are enriched with per-class
Actions metadata (linked dataset, invocable Unity Catalog functions) fetched
once per session from GET /dtwin/classes and cached on
``ToolContext.dtwin_class_actions``. ``request_entity_action`` only mints a
*pending* action (stored on ``ToolContext.pending_action``); the UI renders
a confirmation card and a separate ``/dtwin/nodes/action/confirm`` call
(outside the LLM tool loop) actually invokes the Unity Catalog function.

Note: ``run_sparql`` (POST /dtwin/execute) is implemented but excluded from
the active tool set — it queries the warehouse Delta view and cannot see
inferred/reasoning triples. Use ``describe_entity`` or ``query_graphql`` for
full-graph access.
"""

from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional

import httpx  # noqa: F401 — kept for httpx.HTTPStatusError in tool handlers

from agents.agent_dtwin_chat.analysis import (
    DEFAULT_METRIC,
    DEFAULT_ORDER,
    DEFAULT_TOP,
    MAX_TOP,
    METRICS,
    SET_OPERATIONS,
    Executor,
    analyze,
)
from agents.tools.context import ToolContext
from agents.tools.loopback_http import loopback_client, loopback_registry_params
from agents.tools.graph_formatting import (
    format_find_response,
    format_graphql_response,
    format_node_context_response,
    format_sparql_rows,
    local_name,
    pretty_predicate,
)
from back.core.errors import ValidationError
from back.core.logging import get_logger
from back.core.w3c.sparql import require_read_only_sparql

logger = get_logger(__name__)

_HTTP_TIMEOUT = 120  # fallback floor when the graph timeout can't be resolved
_HTTP_TIMEOUT_MARGIN_S = 30
_MAX_DEPTH = 1
# Triples fetched per describe_entity call. Every tool result is re-sent to
# the LLM on each later iteration, so large pages inflate prompt tokens and
# latency for the whole turn.
_FIND_PAGE_LIMIT = 200
# Hard ceiling on the text handed back to the LLM by graph tools.
_MAX_TOOL_RESULT_CHARS = 20_000
# query_graphql gets a higher ceiling: with the filter / count fast path a
# turn needs one or two GraphQL calls, so the larger result is re-sent at
# most once or twice, and a complete filtered fact list (~110 rows with a
# few nested refs + uris, ~20-25k chars in the compact format) must fit
# without truncation so counts and "who" answers are exact.
_MAX_GRAPHQL_RESULT_CHARS = 32_000

_ACTION_HINT = (
    "call request_entity_action(entity_uri, action) to propose one "
    "(UI confirmation required)"
)


# =====================================================
# Internal helpers
# =====================================================


def _client(ctx: ToolContext):
    """Build a sync HTTP client bound to the loopback OntoBricks URL.

    The timeout is derived from the server-side graph statement timeout plus a
    margin, so a bounded query is cancelled *server-side* (returning a clean
    error the LLM can react to) before the loopback client gives up on a query
    that is still running.
    """
    try:
        from back.core.query_limits import get_graph_query_timeout_s

        timeout = max(
            _HTTP_TIMEOUT, get_graph_query_timeout_s() + _HTTP_TIMEOUT_MARGIN_S
        )
    except Exception:  # noqa: BLE001
        timeout = _HTTP_TIMEOUT
    return loopback_client(ctx, timeout=timeout)


def _registry_params(ctx: ToolContext) -> dict:
    return loopback_registry_params(ctx)


def _cap_result(
    text: str,
    max_chars: int | None = None,
    hint: str = "Narrow the search (exact id, entity_type) for the rest.",
) -> str:
    """Trim a tool result to *max_chars* with an explicit partial-result note."""
    cap = max_chars or _MAX_TOOL_RESULT_CHARS
    if len(text) <= cap:
        return text
    return (
        text[:cap]
        + f"\n\n[...truncated, {len(text):,} chars total -- this result is "
        f"PARTIAL. {hint}]"
    )


def _error(msg: str) -> str:
    logger.warning("agent_dtwin_chat: %s", msg)
    return json.dumps({"error": msg})


def _get_ontology_labels(ctx: ToolContext) -> dict:
    """Return (and lazily populate) the ontology URI→label map on the context."""
    if ctx.dtwin_ontology_labels:
        return ctx.dtwin_ontology_labels
    try:
        with _client(ctx) as c:
            resp = c.get("/ontology/load")
            if resp.status_code != 200:
                return {}
            data = resp.json()
        config = data.get("config", {}) if isinstance(data, dict) else {}
        labels: dict = {}
        for cls in config.get("classes", []):
            lbl = cls.get("label") or cls.get("name") or ""
            uri = cls.get("uri", "")
            name = cls.get("name", "")
            if uri and lbl:
                labels[uri] = lbl
            if name and lbl:
                labels[name.lower()] = lbl
        for prop in config.get("properties", []):
            lbl = prop.get("label") or prop.get("name") or ""
            uri = prop.get("uri", "")
            name = prop.get("name", "")
            if uri and lbl:
                labels[uri] = lbl
            if name and lbl:
                labels[name.lower()] = lbl
        ctx.dtwin_ontology_labels = labels
        logger.info("Loaded %d ontology labels for graph chat", len(labels))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load ontology labels: %s", exc)
    return ctx.dtwin_ontology_labels


def _get_class_actions(ctx: ToolContext) -> dict:
    """Return (and lazily populate) the class URI→Actions-metadata map on the context.

    Actions metadata (linked dataset, cross-domain bridges, invocable Unity
    Catalog functions) is authored per ontology class and used to enrich
    ``list_entity_types`` / ``describe_entity`` output and to power
    ``get_entity_context`` / ``request_entity_action``.
    """
    if ctx.dtwin_class_actions:
        return ctx.dtwin_class_actions
    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/classes")
            if resp.status_code != 200:
                return {}
            data = resp.json()
        out = {}
        for item in data.get("classes") or []:
            uri = item.get("uri") or ""
            if not uri:
                continue
            out[uri] = {
                "name": item.get("name", ""),
                "dataset": item.get("dataset"),
                "bridges": item.get("bridges") or [],
                "actions": item.get("actions") or [],
            }
        ctx.dtwin_class_actions = out
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load class actions: %s", exc)
    return ctx.dtwin_class_actions


# =====================================================
# Tool handlers
# =====================================================


def tool_list_entity_types(ctx: ToolContext, **_kwargs) -> str:
    """List entity types + counts + aggregate statistics."""
    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/sync/stats")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"stats request failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"stats request error: {exc}")

    if not data.get("success"):
        return data.get("message", "Could not retrieve statistics.")

    lines: list[str] = []
    lines.append(f"Graph Viewer -- {ctx.dtwin_domain_name}")
    lines.append("=" * 40)
    inferred = data.get("inferred_triples", 0)
    lines.append(f"Total triples:       {data.get('total_triples', 0):,}")
    lines.append(f"Distinct entities:   {data.get('distinct_subjects', 0):,}")
    lines.append(f"Distinct predicates: {data.get('distinct_predicates', 0):,}")
    lines.append(f"Labels:              {data.get('label_count', 0):,}")
    lines.append(f"Type assertions:     {data.get('type_assertion_count', 0):,}")
    lines.append(f"Relationships:       {data.get('relationship_count', 0):,}")
    if inferred > 0:
        lines.append(
            f"Inferred triples:    {inferred:,}  "
            f"[reasoning output — use describe_entity to query them; "
            f"query_graphql may miss predicates not in the schema]"
        )
    lines.append("")

    onto_labels = _get_ontology_labels(ctx)
    class_actions = _get_class_actions(ctx)

    entity_types = data.get("entity_types", [])
    if entity_types:
        lines.append("Entity Types")
        lines.append("-" * 40)
        for et in entity_types:
            uri = et.get("uri", "")
            count = et.get("count", 0)
            key = local_name(uri).lower()
            name = onto_labels.get(uri) or onto_labels.get(key) or local_name(uri)
            lines.append(f"  - {name}  ({count:,} instances)")
            lines.append(f"    URI: {uri}")
            actions = class_actions.get(uri) or {}
            dataset = actions.get("dataset") or {}
            if dataset.get("fullName"):
                lines.append(f"    Dataset: {dataset['fullName']}")
                desc = (dataset.get("description") or "").strip()
                if desc:
                    lines.append(f"    Description: {desc}")
            for fn_action in actions.get("actions") or []:
                fn_desc = (fn_action.get("description") or "").strip()
                suffix = f" — {fn_desc}" if fn_desc else ""
                lines.append(f"    Action: {fn_action.get('fullName', '')}{suffix}")
        lines.append("")

    top_predicates = data.get("top_predicates", [])
    if top_predicates:
        lines.append("Predicates (attributes & relationships)")
        lines.append("-" * 40)
        for tp in top_predicates:
            uri = tp.get("uri", "")
            count = tp.get("count", 0)
            key = local_name(uri).lower()
            name = onto_labels.get(uri) or onto_labels.get(key) or pretty_predicate(uri)
            lines.append(f"  - {name}  ({count:,} usages)")

    return "\n".join(lines)


def tool_describe_entity(
    ctx: ToolContext,
    *,
    search: str | None = None,
    entity_type: str | None = None,
    depth: int = _MAX_DEPTH,
    **_kwargs,
) -> str:
    """Search for entities and traverse their relationships."""
    if not search and not entity_type:
        return _error("Please provide at least a search term or an entity type.")

    params: dict = {
        "depth": min(max(int(depth or _MAX_DEPTH), 1), 10),
        "limit": _FIND_PAGE_LIMIT,
        "offset": 0,
    }
    if search:
        params["search"] = search
    if entity_type:
        params["entity_type"] = entity_type

    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/triples/find", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"triples/find failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"triples/find error: {exc}")

    return _cap_result(
        format_find_response(
            data,
            ontology_labels=_get_ontology_labels(ctx),
            class_actions=_get_class_actions(ctx),
            action_invoke_hint=_ACTION_HINT,
        )
    )


def tool_get_status(ctx: ToolContext, **_kwargs) -> str:
    """Return the selected domain's triple-store status + row count."""
    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/sync/status")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"status failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"status error: {exc}")

    status = data.get("reason") or ("OK" if data.get("success") else "unknown")
    return (
        f"Domain:  {ctx.dtwin_domain_name}\n"
        f"View:    {data.get('view_table', 'N/A')}\n"
        f"Graph:   {data.get('graph_name', 'N/A')}\n"
        f"Status:  {status}\n"
        f"Data:    {'Yes' if data.get('has_data') else 'No'} "
        f"({data.get('count', 0):,} triples)"
    )


def tool_get_graphql_schema(ctx: ToolContext, **_kwargs) -> str:
    """Return the domain's auto-generated GraphQL schema (SDL).

    Uses the internal, session-aware ``/dtwin/graphql/schema`` endpoint
    so this works even when the domain has never been saved / published
    to the registry — the schema is generated on the fly from the
    in-session ontology.
    """
    domain = ctx.dtwin_domain_name
    if not domain:
        return _error("No domain selected in the current session.")

    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/graphql/schema")
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"GraphQL schema failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"GraphQL schema error: {exc}")

    if not data.get("success"):
        return data.get("message") or "Could not build GraphQL schema."

    sdl = data.get("sdl", "")
    if not sdl:
        return "GraphQL schema is empty -- the domain may have no ontology classes."

    return (
        f"GraphQL Schema -- {domain}\n"
        + ("=" * 50)
        + "\n\n"
        + sdl
        + "\n\nUse query_graphql to execute queries against this schema."
    )


def _graphql_executor(client: httpx.Client) -> Executor:
    """Return a function that POSTs a GraphQL query to the loopback execute route."""

    def execute(query: str, variables: Optional[dict] = None) -> dict:
        body = {"query": query, **({"variables": variables} if variables else {})}
        resp = client.post("/dtwin/graphql/execute", json=body)
        resp.raise_for_status()
        return resp.json()

    return execute


def _json_arg(value, name: str):
    """Accept a JSON string or an already-decoded value for tool arguments."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid JSON in '{name}' argument.") from exc


def tool_query_graphql(
    ctx: ToolContext,
    *,
    query: str,
    variables: dict | str | None = None,
    **_kwargs,
) -> str:
    """Execute a GraphQL query against the selected domain."""
    domain = ctx.dtwin_domain_name
    if not domain:
        return _error("No domain selected in the current session.")
    if not query:
        return _error("Missing required 'query' argument.")
    try:
        query_variables = _json_arg(variables or None, "variables")
    except ValidationError as exc:
        return _error(str(exc))

    try:
        with _client(ctx) as c:
            data = _graphql_executor(c)(query, query_variables)
    except httpx.HTTPStatusError as exc:
        return _error(
            f"GraphQL query failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"GraphQL query error: {exc}")

    return _cap_result(
        format_graphql_response(data, domain),
        max_chars=_MAX_GRAPHQL_RESULT_CHARS,
        hint=(
            "Select fewer fields, add filters, or use the <list>Count field "
            "for exact totals."
        ),
    )


def tool_analyze_graphql(
    ctx: ToolContext,
    *,
    query: str,
    operation: str,
    source: str | None = None,
    by: list | str | None = None,
    metric: str | None = DEFAULT_METRIC,
    field: str | None = None,
    where: list | str | None = None,
    restrict: dict | str | None = None,
    sets: list | str | None = None,
    top: int | None = None,
    order: str | None = DEFAULT_ORDER,
    variables: dict | str | None = None,
    **_kwargs,
) -> str:
    """Fetch every row of a GraphQL query and aggregate it server-side."""
    if not ctx.dtwin_domain_name:
        return _error("No domain selected in the current session.")
    if not query or not operation:
        return _error("Missing required 'query' / 'operation' argument.")

    try:
        by_paths = _json_arg(by, "by")
        options = {
            "operation": operation,
            "variables": _json_arg(variables, "variables"),
            "source": source,
            "by": [by_paths] if isinstance(by_paths, str) else by_paths,
            "metric": metric or DEFAULT_METRIC,
            "field": field,
            "where": _json_arg(where, "where"),
            "restrict": _json_arg(restrict, "restrict"),
            "sets": _json_arg(sets, "sets"),
            "top": top or DEFAULT_TOP,
            "order": order or DEFAULT_ORDER,
        }
        with _client(ctx) as c:
            return _cap_result(analyze(query, _graphql_executor(c), **options))
    except ValidationError as exc:  # includes AnalysisError
        return _error(str(exc))
    except httpx.HTTPStatusError as exc:
        return _error(
            f"GraphQL query failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"analyze_graphql error: {exc}")


def tool_get_entity_context(
    ctx: ToolContext,
    *,
    entity_uri: str,
    fetch_dataset_rows: bool = False,
    dataset_row_limit: int = 5,
    follow_bridges: bool = False,
    **_kwargs,
) -> str:
    """Return linked dataset rows and/or cross-domain bridge entities for a node."""
    if not entity_uri:
        return _error("Missing required 'entity_uri' argument.")

    params: dict = {
        "entity_uri": entity_uri,
        "fetch_dataset_rows": str(bool(fetch_dataset_rows)).lower(),
        "dataset_row_limit": min(max(int(dataset_row_limit or 5), 1), 20),
        "follow_bridges": str(bool(follow_bridges)).lower(),
    }

    try:
        with _client(ctx) as c:
            resp = c.get("/dtwin/nodes/context", params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"node context request failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"node context request error: {exc}")

    return format_node_context_response(data, action_invoke_hint=_ACTION_HINT)


def tool_request_entity_action(
    ctx: ToolContext,
    *,
    entity_uri: str,
    action: str,
    **_kwargs,
) -> str:
    """Mint a one-time pending-action token for a UC function on an entity.

    Does *not* invoke the function — the UI renders a confirmation card from
    ``ctx.pending_action`` and a separate, out-of-band call confirms it.
    """
    if not entity_uri or not action:
        return _error("Missing required 'entity_uri' and/or 'action' argument.")

    body = {"entity_uri": entity_uri, "action_full_name": action}

    try:
        with _client(ctx) as c:
            resp = c.post("/dtwin/nodes/action/request", json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"action request failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"action request error: {exc}")

    if not data.get("success"):
        return data.get("message") or "Could not request this action."

    pending = data.get("pending_action") or {}
    ctx.pending_action = pending
    entity_label = pending.get("entity_label", "")
    action_name = pending.get("action", action)
    return (
        f"Action proposed: {action_name} on {entity_label}. "
        f"A confirmation card will appear in the UI — the user must confirm "
        f"before the function runs."
    )


def tool_run_sparql(
    ctx: ToolContext,
    *,
    query: str,
    limit: int | None = None,
    **_kwargs,
) -> str:
    """Execute a read-only SPARQL SELECT (or ASK) query.

    Dormant: excluded from TOOL_DEFINITIONS/TOOL_HANDLERS because it queries
    the warehouse Delta view and cannot see inferred/reasoning triples.  Kept
    for future activation when a raw-SPARQL mode is needed.
    """
    try:
        query = require_read_only_sparql(query)
    except ValidationError as exc:
        return _error(exc.message)

    payload: dict = {"query": query}
    if limit is not None:
        try:
            payload["limit"] = int(limit)
        except (TypeError, ValueError):
            return _error("'limit' must be an integer.")

    try:
        with _client(ctx) as c:
            resp = c.post("/dtwin/execute", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return _error(
            f"SPARQL execute failed ({exc.response.status_code}): "
            f"{exc.response.text[:300]}"
        )
    except Exception as exc:
        return _error(f"SPARQL execute error: {exc}")

    if not data.get("success"):
        return data.get("message") or "SPARQL query failed."

    columns = data.get("columns") or data.get("headers") or []
    rows = data.get("rows") or data.get("data") or []

    if not columns and rows and isinstance(rows[0], dict):
        columns = list(rows[0].keys())
        rows = [[r.get(c) for c in columns] for r in rows]

    header = (
        f"SPARQL Result -- {ctx.dtwin_domain_name}\n"
        + ("=" * 50)
        + f"\nRows: {len(rows)}\n"
    )
    return header + format_sparql_rows(columns, rows)


# =====================================================
# Tool schema definitions (OpenAI chat-completions format)
# =====================================================


_LIST_ENTITY_TYPES_DEF = {
    "type": "function",
    "function": {
        "name": "list_entity_types",
        "description": (
            "List all entity types (rdf:type) with instance counts and overall "
            "statistics of the selected domain's knowledge graph. The schema "
            "digest in the system prompt already names every type; call this "
            "only when you need instance counts or graph statistics."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_DESCRIBE_ENTITY_DEF = {
    "type": "function",
    "function": {
        "name": "describe_entity",
        "description": (
            "Profile of entities matching a text and/or type over the raw "
            "triple store (attributes + direct relationships, incl. inferred "
            "triples). For 'what did X buy / X's history' or filtered lists, "
            "use query_graphql with filters instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Text to search in labels / names / URIs (e.g. 'Jacob Martinez', 'CUST00094').",
                },
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Entity type local name (case-insensitive) e.g. "
                        "'Customer'. Omit it when unsure of the type."
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "Relationship traversal depth (default 1; keep 1 -- "
                        "higher depths explode through hub entities)."
                    ),
                },
            },
        },
    },
}

_GET_STATUS_DEF = {
    "type": "function",
    "function": {
        "name": "get_status",
        "description": "Return the triple-store status (view, graph, row count) for the selected domain.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_GET_GRAPHQL_SCHEMA_DEF = {
    "type": "function",
    "function": {
        "name": "get_graphql_schema",
        "description": (
            "Return the full auto-generated GraphQL schema (SDL) for the "
            "selected domain. Only needed when the schema digest in the system "
            "prompt lacks a type or field you need."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

_QUERY_GRAPHQL_DEF = {
    "type": "function",
    "function": {
        "name": "query_graphql",
        "description": (
            "Execute a GraphQL query against the selected domain. Fast path "
            "for entity-centric, filtered and count questions: list fields "
            "accept filters:[{field, value}] (AND), <list>Count(filters, "
            "distinctBy) returns exact counts, and inverse fields "
            "<pluralOfSource>Via<Property> walk relationships backwards. "
            "Field names are in the schema digest."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "GraphQL query string, e.g. '{ orders(filters:[{field:"
                        "\"placedBy\", value:\"CUST001\"}]) { id product { label } } "
                        "ordersCount(filters:[{field:\"placedBy\", value:"
                        "\"CUST001\"}]) }'."
                    ),
                },
                "variables": {
                    "type": "string",
                    "description": "Optional JSON string of query variables.",
                },
            },
            "required": ["query"],
        },
    },
}

_SELECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "description": "Root list alias in the query."},
        "key": {
            "type": "string",
            "description": "Dotted path of the entity to compare, e.g. 'soldTo.id' or 'id'.",
        },
        "where": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Row conditions (AND) applied before taking keys, same syntax as "
                "the group 'where', e.g. ['product.category.label=TV']."
            ),
        },
        "having": {
            "type": "string",
            "description": (
                "Keep keys whose own rows satisfy an aggregate, e.g. "
                "'sum(usageMinutes)>=300', 'count>=2', 'count_distinct(channel.id)>=3'."
            ),
        },
    },
    "required": ["key"],
}

_ANALYZE_GRAPHQL_DEF = {
    "type": "function",
    "function": {
        "name": "analyze_graphql",
        "description": (
            "Exact aggregation over a GraphQL query, computed server-side. "
            "Every root list is paged to the end (no sampling), then: "
            "operation='group' ranks groups by count / count_distinct / sum / "
            "avg / min / max (ties share a rank); operation='intersect' / "
            "'difference' (first set minus the others) / 'union' compares "
            "entity key sets across lists. Use it for rankings, top-N, "
            "totals, distinct counts per group and 'A and B' / 'A but not B' "
            "questions instead of counting rows yourself. Put simple "
            "conditions in GraphQL filters:[...]; use 'where' for nested "
            "conditions (e.g. 'product.category.label=TV')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "GraphQL query whose aliased root lists select only "
                        "the fields the analysis needs, e.g. '{ s: sales("
                        "filters:[{field:\"isReturned\", value:\"Y\"}]) "
                        "{ channel { id label } } }'. No limit needed."
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": ["group", *SET_OPERATIONS],
                },
                "source": {
                    "type": "string",
                    "description": "group: root list alias to group (optional when there is one).",
                },
                "by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "group: dotted group-key path(s), e.g. ['channel'] or ['soldTo','product.label'].",
                },
                "metric": {
                    "type": "string",
                    "enum": list(METRICS),
                    "description": "group: default count (rows per group).",
                },
                "field": {
                    "type": "string",
                    "description": "group: dotted path the metric reads, e.g. 'grossKrw' or 'channel.id'.",
                },
                "where": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "group: row conditions (AND): 'path=value', 'path!=value', "
                        "'path~contains', 'path>value' (numbers or ISO dates); "
                        "'path>@otherPath' compares two fields of the same row."
                    ),
                },
                "restrict": {
                    "type": "object",
                    "description": (
                        "group: keep rows whose entity at 'key' is in (or with "
                        "exclude:true, not in) the key set 'in' from another list, "
                        "e.g. {key:'soldTo.id', in:{source:'churned', key:'heldBy.id'}}."
                    ),
                    "properties": {
                        "key": {"type": "string"},
                        "in": _SELECTION_SCHEMA,
                        "exclude": {"type": "boolean"},
                        "compare": {
                            "type": "string",
                            "description": (
                                "Cross-record condition '<thisRowPath><op><otherRowPath>' "
                                "between a group row and a matched 'in' row with the same "
                                "key, e.g. 'saleDate>reviewDate' (bought after reviewing)."
                            ),
                        },
                    },
                    "required": ["key", "in"],
                },
                "sets": {
                    "type": "array",
                    "items": _SELECTION_SCHEMA,
                    "description": "intersect / difference / union: two or more key sets.",
                },
                "top": {
                    "type": "integer",
                    "description": f"group: rows to show (default {DEFAULT_TOP}, max {MAX_TOP}).",
                },
                "order": {"type": "string", "enum": ["desc", "asc"]},
                "variables": {"type": "string", "description": "Optional JSON string of query variables."},
            },
            "required": ["query", "operation"],
        },
    },
}

_GET_ENTITY_CONTEXT_DEF = {
    "type": "function",
    "function": {
        "name": "get_entity_context",
        "description": (
            "Return complete context for an entity node: linked dataset rows "
            "and/or cross-domain bridge entities. The class must have "
            "dataset / bridges configured in the ontology. Use the entity "
            "URI from describe_entity output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_uri": {
                    "type": "string",
                    "description": "Full URI of the entity (e.g. from describe_entity).",
                },
                "fetch_dataset_rows": {
                    "type": "boolean",
                    "description": "If true, query the linked UC table/view for rows.",
                },
                "dataset_row_limit": {
                    "type": "integer",
                    "description": "Max rows to return (1-20, default 5).",
                },
                "follow_bridges": {
                    "type": "boolean",
                    "description": "If true, load entities from bridge target domains.",
                },
            },
            "required": ["entity_uri"],
        },
    },
}

_REQUEST_ENTITY_ACTION_DEF = {
    "type": "function",
    "function": {
        "name": "request_entity_action",
        "description": (
            "Propose running a Unity Catalog function action configured on "
            "an entity's class. This only validates the entity/action and "
            "stages a confirmation card in the UI — it never invokes the "
            "function directly. The user must confirm before it runs. "
            "Discover available actions with describe_entity or "
            "get_entity_context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_uri": {
                    "type": "string",
                    "description": "Full URI of the entity (e.g. from describe_entity).",
                },
                "action": {
                    "type": "string",
                    "description": "Fully qualified function name (catalog.schema.function).",
                },
            },
            "required": ["entity_uri", "action"],
        },
    },
}

TOOL_DEFINITIONS: List[dict] = [
    _LIST_ENTITY_TYPES_DEF,
    _DESCRIBE_ENTITY_DEF,
    _GET_STATUS_DEF,
    _GET_GRAPHQL_SCHEMA_DEF,
    _QUERY_GRAPHQL_DEF,
    _ANALYZE_GRAPHQL_DEF,
    _GET_ENTITY_CONTEXT_DEF,
    _REQUEST_ENTITY_ACTION_DEF,
]

TOOL_HANDLERS: Dict[str, Callable] = {
    "list_entity_types": tool_list_entity_types,
    "describe_entity": tool_describe_entity,
    "get_status": tool_get_status,
    "get_graphql_schema": tool_get_graphql_schema,
    "query_graphql": tool_query_graphql,
    "analyze_graphql": tool_analyze_graphql,
    "get_entity_context": tool_get_entity_context,
    "request_entity_action": tool_request_entity_action,
}
