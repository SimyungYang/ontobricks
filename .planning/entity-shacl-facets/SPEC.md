# SPEC: entity-shacl-facets

> **Scope:** a new entity-scoped data-quality check, exposed three ways — a
> Graph Explorer right-click item opening a popup, a public REST endpoint, and
> a new MCP tool under `src/mcp-server/**`.
>
> Not a new Foundation Model agent. OntoBricks does not call the LLM here —
> this SPEC covers the **tool contract** (a new `check_entity_shacl_facets`
> tool and the hint that tells a model when to call it) and the **response
> contract** both the popup and downstream LLMs read.

## 1. Purpose

SHACL shapes are authored per class in **Ontology → Data Quality** and executed
in bulk by **Digital Twin → Data Quality**, which answers "how healthy is the
`Customer` population?". Nothing answers the question a user actually has when
looking at one node: *"is this customer OK, and if not, what is wrong with it?"*

Today the only route is to run the whole data-quality suite, open each failing
check, and scan its violation sample for the entity — which is capped at
`violation_limit` rows, so the entity may simply not be listed.

This change adds an **entity facet check**: for one entity, collect every
enabled SHACL shape that relates to its type, evaluate each against that entity
only, and return one verdict per shape together with the values the entity
actually carries on the constrained property.

### 1.1 Which shapes relate to the entity type

An enabled shape is selected when its `target_class_uri` is:

1. one of the entity's own `rdf:type` classes, or
2. an ancestor of one of them (`Rules.class_lineage`), or
3. empty — a global shape (`requireLabels`, `noOrphans`, property-level
   `sh:maxCount` from `owl:FunctionalProperty`, …).

The entity's types come from its `rdf:type` triples in the triple store. When
none is found, `NodeContextService.match_ontology_class` (URI-shape matching)
is the fallback, so a node reached through the Graph Explorer always resolves.

### 1.2 Deliberate non-goals

- **SWRL rules, decision tables and aggregate rules** are not SHACL and stay in
  the bulk Data Quality run. The popup does not list them.
- **No remediation.** The popup reports; it does not edit triples or shapes.
- **No change to the bulk run.** The inherited-shape class substitution
  (§3.3) is applied on the entity path only. The bulk run keeps matching
  exact `rdf:type`; aligning it is a separate decision.

## 2. Identity

| Field | Value |
|---|---|
| `agent_name` | `entity_shacl_facets` (tool-contract, not an agent) |
| `module_path` | `src/back/objects/digitaltwin/node/NodeShaclService.py` + `src/mcp-server/server/tools.py` + `src/back/core/mcp_tools.py` |
| `model_endpoint` | n/a — consumed by external LLMs (Playground, Cursor, Claude Desktop) |
| `temperature` | n/a |
| `mlflow_experiment` | `/Shared/ontobricks/mcp/entity_shacl_facets` (created on first eval run) |

## 3. Contracts

### 3.1 Surfaces

| Surface | Change |
|---|---|
| `GET /api/v1/digitaltwin/nodes/shacl` | **New.** Public, `assert_public_graph_read`. Query: `entity_uri`, `domain_name`, `domain_version`, registry overrides |
| `GET /dtwin/nodes/shacl` | **New.** Internal (UI), `assert_domain_graph_read`, session domain |
| `GET …/nodes/context` | Gains `shacl_shape_count` (int, selected shapes per §1.1, no evaluation, no warehouse query beyond the existing ones) |
| Graph Explorer node menu | New **Data quality** section with **Check SHACL facets (N)**, rendered only when `shacl_shape_count > 0` |
| MCP `check_entity_shacl_facets(entity_uri)` | **New.** Registered in `MCP_DOMAIN_TOOLS` with `requires_graph: True`, so the per-domain tool toggle governs it |
| MCP `get_entity_context` / `describe_entity` | `[Context]` block gains `SHACL shapes (N) — call check_entity_shacl_facets(entity_uri) to evaluate` when N > 0 and the tool is enabled |

### 3.2 Response contract (authoritative)

```json
{
  "success": true,
  "entity_uri": "http://…/Customer/C-42",
  "entity_local_id": "C-42",
  "entity_label": "ACME Corp",
  "classes": [{"name": "Customer", "uri": "http://…#Customer"}],
  "lineage": ["Customer", "Party"],
  "summary": {
    "total": 7, "pass": 4, "fail": 2, "not_applicable": 1,
    "not_evaluated": 0, "error": 0,
    "fail_by_severity": {"sh:Violation": 1, "sh:Warning": 1, "sh:Info": 0}
  },
  "facets": [
    {
      "shape_id": "auto_completeness_Customer_email_1a2b3c4d",
      "label": "Customer.email must not be empty",
      "category": "completeness",
      "severity": "sh:Violation",
      "target_class": "Customer",
      "inherited_from": null,
      "property_path": "email",
      "property_uri": "http://…/email",
      "shacl_type": "sh:minCount",
      "parameters": {"sh:minCount": 1},
      "constraint": "sh:minCount 1",
      "status": "fail",
      "values": [],
      "values_truncated": false,
      "details": [{"s": "http://…/Customer/C-42", "count": 0}],
      "message": "Customer.email must not be empty"
    }
  ]
}
```

Field rules:

- `status` is one of `pass`, `fail`, `not_applicable`, `not_evaluated`,
  `error`.
  - `not_applicable` — the shape carries conditions and the entity does not
    match the guard.
  - `not_evaluated` — `SHACLService.shape_to_sql` returns `None` (e.g.
    `sh:closed`, an unrecognised `sh:sparql`). **Never reported as `pass`.**
  - `error` — the shape's SQL failed; `message` carries the first
    `_SQL_ERROR_MAX_CHARS` of the engine error.
- `inherited_from` names the ancestor class when the shape was selected via
  §1.1 (2); `null` for own-class and global shapes.
- `values` lists the entity's objects on `property_uri` (resolved with
  `SHACLService.resolve_prop_uri`), capped at 20 with `values_truncated`.
  Empty list for shapes without a property.
- `details` holds the violation rows restricted to the entity (each row as the
  shape SQL emitted it: `count`, `val`, `target`, …). Empty on `pass`.
- `facets` order: `fail` (Violation → Warning → Info), `error`,
  `not_evaluated`, `pass`, `not_applicable`; then category, then label.
- An entity whose types match no shape returns `success: true`, empty
  `facets`, all-zero `summary` — not an error.

### 3.3 Evaluation (SQL, entity-scoped)

`NodeShaclService.evaluate(domain, settings, entity_uri)`:

1. `store = get_graphdb(domain, settings, engine="view")`; table from the same
   resolver the Data Quality routes use (`_dataquality_table`, to be lifted
   into the service layer so both share it).
2. One query loads the entity's own triples
   (`SELECT predicate, object FROM t WHERE subject = :uri`) → types, label,
   `values`.
3. Select shapes per §1.1. For an inherited shape, evaluate a **copy** whose
   `target_class_uri` is replaced by the entity's own class URI: the triple
   store only carries the asserted `rdf:type`, so evaluating against the
   ancestor URI would silently pass.
4. Resolve property URIs with `SqlQualityChecks._resolve_shape_uri_for_sql`
   against the predicates seen in step 2 plus the shape's own URI.
5. Build one `UNION ALL` batch. Each branch is
   `SELECT '<shape_id>' AS shape_id, 'v' AS kind, to_json(struct(v.*)) AS detail FROM (<shape_to_sql>) v WHERE v.s = '<uri>'`.
   Shapes with conditions add a `kind = 'g'` branch from
   `ShapeConditions.subject_sql(...)` filtered on the entity: no `g` row ⇒
   `not_applicable`.
6. If the batch fails, rerun shape by shape so one broken shape yields one
   `error` facet instead of failing the whole response.
7. All literals go through `sql_escape`; shape IDs are restricted to
   `[A-Za-z0-9_\-]` before being inlined.

Global uniqueness shapes (`sh:sparql` inverse-functional) scan the whole table
inside the subquery; this is accepted — it is the same query the bulk run
issues, filtered afterwards.

### 3.4 Enforcement points

1. **MCP tool toggle** — `check_entity_shacl_facets` follows the per-domain
   `MCP_DOMAIN_TOOLS` policy; disabled ⇒ not registered for that domain and
   the `[Context]` hint is omitted.
2. **Read-only** — both routes use read guards; nothing is written.
3. **No shape injection** — shapes are read from the domain, never from the
   request body (unlike `/dataquality/execute`).

## 4. Success criteria

1. **Own, inherited and global shapes are all listed**
   - input: `Customer ⊑ Party`; shapes on `Customer.email`, `Party.name`, and
     global `requireLabels`.
   - expected: three facets; `Party.name` has `inherited_from: "Party"`.
2. **Inherited shapes are actually evaluated**
   - input: entity typed `Customer` only, missing `name`; `Party.name minCount 1`.
   - expected: that facet is `fail`, not `pass`.
3. **Verdict parity with the bulk run**
   - input: any entity, all enabled own-class shapes.
   - expected: a shape is `fail` here iff the entity appears in the bulk run's
     uncapped violation set for that shape.
4. **Guarded shapes**
   - input: shape with condition `status = "active"`; entity has `status = "closed"`.
   - expected: `not_applicable`.
5. **Untranslatable shapes are never green**
   - input: `sh:closed` shape on the class.
   - expected: `not_evaluated`.
6. **One broken shape degrades to one facet**
   - input: a shape whose SQL fails (bad regex for `RLIKE`).
   - expected: that facet is `error`, the others carry verdicts, `success: true`.
7. **Popup**
   - right-click a node whose class has shapes ⇒ menu item with the count;
     click ⇒ modal with summary chips and facets grouped by category,
     failures first. No item when the count is 0.
8. **MCP**
   - "Is customer C-42 compliant with our data-quality rules?" ⇒ the model
     calls `check_entity_shacl_facets` and names the failing constraints and
     their observed values.

## 5. Eval dimensions

The CI gate parses this table — keep it well-formed.

| Dimension | Metric | Threshold | Weight | Judge |
|---|---|---|---|---|
| `facets_complete` | fraction of rows where every shape selected per §1.1 appears in the response | `1.00` | `0.25` | rule-based |
| `verdict_parity` | fraction of rows where each facet's pass/fail matches the bulk run's uncapped violation set for that entity | `1.00` | `0.25` | rule-based |
| `never_false_pass` | fraction of rows where untranslatable or erroring shapes are not reported as `pass` | `1.00` | `0.20` | rule-based |
| `calls_tool_for_quality_question` | fraction of quality-question rows where the model calls `check_entity_shacl_facets` before answering | `0.90` | `0.30` | rule-based |

**Aggregate threshold:** weighted sum ≥ `0.90` to pass G2.

The three contract dimensions are pass/fail (`1.00`) and cannot be masked by
the behavioural one.

## 6. Failure modes

| Symptom | Detection | Mitigation |
|---|---|---|
| Inherited shape always passes | Criterion 2 unit test | Class substitution in §3.3 step 3 |
| Property shape always fails with `count: 0` | `values` empty while the Graph Explorer shows the attribute | `#` vs `/` URI mismatch — resolved via `resolve_prop_uri` against the entity's own predicates |
| Popup slow on large graphs | Latency > 3 s on uniqueness shapes | Inherent to the global subquery; acceptable for an on-demand action. Revisit with a per-shape cache if it becomes the norm |
| Whole popup errors on one bad shape | Criterion 6 test | Per-shape rerun fallback (§3.3 step 6) |
| Entity resolves to no class | `classes` empty, only global facets | `match_ontology_class` fallback; otherwise the entity genuinely has no `rdf:type` and global shapes are all that apply |
| Model answers from `[Context]` without evaluating | `calls_tool_for_quality_question` < 0.90 | Sharpen the hint wording |

## 7. Eval dataset

- **Baseline file:** `tests/eval/datasets/mcp/entity_shacl_facets.jsonl` —
  10 examples (material-change floor per `.cursor/12`): own/inherited/global
  selection, inherited fail, guarded `not_applicable`, `sh:closed`
  `not_evaluated`, broken-regex `error`, entity with no shapes, tool disabled
  for the domain, and three natural-language quality questions.
- Row shape mirrors `tests/eval/datasets/mcp/virtual_attributes.jsonl`.
  New constraint kinds: `facet_listed`, `facet_status`, `facet_inherited_from`,
  `shacl_tool_called`; reused: `call_refused`.

## 8. MLflow tracing

No Foundation Model API call on this path: the MCP handler proxies the REST
endpoint and formats text. The eval harness publishes one MLflow run per
evaluation under `/Shared/ontobricks/mcp/entity_shacl_facets`; the PR body
pastes the run URI under "MLflow eval run" per `.cursor/12`.

## 9. Test plan

- `tests/units/…/test_node_shacl_service.py` — shape selection (§1.1),
  class substitution, status mapping, ordering, summary, batch-failure
  fallback (store mocked).
- SQL snapshot test for the `UNION ALL` batch, including escaping of a URI
  containing `'`.
- Route tests for both endpoints (public guard, missing entity, no shapes).
- `tests/…/mcp` formatting test for `check_entity_shacl_facets` and the
  `[Context]` hint, plus the disabled-tool case.

## 10. Sign-off

- [ ] Contracts in §3 reviewed.
- [ ] Eval dataset committed (10 examples).
- [ ] Baseline eval run URI pasted into PR body.
- [ ] Aggregate threshold ≥ `0.90`.
