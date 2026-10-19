"""Server-side aggregation behind the Graph Chat ``analyze_graphql`` tool.

The chat LLM used to page through hundreds of rows and count, intersect or
sum them in its own context, which produced plausible but wrong numbers
(baseline: 0/12 on cross-condition questions). This module does that work
deterministically instead:

1. ``fetch_all_rows`` runs the GraphQL query and pages every root list to
   completion (``limit`` / ``offset``), so results are never a sample.
2. ``group_rows`` ranks groups by count / count_distinct / sum / avg /
   min / max over dotted field paths.
3. ``set_operation`` intersects / subtracts / unions key sets taken from
   one or more lists (e.g. buyers of TVs AND buyers of refrigerators).

Only the small result table is returned to the LLM.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import math
import operator
import re
import statistics
from collections import Counter
from typing import Callable, Dict, List, Optional, Tuple

from graphql import GraphQLError, parse, print_ast
from graphql.language import (
    ArgumentNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    IntValueNode,
    NameNode,
    OperationDefinitionNode,
    SelectionSetNode,
    VariableNode,
    visit,
    Visitor,
)

from agents.tools.graph_formatting import is_krw_field, krw_eok_text
from agents.agent_dtwin_chat.AnalysisError import AnalysisError
from agents.agent_dtwin_chat.EntityRef import EntityRef
from back.core.graphql.constants import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT

Executor = Callable[[str, Optional[dict]], dict]

PAGE_SIZE = MAX_LIST_LIMIT
MAX_ROWS_PER_LIST = 20_000
DEFAULT_TOP = 20
DEFAULT_METRIC = "count"
DEFAULT_ORDER = "desc"
MAX_TOP = 100
MAX_MEMBERS = 20
MAX_HINT_VALUES = 12
METRICS = ("count", "count_distinct", "sum", "avg", "min", "max")
SET_OPERATIONS = ("intersect", "difference", "union")

# Per-list fetch status returned by ``fetch_all_rows``.
LIST_COMPLETE = "complete"
LIST_ROW_CAP = "row_cap"
LIST_PAGE_ERROR = "error"
_PARTIAL_NOTES = {
    LIST_ROW_CAP: " (PARTIAL: row cap reached)",
    LIST_PAGE_ERROR: " (PARTIAL: a page failed; see GraphQL warnings)",
}

_NONE_KEY = "(none)"
_CONDITION = re.compile(r"^\s*([\w.]+)\s*(!=|>=|<=|=|~|>|<)\s*(.*?)\s*$")
_OPS: Dict[str, Callable[[object, object], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "=": operator.eq,
    "!=": operator.ne,
}
_AGGREGATES: Dict[str, Callable[[List[float]], float]] = {
    "sum": sum,
    "avg": statistics.fmean,
    "min": min,
    "max": max,
}


_NONE_ENTITY = EntityRef(_NONE_KEY, _NONE_KEY, "")


# ── fetching ─────────────────────────────────────────────────────────────


class _VariableCollector(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def enter_variable(self, node: VariableNode, *_args) -> None:
        self.names.add(node.name.value)


def _int_arg(node: Optional[FieldNode], name: str, variables: dict, default: int) -> int:
    for arg in (node.arguments if node else ()) or ():
        if arg.name.value != name:
            continue
        if isinstance(arg.value, IntValueNode):
            return int(arg.value.value)
        if isinstance(arg.value, VariableNode):
            value = variables.get(arg.value.name.value)
            if isinstance(value, int):
                return value
    return default


def _page_query(
    op: OperationDefinitionNode,
    field: FieldNode,
    fragments: List[FragmentDefinitionNode],
    *,
    limit: int,
    offset: int,
) -> str:
    """Return a query containing only *field* with ``limit`` / ``offset`` set."""
    page_field = copy.deepcopy(field)
    args = [a for a in (page_field.arguments or ()) if a.name.value not in ("limit", "offset")]
    for name, value in (("limit", limit), ("offset", offset)):
        args.append(ArgumentNode(name=NameNode(value=name), value=IntValueNode(value=str(value))))
    page_field.arguments = tuple(args)

    used = _VariableCollector()
    visit(page_field, used)
    for frag in fragments:
        visit(frag, used)
    page_op = copy.deepcopy(op)
    page_op.selection_set = SelectionSetNode(selections=(page_field,))
    page_op.variable_definitions = tuple(
        v for v in (op.variable_definitions or ()) if v.variable.name.value in used.names
    )
    return print_ast(DocumentNode(definitions=(page_op, *fragments)))


def _error_messages(response: dict) -> List[str]:
    return [str(e.get("message", e)) for e in (response.get("errors") or [])]


def _page_rest(
    rows: list,
    *,
    alias: str,
    op: OperationDefinitionNode,
    node: FieldNode,
    fragments: List[FragmentDefinitionNode],
    variables: Optional[dict],
    execute: Executor,
    errors: List[str],
) -> str:
    """Append the remaining pages of root list *alias* to *rows*; return its status."""
    arg_values = variables or {}
    # The server clamps ``limit`` to MAX_LIST_LIMIT, so a first page shorter
    # than the requested limit is only final below that clamp.
    limit = max(1, min(_int_arg(node, "limit", arg_values, DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    if len(rows) < limit:
        return LIST_COMPLETE
    offset = _int_arg(node, "offset", arg_values, 0) + len(rows)
    while len(rows) < MAX_ROWS_PER_LIST:
        page = execute(_page_query(op, node, fragments, limit=PAGE_SIZE, offset=offset), variables)
        if page.get("errors"):
            errors.extend(_error_messages(page))
            return LIST_PAGE_ERROR
        batch = (page.get("data") or {}).get(alias) or []
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < PAGE_SIZE:
            return LIST_COMPLETE
    return LIST_ROW_CAP


def fetch_all_rows(
    query: str, variables: Optional[dict], execute: Executor
) -> Tuple[Dict[str, list], Dict[str, str], List[str]]:
    """Run *query* and page every root list to completion.

    Returns ``(lists, status, errors)``: rows per root alias, the fetch status
    of each list (``LIST_COMPLETE``, ``LIST_ROW_CAP`` past
    ``MAX_ROWS_PER_LIST``, or ``LIST_PAGE_ERROR`` when a later page failed),
    and GraphQL error messages.
    """
    try:
        doc = parse(query)
    except GraphQLError as exc:
        raise AnalysisError(f"GraphQL syntax error: {exc}") from exc
    ops = [d for d in doc.definitions if isinstance(d, OperationDefinitionNode)]
    if len(ops) != 1 or ops[0].operation.value != "query":
        raise AnalysisError("Send exactly one GraphQL query operation.")
    op = ops[0]
    fragments = [d for d in doc.definitions if isinstance(d, FragmentDefinitionNode)]
    fields = {
        (f.alias or f.name).value: f
        for f in op.selection_set.selections
        if isinstance(f, FieldNode)
    }

    data = execute(query, variables)
    errors = _error_messages(data)
    lists: Dict[str, list] = {}
    status: Dict[str, str] = {}
    for alias, value in (data.get("data") or {}).items():
        if not isinstance(value, list):
            continue
        rows = list(value)
        node = fields.get(alias)
        status[alias] = (
            LIST_COMPLETE
            if node is None
            else _page_rest(
                rows,
                alias=alias,
                op=op,
                node=node,
                fragments=fragments,
                variables=variables,
                execute=execute,
                errors=errors,
            )
        )
        lists[alias] = rows
    return lists, status, errors


# ── path helpers ─────────────────────────────────────────────────────────


def values_at(row: object, path: str) -> list:
    """All values at dotted *path* under *row*; nested lists are flattened."""
    current = [row]
    for part in path.split("."):
        nxt: list = []
        for item in current:
            if not isinstance(item, dict):
                continue
            value = item.get(part)
            if isinstance(value, list):
                nxt.extend(v for v in value if v is not None)
            elif value is not None:
                nxt.append(value)
        current = nxt
    return current


def _entity(value: object) -> EntityRef:
    """:class:`EntityRef` for a nested entity dict or a scalar."""
    if isinstance(value, dict):
        key = str(value.get("id") or value.get("uri") or value.get("label") or "")
        label = str(value.get("label") or "")
        display = f"{label} [{key}]" if label and label != key else key
        return EntityRef(key, display, str(value.get("uri") or ""))
    return EntityRef(str(value), str(value), "")


def entities_at(row: object, path: str) -> List[EntityRef]:
    """Entities at *path*; ``a.b.id`` / ``a.b.uri`` / ``id`` resolve to their parent entity."""
    if path in ("id", "uri"):
        return [_entity(row)] if isinstance(row, dict) else []
    head, _, last = path.rpartition(".")
    if last in ("id", "uri") and head:
        return [_entity(p) for p in values_at(row, head) if isinstance(p, dict)]
    return [_entity(v) for v in values_at(row, path)]


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return None if math.isnan(number) else number


def _row_keys(row: object, path: str) -> set:
    return {e.key for e in entities_at(row, path) if e.key}


def _row_numbers(row: object, path: str) -> List[float]:
    return [n for n in map(_number, values_at(row, path)) if n is not None]


def _match_texts(value: object) -> List[str]:
    """Texts a condition is checked against (label / id / uri for an entity)."""
    if isinstance(value, dict):
        return [str(value.get(k)) for k in ("label", "id", "uri") if value.get(k) is not None]
    return [str(value)]


def _scalar_matches(value: object, op: str, expected: str) -> bool:
    texts = _match_texts(value)
    want = expected.lower()
    if op == "=":
        return any(t.lower() == want for t in texts)
    if op == "!=":
        return not _scalar_matches(value, "=", expected)
    if op == "~":
        return any(want in t.lower() for t in texts)
    if not texts:
        return False
    left, right = _number(texts[0]), _number(expected)
    if left is None or right is None:
        left, right = texts[0], expected  # ISO dates compare as strings
    return _OPS[op](left, right)


def parse_condition(text: str) -> Tuple[str, str, str]:
    match = _CONDITION.match(str(text or ""))
    if not match:
        raise AnalysisError(
            f"Bad where condition {text!r}; use 'path=value', 'path!=value', "
            "'path~contains', or 'path>value' / '<' / '>=' / '<='."
        )
    return match.group(1), match.group(2), match.group(3)


def _condition_targets(row: object, expected: str) -> List[str]:
    """Values a condition compares with; ``@other.path`` reads another field of *row*."""
    if expected.startswith("@"):
        return [_entity(t).key for t in values_at(row, expected[1:])]
    return [expected]


def row_matches(row: object, conditions: List[Tuple[str, str, str]]) -> bool:
    """AND of *conditions*; a condition holds when any value at its path matches."""
    for path, op, expected in conditions:
        values = values_at(row, path)
        targets = _condition_targets(row, expected)
        if op == "!=":
            # "!=" holds when no value equals any target (also when there is no value).
            if any(_scalar_matches(v, "=", t) for v in values for t in targets):
                return False
        elif not any(_scalar_matches(v, op, t) for v in values for t in targets):
            return False
    return True


_HAVING = re.compile(r"^\s*(count|count_distinct|sum|avg|min|max)\s*(?:\(\s*([\w.]*)\s*\))?\s*(>=|<=|!=|=|>|<)\s*(-?[\d.,]+)\s*$")


def parse_having(text: str) -> Tuple[str, Optional[str], str, float]:
    """``"sum(usageMinutes)>=300"`` -> ``("sum", "usageMinutes", ">=", 300.0)``."""
    match = _HAVING.match(str(text or ""))
    if not match or (match.group(1) != "count" and not match.group(2)):
        raise AnalysisError(
            f"Bad having {text!r}; use e.g. 'count>=2', 'sum(usageMinutes)>=300', "
            "'count_distinct(channel.id)>=3'."
        )
    return match.group(1), match.group(2) or None, match.group(3), float(match.group(4).replace(",", ""))


@dataclasses.dataclass
class _Group:
    """Accumulated rows of one group and the inputs its metric needs."""

    display: Tuple[EntityRef, ...]
    rows: int = 0
    distinct: set = dataclasses.field(default_factory=set)
    nums: List[float] = dataclasses.field(default_factory=list)

    def add(self, row: object, metric: str, field: Optional[str]) -> None:
        self.rows += 1
        if metric == "count_distinct":
            self.distinct.update(_row_keys(row, field))
        elif metric != "count":
            self.nums.extend(_row_numbers(row, field))

    def value(self, metric: str) -> Optional[float]:
        if metric == "count":
            return self.rows
        if metric == "count_distinct":
            return len(self.distinct)
        return _AGGREGATES[metric](self.nums) if self.nums else None

    @property
    def sort_name(self) -> str:
        return self.display[0].display

    @property
    def label(self) -> str:
        return " / ".join(f"{e.display} <{e.uri}>" if e.uri else e.display for e in self.display)


def _metric_value(metric: str, rows: list, field: Optional[str]) -> Optional[float]:
    group = _Group(display=())
    for row in rows:
        group.add(row, metric, field)
    value = group.value(metric)
    return None if value is None else float(value)


# ── selections ───────────────────────────────────────────────────────────


def select_rows(lists: Dict[str, list], source: Optional[str], where: Optional[list]) -> Tuple[str, list]:
    """Rows of root list *source* (the only list when omitted) passing *where*."""
    if not source:
        if len(lists) != 1:
            raise AnalysisError(
                f"Name the root list to analyse ('source'); the query returned {sorted(lists)}."
            )
        source = next(iter(lists))
    if source not in lists:
        raise AnalysisError(f"Unknown source {source!r}; root lists returned: {sorted(lists)}.")
    conditions = [parse_condition(c) for c in (where or [])]
    return source, [r for r in lists[source] if row_matches(r, conditions)]


def values_hint(lists: Dict[str, list], source: Optional[str], where: Optional[list]) -> str:
    """Values present at each *where* path -- shown when the conditions matched nothing."""
    if not where:
        return ""
    rows = lists.get(source) if source else (next(iter(lists.values())) if len(lists) == 1 else None)
    parts = []
    for cond in where:
        path = parse_condition(cond)[0]
        seen: Dict[str, None] = {}
        for row in rows or []:
            for value in values_at(row, path):
                seen.setdefault(_entity(value).display, None)
            if len(seen) > MAX_HINT_VALUES:
                break
        shown = ", ".join(list(seen)[:MAX_HINT_VALUES]) or "(no values)"
        parts.append(f"{path}: {shown}")
    return "no rows matched 'where'; values present -> " + " | ".join(parts)


def _select_keys(
    lists: Dict[str, list], spec: dict
) -> Tuple[str, Dict[str, EntityRef], Dict[str, list]]:
    """``(description, members, rows_by_key)`` of the selection *spec*.

    *members* maps each key at ``spec['key']`` in the selected rows (after
    ``having``) to its entity; *rows_by_key* holds the selected rows per member.
    """
    if not isinstance(spec, dict) or not spec.get("key"):
        raise AnalysisError("Each set needs {source, key, where?}, e.g. {source:'a', key:'soldTo.id'}.")
    source, rows = select_rows(lists, spec.get("source"), spec.get("where"))
    members: Dict[str, EntityRef] = {}
    rows_by_key: Dict[str, list] = {}
    for row in rows:
        for ref in entities_at(row, spec["key"]):
            if ref.key:
                members.setdefault(ref.key, ref)
                rows_by_key.setdefault(ref.key, []).append(row)
    description = f"{source}.{spec['key']}"
    if spec.get("where"):
        description += f" where {' AND '.join(spec['where'])}"
    having = spec.get("having")
    if having:
        metric, field, op, threshold = parse_having(having)
        for key in list(members):
            value = _metric_value(metric, rows_by_key[key], field)
            if value is None or not _OPS[op](value, threshold):
                del members[key]
                del rows_by_key[key]
        description += f" having {having}"
    return description, members, rows_by_key


def key_set(lists: Dict[str, list], spec: dict) -> Tuple[str, Dict[str, EntityRef]]:
    """``(description, {key: EntityRef})`` -- a readable name of the selection
    *spec* and the entities at ``spec['key']`` in its rows (after ``having``)."""
    description, members, _ = _select_keys(lists, spec)
    return description, members


# ── operations ───────────────────────────────────────────────────────────


def _format_value(metric: str, field: Optional[str], value: float) -> str:
    if metric in ("count", "count_distinct"):
        return f"{int(value):,}"
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    if field and is_krw_field(field.rsplit(".", 1)[-1]):
        return f"{text} ({krw_eok_text(value)})"
    return text


def _ranked_lines(
    scored: List[Tuple[float, _Group]], metric: str, field: Optional[str], shown: int
) -> List[str]:
    """``rank | group | value | rows`` lines for the first *shown* groups."""
    ties = Counter(value for value, _ in scored)
    lines = []
    rank, previous = 0, None
    for position, (value, group) in enumerate(scored[:shown], start=1):
        if value != previous:
            rank, previous = position, value
        tie = " (tie)" if ties[value] > 1 else ""
        lines.append(f"{rank}{tie} | {group.label} | {_format_value(metric, field, value)} | {group.rows}")
    if len(scored) > shown:
        lines.append(f"... {len(scored) - shown} more groups (raise 'top' to see them)")
    return lines


def group_rows(
    rows: list,
    by: List[str],
    metric: str = DEFAULT_METRIC,
    field: Optional[str] = None,
    top: int = DEFAULT_TOP,
    order: str = DEFAULT_ORDER,
) -> List[str]:
    """Ranked ``rank | group | value`` lines (ties share a rank)."""
    if metric not in METRICS:
        raise AnalysisError(f"metric must be one of {METRICS}.")
    if metric != "count" and not field:
        raise AnalysisError(f"metric {metric!r} needs 'field' (dotted path).")
    groups: Dict[Tuple[str, ...], _Group] = {}
    for row in rows:
        per_path = [entities_at(row, p) or [_NONE_ENTITY] for p in by]
        for combo in itertools.product(*per_path):
            key = tuple(ref.key for ref in combo)
            groups.setdefault(key, _Group(display=combo)).add(row, metric, field)

    scored = [(g.value(metric), g) for g in groups.values()]
    scored = [(v, g) for v, g in scored if v is not None]
    # Two stable sorts: by name, then by value, so ties stay in name order.
    scored.sort(key=lambda vg: vg[1].sort_name)
    scored.sort(key=lambda vg: vg[0], reverse=order != "asc")
    label = metric if metric == "count" else f"{metric}({field})"
    shown = max(1, min(top, MAX_TOP))
    return [f"groups: {len(scored)} | rank | {' / '.join(by)} | {label} | rows"] + _ranked_lines(
        scored, metric, field, shown
    )


def set_operation(lists: Dict[str, list], operation: str, sets: List[dict]) -> List[str]:
    """Size and first members of intersect / difference (first minus rest) / union."""
    if operation not in SET_OPERATIONS:
        raise AnalysisError(f"operation must be 'group' or one of {SET_OPERATIONS}.")
    if not sets or len(sets) < 2:
        raise AnalysisError("Set operations need at least two sets.")
    named = [key_set(lists, s) for s in sets]
    lines = []
    for i, (spec, (name, members)) in enumerate(zip(sets, named)):
        lines.append(f"set {i + 1}: {name} -> {len(members):,} distinct")
        if not members:
            hint = values_hint(lists, spec.get("source"), spec.get("where"))
            lines.extend([f"  {hint}"] if hint else [])
    keys = [set(m) for _, m in named]
    if operation == "intersect":
        result = set.intersection(*keys)
    elif operation == "difference":
        result = keys[0].difference(*keys[1:])
    else:
        result = set.union(*keys)
    lookup: Dict[str, EntityRef] = {}
    for _, members in reversed(named):
        lookup.update(members)
    lines.append(f"RESULT {operation}: {len(result):,} distinct")
    for key in sorted(result, key=lambda k: lookup[k].display)[:MAX_MEMBERS]:
        ref = lookup[key]
        lines.append(f"- {ref.display} <{ref.uri}>" if ref.uri else f"- {ref.display}")
    if len(result) > MAX_MEMBERS:
        lines.append(f"... {len(result) - MAX_MEMBERS} more")
    return lines


def _restrict_by_membership(
    rows: list, key_path: str, members: Dict[str, EntityRef], exclude: bool
) -> list:
    return [r for r in rows if any(e.key in members for e in entities_at(r, key_path)) != exclude]


def _restrict_by_compare(
    rows: list,
    key_path: str,
    rows_by_key: Dict[str, list],
    condition: Tuple[str, str, str],
    exclude: bool,
) -> list:
    """Rows with (or with *exclude*, without) a same-key other row satisfying *condition*."""
    left, op, right = condition

    def matches(row: object) -> bool:
        return any(
            _scalar_matches(lv, op, str(rv))
            for ref in entities_at(row, key_path)
            for other in rows_by_key.get(ref.key, [])
            for lv in values_at(row, left)
            for rv in values_at(other, right)
        )

    return [r for r in rows if matches(r) != exclude]


def restrict_rows(lists: Dict[str, list], rows: list, restrict: Optional[dict]) -> Tuple[list, str]:
    """Keep rows whose ``restrict['key']`` entity is (or with exclude, is not) in a key set."""
    if not restrict:
        return rows, ""
    if not isinstance(restrict, dict) or not restrict.get("key") or not isinstance(restrict.get("in"), dict):
        raise AnalysisError("restrict needs {key, in:{source, key, where?}, exclude?, compare?}.")
    name, members, rows_by_key = _select_keys(lists, restrict["in"])
    key_path = restrict["key"]
    exclude = bool(restrict.get("exclude"))
    compare = restrict.get("compare")
    if compare:
        # cross-record condition: this row's field vs. a matched row of the other list
        condition = parse_condition(compare)
        kept = _restrict_by_compare(rows, key_path, rows_by_key, condition, exclude)
        left, op, right = condition
        verb = "NOT matching" if exclude else "matching"
        return kept, (
            f"restricted to {key_path} {verb} {name} with {left} {op} other.{right}"
            f" ({len(members):,} keys)"
        )
    kept = _restrict_by_membership(rows, key_path, members, exclude)
    verb = "NOT in" if exclude else "in"
    return kept, f"restricted to {key_path} {verb} {name} ({len(members):,} keys)"


def analyze(
    query: str,
    execute: Executor,
    *,
    operation: str,
    variables: Optional[dict] = None,
    source: Optional[str] = None,
    by: Optional[List[str]] = None,
    metric: str = DEFAULT_METRIC,
    field: Optional[str] = None,
    where: Optional[List[str]] = None,
    restrict: Optional[dict] = None,
    sets: Optional[List[dict]] = None,
    top: int = DEFAULT_TOP,
    order: str = DEFAULT_ORDER,
) -> str:
    """Fetch every row of *query* and return the requested aggregate as text."""
    lists, status, errors = fetch_all_rows(query, variables, execute)
    if not lists:
        msg = "; ".join(errors) if errors else "the query returned no root list"
        raise AnalysisError(f"Nothing to analyse: {msg}.")
    lines = [
        "analyze_graphql -- computed server-side over ALL fetched rows; "
        "quote these numbers as-is.",
        "rows fetched: "
        + ", ".join(f"{a}={len(r):,}" + _PARTIAL_NOTES.get(status[a], "") for a, r in lists.items()),
    ]
    if operation == "group":
        if not by:
            raise AnalysisError("operation 'group' needs 'by' (one or more dotted paths).")
        name, rows = select_rows(lists, source, where)
        rows, note = restrict_rows(lists, rows, restrict)
        lines.append(
            f"group {name}" + (f" where {' AND '.join(where)}" if where else "")
            + (f"; {note}" if note else "") + f": {len(rows):,} rows"
        )
        if not rows and where:
            lines.append(values_hint(lists, source, where))
        lines.extend(group_rows(rows, by, metric, field, int(top or DEFAULT_TOP), order))
    else:
        lines.extend(set_operation(lists, operation, sets or []))
    if errors:
        lines.append("GraphQL warnings: " + "; ".join(errors[:3]))
    return "\n".join(lines)
