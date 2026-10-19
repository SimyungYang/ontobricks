"""
Graph Chat Agent engine.

Runs one turn of a multi-tool conversation: the LLM can iteratively call
the knowledge-graph tools defined in :mod:`agents.agent_dtwin_chat.tools`
until it produces a final natural-language answer.

The engine shares the ``call_serving_endpoint`` / ``dispatch_tool`` /
``accumulate_usage`` helpers defined in :mod:`agents.engine_base`, so it
behaves identically to the Ontology Assistant (same retry logic,
tracing, OpenAI-compatible schema).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from back.core.logging import get_logger
from agents.agent_dtwin_chat.tools import TOOL_DEFINITIONS, TOOL_HANDLERS
from agents.tools.context import ToolContext
from agents.engine_base import (
    AgentStep,
    accumulate_usage,
    call_serving_endpoint,
    dispatch_tool,
)
from agents.tracing import content_block_types, trace_agent

logger = get_logger(__name__)

MAX_ITERATIONS = 12
LLM_TIMEOUT = 120
# Output-token budget per chat turn. Reasoning-capable models spend part of
# the budget on hidden reasoning, so 2048 left Claude Sonnet/Opus 4.6+ with no
# text and the UI showed the "couldn't display" fallback. 8192 is the platform
# ceiling; override with ONTOBRICKS_CHAT_MAX_TOKENS.
CHAT_MAX_TOKENS = int(os.getenv("ONTOBRICKS_CHAT_MAX_TOKENS", "8192"))

_TRACE_NAME = "dtwin_chat"
# Every earlier tool result is re-sent on each iteration (median 132k prompt
# tokens per question in the baseline). Keep the newest results in full and
# shrink older ones to a head + note before each LLM call.
KEEP_FULL_TOOL_RESULTS = 2
COMPACT_TOOL_HEAD_CHARS = 1500
_COMPACTED_NOTE = (
    "\n[compacted: older tool result, {total:,} chars total, first {head:,} "
    "shown. Re-run a narrower query if you need the rest.]"
)
_UNREADABLE_REPLY = "I couldn't display that answer. Please try again."
# Compact only when it saves more than this many characters beyond the head
# and the note, so results just over the head length are left untouched.
_MIN_COMPACTION_SAVINGS = 20


def _extract_reply_text(content: object) -> list[str]:
    """Recursively extract user-visible text from model content blocks."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            parts.extend(_extract_reply_text(item))
        return parts
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type and block_type not in ("text", "output_text"):
            return []
        for key in ("text", "content", "value"):
            if key in content:
                return _extract_reply_text(content[key])
    return []


# ``[text](/resolve?uri=<value>)`` whose value has no scheme (a bare id such
# as ``C042``) cannot be resolved by the UI; keep the text, drop the link.
_UNRESOLVABLE_LINK = re.compile(r"\[([^\]]+)\]\(/resolve\?uri=(?![A-Za-z][A-Za-z0-9+.-]*:)[^)]*\)")


def drop_unresolvable_links(text: str) -> str:
    """Replace knowledge-graph links that carry a bare id with their text."""
    return _UNRESOLVABLE_LINK.sub(r"\1", text or "")


def normalize_reply_content(content: object) -> str:
    """Return readable Markdown for a model response content payload."""
    if isinstance(content, str) and content.strip():
        return content
    parts = [part.strip() for part in _extract_reply_text(content) if part.strip()]
    return "\n\n".join(parts) if parts else _UNREADABLE_REPLY


def compact_tool_messages(
    messages: List[dict],
    keep_full: int = KEEP_FULL_TOOL_RESULTS,
    head_chars: int = COMPACT_TOOL_HEAD_CHARS,
) -> List[dict]:
    """Return a copy of *messages* with older tool results shortened.

    Tool results answering the latest assistant turn, plus the newest
    *keep_full* tool results overall, stay intact; earlier ones keep only
    their first *head_chars* characters and a ``[compacted ...]`` note.
    Message order, roles and ``tool_call_id`` values are preserved, so every
    assistant ``tool_calls`` entry still pairs with its ``tool`` message.
    """
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if not tool_idx:
        return list(messages)
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    keep = {i for i in tool_idx if i > last_assistant}
    # Guard keep_full == 0: ``tool_idx[-0:]`` would keep every result.
    keep.update(tool_idx[-keep_full:] if keep_full > 0 else [])

    out: List[dict] = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if (
            i in keep
            or msg.get("role") != "tool"
            or not isinstance(content, str)
            or len(content) <= head_chars + len(_COMPACTED_NOTE) + _MIN_COMPACTION_SAVINGS
        ):
            out.append(msg)
            continue
        compacted = dict(msg)
        compacted["content"] = content[:head_chars] + _COMPACTED_NOTE.format(
            total=len(content), head=head_chars
        )
        out.append(compacted)
    return out


@dataclass
class AgentResult:
    """Outcome of a single Graph Chat turn."""

    success: bool
    reply: str = ""
    steps: List[AgentStep] = field(default_factory=list)
    iterations: int = 0
    error: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    pending_action: Optional[dict] = None


def _finalize_result(result: AgentResult, ctx: ToolContext) -> AgentResult:
    """Copy session-side state onto the result before any return path."""
    result.pending_action = ctx.pending_action
    return result


_SCHEMA_DIGEST_TOKEN = "<<SCHEMA_DIGEST>>"
_DESCRIBE_DEPTH_TOKEN = "<<DESCRIBE_DEPTH>>"
DEFAULT_DESCRIBE_DEPTH = 1
MAX_DESCRIBE_DEPTH = 5
_NO_DIGEST_TEXT = (
    "(not available for this turn -- call get_graphql_schema once if you "
    "need type / field names)"
)

SYSTEM_PROMPT = (
    """\
You are the Graph Chat assistant for OntoBricks. You help the user
explore a Knowledge Graph with natural-language questions.

CONTEXT
The user has already selected a domain (knowledge graph). You do NOT
need to list or select domains. Every tool operates on the active
session's domain automatically.

SCHEMA DIGEST (current domain -- use these exact type / field names)
"""
    + _SCHEMA_DIGEST_TOKEN
    + """
The digest replaces list_entity_types / get_graphql_schema for planning.
Call those only when a type or field you need is missing from it.

TOOLS
  - query_graphql(query)  -- typed reads through the ontology schema.
      FAST PATH for entity-centric, filtered-list and count questions.
      * <list>(limit, offset, search, filters:[{field, value}])  (AND)
      * <list>Count(search, filters, distinctBy)  -- exact counts;
        distinctBy:"<relField>" counts distinct targets (e.g. buyers).
      * <single>(id)
      * Forward relationship fields, and inverse fields named
        <pluralOfSource>Via<Property> (e.g. ordersViaPlacedBy on
        Customer = the Orders whose placedBy points at that Customer).
      * filters on a relationship field match when the target's label
        contains the value (case-insensitive) or the target id equals it;
        on a data field they are case-insensitive "contains"; field "id"
        matches the entity id exactly.
      * Only selected fields are loaded -- select just what you need.
      * Only ontology predicates are visible (no inferred-only triples).
  - analyze_graphql(query, operation, ...)  -- EXACT aggregation, computed
      server-side over every row of the query's root lists (paged to the
      end; no sampling). Returns only the result table.
      * operation:"group", by:[path..], metric: count | count_distinct |
        sum | avg | min | max, field:<path>, where:[cond..], top:N
      * operation:"intersect" | "difference" (first minus the rest) |
        "union", sets:[{source:<alias>, key:<path>, where:[cond..],
        having:"sum(usageMinutes)>=300"}, ..]  (having = per-key aggregate)
      * restrict:{key:<path>, in:{source, key, where}, exclude?, compare?}
        keeps group rows whose entity is (not) in another list's key set;
        compare:"saleDate>reviewDate" also requires a matched row of that
        list with this row's saleDate after its reviewDate.
      * Paths are dotted through nested fields: "soldTo.id",
        "product.category.label", "id" (the root entity itself).
        Conditions: "path=v", "path!=v", "path~contains", "path>v" (numbers
        or ISO dates), "path>@otherPath" (two fields of the same row, e.g.
        "actualGiDate>@plannedGiDate"). Put simple conditions in GraphQL
        filters instead.
  - describe_entity(search, entity_type?, depth?)
      Raw-triple profile of ONE entity, incl. inferred triples. Keep the
      default ``describe_entity`` (depth="""
    + _DESCRIBE_DEPTH_TOKEN
    + """). If you are not sure of the
      type, OMIT entity_type -- never guess types one by one.
  - list_entity_types / get_status / get_graphql_schema -- only when the
      digest above is missing or lacks what you need.

  CONTEXT (ontology design)
  - get_entity_context(entity_uri, fetch_dataset_rows?, follow_bridges?)
      Linked Dataset rows and/or cross-domain Bridge entities.
  - request_entity_action(entity_uri, action)
      Propose a class Action (UC function). Does NOT execute it.
      The UI will ask the user to Confirm or Cancel.

WORKFLOW -- pick the route, then answer in as few calls as possible
  A. "What did X buy / order / where / X's history": ONE query_graphql
     call on the FACT / EVENT type, filtered on the relationship that
     points at X, with the nested fields the user asked about, e.g.
       { orders(filters:[{field:"placedBy", value:"<name or id>"}],
                limit:200) {
           id orderDate placedBy { id label } product { label }
           channel { label } } }
     (or the inverse field from X's type). Do NOT call describe_entity
     or get_entity_context first.
  B. Filtered lists ("customers who bought P at S"): filter the fact type
     on every condition and get exact totals in the SAME request:
       { rows: orders(filters:[{field:"store", value:"<S>"},
                               {field:"product", value:"<P>"}], limit:500)
           { id placedBy { id label } product { label } }
         total: ordersCount(filters:[...same...])
         people: ordersCount(filters:[...same...], distinctBy:"placedBy") }
  C. Profile of one entity (its attributes / everything it links to):
     describe_entity(search) once.
  D. Cross-record aggregation (rankings, top-N, sums, averages, ratios,
     "who has the most", distinct counts per group): ONE analyze_graphql
     call. If the digest has a pre-aggregated type (*Summary, *Performance,
     *Target) whose stored totals answer the question at the asked grain,
     read it with filters instead -- but never add up rows that sit at
     different grains / dimensions of such a type.
       ranking:  analyze_graphql(query:'{ s: sales(filters:[{field:
                   "isReturned", value:"Y"}]) { channel { id label } } }',
                 operation:"group", by:["channel"])
       sum:      ... by:["branch"], metric:"sum", field:"grossKrw"
       distinct: ... by:["soldTo"], metric:"count_distinct", field:"channel.id"
       largest single record: ... by:["id"], metric:"max",
                 field:"expectedAmountKrw", top:5
  E. Cross conditions ("bought both A and B", "has X but no Y", "did A
     and also B"): one analyze_graphql call with an aliased list per
     condition and a set operation on the shared entity key, e.g.
       query:'{ a: sales { soldTo { id label } product { category { label } } }
               b: subscriptions { heldBy { id label } } }'
       operation:"intersect", sets:[
         {source:"a", key:"soldTo.id", where:["product.category.label=TV"]},
         {source:"b", key:"heldBy.id"}]
     "What did the people in set X buy / own": group the purchases /
     devices with restrict:{key:"<customer path>", in:<set X>} -- walk
     from the customer to their purchases or devices; do not reuse the
     product or device recorded on the event that defined set X.
     Conditions across two records of the same entity ("bought AFTER
     their negative review"): group the later records with
       restrict:{key:"soldTo.id", in:{source:"r", key:"writtenBy.id",
                 where:["sentiment=부정"]}, compare:"saleDate>reviewDate"}
     -- the "groups" count is the number of distinct customers.
  F. Data the schema does not have (no such type or field in the digest):
     say first that it is not in the graph. If you then show a nearby
     field, name it, say how it differs and how many records it covers.
  G. Missing links are UNKNOWN, not zero: records with no relationship to
     the field you need (e.g. shipments not linked to any sale) have an
     unknown value. Never count them as 0 / "no" / "none"; exclude them and
     say how many were excluded.
  H. Several entities may share a name: group results by entity id
     (one section or column per id) and tell the user the name is shared.

RULES
  * Use the exact entity / predicate names returned by the tools.
  * If a tool returns an error, read it (it lists valid fields) and fix
    the query instead of switching to slower tools.
  * Do NOT try to mutate data.
  * Never claim an Action ran unless a later tool/UI result says it completed.
  * Never end a turn by announcing a further lookup ("let me query ...").
    Either call the tool now or give the final answer with what you have.
  * Only request actions that appeared in tool output (allow-listed fullName).
  * Never invent URIs or counts. Counts come from <list>Count,
    analyze_graphql, a "distinct" summary line, or a full (non-truncated)
    list.
  * A list that returns exactly `limit` rows, or a "[...truncated" note,
    is PARTIAL: say so and quote the Count total instead.

ANSWER STYLE
  * Concise and relevant. Show at most ~10 table rows, then the totals /
    counts (e.g. "110 sales, 102 customers -- first 10 shown").
  * Say explicitly when results are partial or truncated.
  * Include URIs only when the user asks for them.
  * MONEY: KRW amount fields come with a pre-computed 억 value in
    parentheses, e.g. ``grossKrw=1409235 (0.014억, 140.9만원)``
    (1억 = 100,000,000 KRW). Copy that value; never convert units yourself.
  * Never add up, average, rank or intersect rows yourself. Use Count
    fields or analyze_graphql and quote their numbers as-is; show ties as
    ties.
  * Revenue terms: 매출 / 총매출 / 매출액 / 구매 금액 (no qualifier) =
    grossKrw; 순매출 = netKrw (returns included unless asked);
    수수료 = commissionKrw; 인식매출 = recognizedNetKrw (when present).
  * Money fields: prefer the *Krw field (e.g. netValueKrw) over *Local
    (local currency) so amounts are comparable and summable; use *Local
    only when the user asks for local currency.

FORMATTING
  * Your replies are rendered as Markdown. Use tables for list-style
    results, bullet lists for short summaries, and ``code`` for
    GraphQL snippets or raw identifiers.

LINKS TO THE KNOWLEDGE GRAPH (VERY IMPORTANT)
  Whenever you mention a concrete entity instance whose ``uri`` appeared
  in a tool response, emit it as a Markdown link so the user can jump to
  the knowledge-graph visualization:

      [<display-name>](/resolve?uri=<full-URI>)

  Rules:
    * Use the entity's human-readable name as the link text (its
      ``rdfs:label`` / ``name`` attribute if available, otherwise the
      local name after the last ``/`` or ``#``).
    * The ``<full-URI>`` inside the parentheses MUST be the exact URI
      returned by the tool — do not shorten, prefix, or URL-encode it
      yourself; the app handles encoding.
    * Apply this in prose and inside Markdown tables (put the link in
      the relevant cell). Select ``uri`` in GraphQL only for the
      entities you will actually show.
    * Do NOT link entity *types* / classes (e.g. ``Customer``,
      ``Order``) — only concrete instances.
    * Never invent URIs: only link URIs that appeared in a tool
      response. Never build one from an id, a base URI or an example; if
      you did not select ``uri`` for an entity, show its name and id as
      plain text instead of a link.
"""
)


def build_system_prompt(
    describe_depth: int = DEFAULT_DESCRIBE_DEPTH, schema_digest: str = ""
) -> str:
    """Return the per-turn system prompt with depth and schema digest injected."""
    depth = max(1, min(int(describe_depth or DEFAULT_DESCRIBE_DEPTH), MAX_DESCRIBE_DEPTH))
    digest = (schema_digest or "").strip() or _NO_DIGEST_TEXT
    return SYSTEM_PROMPT.replace(_DESCRIBE_DEPTH_TOKEN, str(depth)).replace(
        _SCHEMA_DIGEST_TOKEN, digest
    )


@trace_agent(name="dtwin_chat")
def _run_tool_calls(
    tool_calls: List[dict],
    ctx: ToolContext,
    result: AgentResult,
    messages: List[dict],
    on_event: Optional[Callable[["AgentStep"], None]],
    iteration: int,
) -> None:
    """Dispatch one assistant turn's *tool_calls* and append their results.

    Records a ``tool_call`` and a ``tool_result`` step per call on *result*
    and appends the matching ``tool`` message to *messages*.
    """
    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name", "")
        tool_id = tc.get("id", "")
        raw_args = func.get("arguments", "{}")

        try:
            arguments = (
                json.loads(raw_args)
                if isinstance(raw_args, str)
                else raw_args
            )
        except json.JSONDecodeError:
            arguments = {}

        logger.info(
            "dtwin_chat: iteration %d -- tool_call '%s'",
            iteration + 1,
            tool_name,
        )

        call_step = AgentStep(
            step_type="tool_call",
            content=json.dumps(arguments, default=str),
            tool_name=tool_name,
        )
        result.steps.append(call_step)
        if on_event:
            on_event(call_step)

        tool_t0 = time.time()
        tool_result = dispatch_tool(
            TOOL_HANDLERS,
            ctx,
            tool_name,
            arguments,
            trace_name=_TRACE_NAME,
        )
        tool_elapsed = int((time.time() - tool_t0) * 1000)

        result_step = AgentStep(
            step_type="tool_result",
            content=tool_result[:500],
            tool_name=tool_name,
            duration_ms=tool_elapsed,
        )
        result.steps.append(result_step)
        if on_event:
            on_event(result_step)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_id,
                "content": tool_result,
            }
        )


def run_agent(
    host: str,
    token: str,
    endpoint_name: str,
    base_url: str,
    domain_name: str,
    registry_params: dict,
    session_cookies: dict,
    user_message: str,
    conversation_history: Optional[List[dict]] = None,
    session_headers: Optional[dict] = None,
    on_step: Optional[Callable[[str], None]] = None,
    on_event: Optional[Callable[["AgentStep"], None]] = None,
    describe_depth: int = 1,
    schema_digest: str = "",
) -> AgentResult:
    """Run one turn of the Graph Chat agent.

    Args:
        host, token, endpoint_name: Databricks serving-endpoint target
            used to issue LLM chat completions.
        base_url: Loopback OntoBricks URL, e.g. ``http://localhost:8000``.
        domain_name: Currently selected domain (from session).
        registry_params: Registry query params injected into every call
            (``registry_catalog``, ``registry_schema``, ``registry_volume``).
        session_cookies: Cookies from the user's request; forwarded to
            the internal ``/dtwin/...`` routes so they resolve the same
            session.
        session_headers: Databricks-Apps identity / CSRF headers
            (``X-Forwarded-Email``, ``X-Forwarded-Access-Token``, …)
            forwarded to the internal routes so
            :class:`PermissionMiddleware` authenticates the loopback
            call as the same user instead of 302-redirecting it to
            ``/access-denied``.
        user_message: The user's new natural-language message.
        conversation_history: Prior ``{role, content}`` messages.
        on_step: Optional progress callback (unused but kept for parity).
        schema_digest: Compact GraphQL schema digest of the active domain
            (see ``SchemaMetadata.digest``), injected into the system prompt
            so the agent can skip schema discovery calls.
    """
    logger.info(
        "===== DTWIN CHAT START ===== endpoint=%s, domain=%s, base_url=%s",
        endpoint_name,
        domain_name,
        base_url,
    )

    ctx = ToolContext(
        host=host.rstrip("/") if host else "",
        token=token or "",
        dtwin_base_url=base_url,
        dtwin_session_cookies=session_cookies or {},
        dtwin_session_headers=session_headers or {},
        dtwin_registry_params=registry_params or {},
        dtwin_domain_name=domain_name or "",
    )

    result = AgentResult(success=False)

    system_prompt = build_system_prompt(describe_depth, schema_digest)

    messages = [{"role": "system", "content": system_prompt}]
    if conversation_history:
        for msg in conversation_history:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_message})

    for iteration in range(MAX_ITERATIONS):
        result.iterations = iteration + 1
        is_last = iteration == MAX_ITERATIONS - 1
        send_tools = TOOL_DEFINITIONS if not is_last else None

        if on_step:
            on_step(f"Iteration {iteration + 1}...")

        try:
            llm_response = call_serving_endpoint(
                host,
                token,
                endpoint_name,
                compact_tool_messages(messages),
                tools=send_tools,
                max_tokens=CHAT_MAX_TOKENS,
                temperature=0.1,
                timeout=LLM_TIMEOUT,
                trace_name=_TRACE_NAME,
            )
        except Exception as exc:
            error_msg = f"LLM request failed: {exc}"
            logger.error(
                "dtwin_chat: %s at iteration %d", error_msg, iteration + 1
            )
            result.error = error_msg
            return _finalize_result(result, ctx)

        accumulate_usage(result.usage, llm_response.get("usage", {}))

        choices = llm_response.get("choices", [])
        if not choices:
            logger.warning(
                "dtwin_chat: empty choices in LLM response at iteration %d",
                iteration + 1,
            )
            result.error = "No choices in LLM response"
            return _finalize_result(result, ctx)

        message = choices[0].get("message", {})
        content = message.get("content", "") or ""
        tool_calls = message.get("tool_calls")
        finish_reason = choices[0].get("finish_reason")
        logger.info(
            "dtwin_chat: iteration %d -- finish_reason=%s, completion_tokens=%s, "
            "content_blocks=%s, tool_calls=%d",
            iteration + 1,
            finish_reason,
            (llm_response.get("usage") or {}).get("completion_tokens"),
            content_block_types(content),
            len(tool_calls or []),
        )

        if tool_calls:
            messages.append(message)

            _run_tool_calls(tool_calls, ctx, result, messages, on_event, iteration)
        else:
            raw_blocks = content_block_types(content)
            content = drop_unresolvable_links(normalize_reply_content(content))
            if content == _UNREADABLE_REPLY:
                logger.warning(
                    "dtwin_chat: final reply had no readable text "
                    "(finish_reason=%s, content_blocks=%s, max_tokens=%d) -- "
                    "raise ONTOBRICKS_CHAT_MAX_TOKENS if finish_reason=length",
                    finish_reason,
                    raw_blocks,
                    CHAT_MAX_TOKENS,
                )
            result.success = True
            result.reply = content
            output_step = AgentStep(step_type="output", content=content[:500])
            result.steps.append(output_step)
            if on_event:
                on_event(output_step)
            logger.info(
                "===== DTWIN CHAT DONE ===== iterations=%d, reply_len=%d",
                result.iterations,
                len(content),
            )
            return _finalize_result(result, ctx)

    result.error = "Max iterations reached"
    result.reply = (
        "I ran out of steps before I could answer. "
        "Could you simplify or narrow down your question?"
    )
    return _finalize_result(result, ctx)
