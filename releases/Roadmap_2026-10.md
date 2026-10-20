# OntoBricks — Product Roadmap

> **Version:** 0.9.0 shipped → 0.10.0 and beyond<br>
> **Last updated:** 2026-10-20<br>
> **Status:** Living document — Asana-first prioritization<br>
> **Source:** OntoBricks-Product board (58 tasks: 37 completed, 21 incomplete)<br>
> **Scheduling policy:** priorities and dependencies are directional; this roadmap contains no delivery-date commitments.

> **Disclaimer:** This roadmap represents the current product direction and planned investments as of the date above. It is provided for informational purposes only and is subject to change at any time without notice. The features, ordering, and priorities described here are aspirational and do not constitute a commitment, promise, or legal obligation to deliver any specific functionality. Actual releases may differ materially from what is described here.

---

## Executive Summary

OntoBricks is a Databricks-native semantic engineering platform: teams design OWL ontologies, map Unity Catalog data, build interchangeable Lakehouse/Lakebase/Neo4j knowledge graphs, apply rules and reasoning, and publish governed graph capabilities through REST, GraphQL, Graph Chat, and MCP.

**v0.8.0, v0.8.1, and v0.9.0 are closed on the Asana board** (every task in those sections is completed). See `releases/ReleaseNotes_V0.8.0.md` and `releases/ReleaseNotes_V0.9.0.md`. v0.9.0 delivered Lakehouse//RT queries, staged Ontology Generate, Knowledge Store, GitHub domain mirror, Metric Views, SPARQL playground, entity SWRL rules, Job class actions, Ontology Studio, OBO / No-LLM gates, and security hardening.

The forward board is **Version 0.10.0** (8 open items) plus **To evaluate** (13 open items). Asana no longer has a separate 1.0.0 column: GA contract, connectors, and Marketplace/Hub still sit in 0.10.0 with Git working branch `1.0.0` — they are later-wave, not this month's authoring work.

1. **Finish the authoring journey** — Auto-Map as the single selective, entity-first mapping workflow (only High-priority incomplete item assigned to 0.10.0).
2. **Platform exchange** — Genie Pages / Databricks Domains, then Ontos after API discovery.
3. **Admin LLM allow-list** — administrators choose which Gateway models OntoBricks may use.
4. **Distribution and GA** — Marketplace listing, stable REST v1, SSO/SCIM, Hub, Neptune/Cosmos.
5. **Triage the rest** — governance, scale/federation, storage isolation, industry catalogs, Semantica-inspired provenance.

No Asana target date is treated as a roadmap commitment. Items are ordered by explicit Asana priority and status first, then dependencies and product value. Unprioritized records remain visible as **Needs triage**.

---

## Market Context

### Why the semantic layer matters

- **AI grounding** — agents need governed entities, relationships, actions, and live attributes rather than unstructured prompt stuffing.
- **Data-product semantics** — Unity Catalog governs physical assets; ontologies add business meaning, constraints, inheritance, and cross-domain relationships.
- **Open standards** — OWL, RDFS, SKOS, SHACL, SWRL, R2RML, SPARQL, REST, GraphQL, and MCP reduce proprietary lock-in.
- **Graph choice without redesign** — the same ontology and mapping model can target Lakehouse, Lakebase, or Neo4j, or publish as ontology-only.
- **Databricks-native operations** — source data remains governed in Unity Catalog while OntoBricks provides design, build, reasoning, exploration, and AI publication in one Databricks App.

### Strategic position

OntoBricks is the semantic authoring and publication layer for the Databricks Lakehouse. It does not require every domain to materialize a graph, and it does not force every graph into one engine.

The next differentiator is workflow quality: selective entity-first mapping, platform-native Genie/Domain exchange, and enterprise-grade API/governance controls. Durable parsed documents and three-stage Generate already shipped in v0.9.0.

---

## Prioritization and Status Legend

### Priority

- **High** — explicit High priority in Asana; should be evaluated before unprioritized work in the same release.
- **Medium / Low** — explicit Asana priority when present.
- **Needs triage** — the active Asana task has no priority value. It remains visible but its relative order is provisional.

### Status

- **In progress** — active implementation or validation work.
- **Analyzing** — scope, packaging, or technical direction is being assessed.
- **Not started** — scoped enough to remain on the release board but implementation has not started.
- **Needs status** — the active Asana task has no status value.
- **Shipped** — completed on the Asana board and/or delivered in the repository changelogs.

### Ordering rule

Within a release:

1. Explicit priority.
2. In progress, then Analyzing, then Not started, then unset status.
3. Dependency order.
4. Product value and risk reduction.

Asana owns planned scope and priority. The repository and `changelogs/v0.9.0/` own delivered status for this line. When those disagree, this document records both.

---

## Current State — v0.9.0 (includes v0.8.x)

### Semantic authoring

- Visual ontology design (Studio) with entity, relationship, attribute, inheritance, constraint, action, virtual-attribute, and inference-rule authoring.
- OWL/RDFS/SKOS/SHACL import/export plus R2RML mapping generation; OWL fidelity across save/import (labels, language tags, leftover RDF).
- Metadata-refresh diff, data-source deletion impact, schema-drift diagnostics, and reliable Studio persistence.
- LLM-assisted three-stage ontology Generate and Auto-Mapping with live agent activity and durable audit reports.
- Knowledge Store (Lakebase parsed corpus) shared by Generate and Mapping.
- Unity Catalog Metric Views as data assets (dimension/measure roles, `MEASURE()` SQL).

### Graph and reasoning

- Per-domain **Lakehouse**, **Lakebase**, **Neo4j**, or **No Backend** selection.
- Lakehouse materialized-table or live-view modes.
- Split **Build** vs **Query** SQL warehouses, optional Lakehouse//RT reads over the Statement Execution API, and a global CloudFetch toggle for Databricks Apps egress.
- OWL 2 RL, SWRL (including per-entity rules), Data Quality (graph-wide and per-entity), cohorts, generated-inference count and safe purge.
- Graph Analytics, Graph Explorer (indexed search, paged BFS, Find Path, quality rings), Graph Chat, SPARQL playground, and bounded graph-read execution.
- Class actions: Unity Catalog functions and Databricks Jobs (`entity_id`, async `run_now`, Background Tasks).

### Publication and integration

- PUBLISHED-only external REST and GraphQL data plane.
- MCP with per-domain tool/context policy, ontology-only `describe_ontology`, cross-domain bridge guidance, actions, datasets, on-demand virtual attributes, entity SHACL checks, and entity business rules.
- GitHub domain mirror (push on save, Load from GitHub, optional GitHub delete).
- Swagger/ReDoc with typed domain payloads and explicit graph-backend readiness.

### Governance and operations

- DRAFT → IN-REVIEW → PUBLISHED lifecycle, including ontology-only review.
- Direct-user and Databricks-group permission resolution; admin-only domain creation; per-backend OBO.
- Builder graph refresh on frozen versions.
- Dual Volume/Lakebase registry, compact domain audit trail, task/run history, scheduler, and DAB deployment.
- Configurable instance-wide title, color, logo, and default entity icon.
- CSRF cookie+header, public `/health` liveness, dialect-aware SQL escaping.

---

## Known Limitations After v0.9.0

- Mapping still has separate Manual and Auto-Map paths instead of one selective, entity-first workflow.
- MCP visibility rules are session-aware, but the selected domain and cached domain context are process-shared in the current standalone server design.
- Fine-grained API credentials, durable Delta audit events, and per-domain/per-version Unity Catalog RBAC are not yet available.
- Large-graph APIs do not consistently use server-side cursor pagination (Explorer BFS is paged; Overview/status now use companions rather than scanning the pass-through graph VIEW).
- Cross-workspace federation and guided graph-engine migration are not available.
- Databricks Ontos, Genie Pages, and Databricks Domains are not yet bidirectional ontology sources/targets.
- Administrators cannot yet restrict which Gateway models are available to OntoBricks features.
- Internationalization has not been scaffolded.
- Amazon Neptune and Azure Cosmos DB connectors are not implemented.
- Enumeration is not a first-class attribute type.

---

## Roadmap

### Closed on the board — v0.7.2 / v0.8.0 / v0.8.1 / v0.9.0

All Asana tasks in these sections are completed (37 of 58 board tasks). Highlights:

- **v0.8.0** — ontology-only domains, MCP policy, virtual attributes, mapping integrity, Lakehouse views, Clarity UI, purge inferences.
- **v0.8.1** — fail-closed Spark SPARQL validator, Databricks License alignment, admin-only domain creation.
- **v0.9.0** — Lakehouse//RT, Knowledge Store, three-stage Generate, GitHub mirror, Metric Views, SPARQL playground, entity SWRL, Job actions, Studio, OBO, packaging/deployment analysis closed on the board.

**Board hygiene:** GitHub support and Packaging & Deployment are completed but still show Status In progress / Analyzing. Full tests campaign is completed with Status still In progress. Close or sync those custom fields so the next snapshot does not look active.

---

### v0.10.0 — Next product column (Asana)

Eight incomplete parents. Only Auto-Map currently has High priority.

#### High-priority authoring

##### 1. Auto-Map revamping: selection UI, drop Manual Mapping, entity-first run

**Priority:** High<br>
**Status:** Not started

Make Auto-Map the single mapping entry point:

- Remove the Manual Mapping page, route, navigation, and unused client code.
- Present potential entity and relationship mappings in separate selectable tabs (all checked by default).
- Run only checked items, **entities first**, persist keys, then generate relationship mappings against those keys.
- Reuse the Knowledge Store when document evidence informs mapping.

Subtasks: design spec; remove Manual Mapping; entity/relation checkbox UI; entity-first run; tests/docs/changelog.

#### Platform authoring integrations

##### 2. Integrate Databricks Genie Pages and Domains

**Priority:** Needs triage<br>
**Status:** Not started

Bidirectional exchange: read Genie Pages and Databricks Domains as Generate sources (combined with uploaded documents), then publish a validated ontology back as Domain and/or Genie Page with preview, confirmation, permission-aware create/update, and source traceability.

##### 3. Administrator-controlled LLM availability

**Priority:** Needs triage<br>
**Status:** Needs status

Allow administrators to choose which configured LLM endpoints/models are available to OntoBricks features. Scope must cover capability discovery, feature compatibility, fallback, secret handling, and whether selection is global or feature-specific.

#### Distribution (overlaps later-wave Marketplace)

##### 4. Marketplace integration/deployment

**Priority:** Needs triage<br>
**Status:** Needs status

Listing requirements, review process, and asset preparation. Distinct from the Distribution & Community one-click-install subtask; treat as packaging/listing groundwork unless the board merges them.

#### Asana still open — already in v0.9.0

##### 5. Ontology entity actions: trigger a Databricks Job asynchronously

**Priority:** Needs triage<br>
**Status:** Needs status

The repository already ships this in v0.9.0: Studio Behaviour → Jobs, Explorer popup with verified Running/Queued, Background Tasks with Databricks run link, MCP `invoke_entity_action` (`job:<id>`), OBO on Lakehouse. Close the Asana parent after confirming remaining subtasks (parameter mapping, instance payload) are either done or explicitly deferred.

#### Later-wave items parked in the 0.10.0 column (Git branch 1.0.0)

These three rollups are incomplete in Version 0.10.0 but tagged Git Working Branch **1.0.0**. Do not schedule them ahead of Auto-Map / Genie without an explicit priority change.

##### 6. GA Contract & Identity

**Priority:** Needs triage<br>
**Status:** Needs status

- Stable REST API v1 with SemVer and a documented deprecation policy.
- SSO/SCIM provisioning for enterprise identity lifecycle.

##### 7. Ecosystem Connectors

**Priority:** Needs triage<br>
**Status:** Needs status

- Amazon Neptune connector using RDF/SPARQL over HTTPS.
- Azure Cosmos DB connector using the Gremlin API.

Both should implement the existing graph-store abstraction. Live service compatibility tests remain mandatory.

##### 8. Distribution & Community

**Priority:** Needs triage<br>
**Status:** Needs status

- Databricks Marketplace one-click installation.
- OntoBricks Hub for community ontologies and mapping templates.

Hub is separable from a GA contract and should not delay stable API/identity unless explicitly reprioritized.

---

### Requests — To evaluate

Thirteen incomplete parents. Dates and old roadmap placement do not promote them automatically.

#### Ontology Import · Evaluate industry standards

**Priority:** High<br>
**Status:** Not started

32 candidate catalogs (same pattern as FIBO, CDISC, IOF, FHIR). Checklist per standard: licence and canonical source, OWL/RDF artefact, volumetry, Lakehouse mapping sketch, overlap, go/no-go. Strongest product-fit names called out on the parent: QUDT, BFO/CCO, CIM, Brick, RealEstateCore, Allotrope, AAS, GS1, OMOP, DCAT-AP, PROV-O.

#### External Surface Expansion

**Priority:** Needs triage<br>
**Status:** Analyzing

- Dashboards, actions, and datasets on the external API.
- MCP GraphRAG-style retrieval.
- Design pass for DRAFT domains and agentic external operations.

Parts of the external surface advanced in v0.8.0/v0.9.0 (typed Swagger, MCP policy, class actions, bridges, virtual attributes). Re-scope the remaining API work before assigning it to a release.

#### Ontos Integration

**Priority:** Needs triage<br>
**Status:** Needs status

Import or align Ontos assets; publish OntoBricks ontology/mapping assets; resolve granularity, authentication, ownership, and conflicts. Discovery-heavy until the external API is validated.

#### Deferred Workflow Items from v0.6.0

**Priority:** Needs triage<br>
**Status:** Needs status

- Ontology iteration UX (version diff, promote, rollback).
- Mapping multi-select.
- Mapping orphan detection.
- Scheduled inference/materialization.
- Temporal reasoning and stratified recursive Datalog.

#### Governance & Access Control

**Priority:** Needs triage<br>
**Status:** Needs status

- Scoped API-key authentication for external REST/GraphQL.
- Durable structured audit events in Delta.
- Fine-grained per-domain and per-version RBAC backed by Unity Catalog grants.

#### Scale, Deploy & Federation

**Priority:** Needs triage<br>
**Status:** Needs status

- Server-side cursor pagination for large graphs.
- One-command deployment of UI, MCP, registry, migrations, jobs, and permissions.
- Guided graph-engine migration between Lakehouse, Lakebase, and Neo4j.
- Cross-workspace domain registry and access federation.

Packaging analysis on the v0.9.0 board is completed; one-command deploy and Marketplace still depend on that groundwork.

#### Ontology & Knowledge Graph Modeling

**Priority:** Needs triage<br>
**Status:** Needs status

- First-class Enumeration attribute type.
- SPARQL editor under Knowledge Graph — **playground already shipped in v0.9.0**; remaining work is Enumeration plus any editor upgrade beyond the playground.

#### Domain Storage Isolation

**Priority:** Needs triage<br>
**Status:** Needs status

- Dedicated Lakebase schema per domain.
- Dedicated Unity Catalog/Lakehouse schema per domain.
- Create, rename-safe bind, retire/drop, grants, and migration lifecycle.

#### Platform UX & Localization

**Priority:** Needs triage<br>
**Status:** Needs status

- Generalize unsaved-change protection beyond Studio surfaces.
- i18n foundations once target languages and translation ownership are known.

#### Virtual attributes in Business Rules conditions

**Priority:** Needs triage<br>
**Status:** Needs status

Evaluate whether on-demand Unity Catalog function values can participate safely in rule conditions (determinism, batching, latency, retries, auditability). Virtual attributes remain outside Data Quality materialization.

#### Arrows.app schema import

**Priority:** Needs triage<br>
**Status:** Needs status

Ontology Import tab for arrows.app JSON → OWL Turtle, preview of created vs dropped items, then the existing OWL replace/append pipeline.

#### Collibra metadata import

**Priority:** Needs triage<br>
**Status:** Needs status

Authentication, asset hierarchy, glossary/ontology mapping, incremental refresh, lineage, and whether the integration belongs in source metadata ingestion or ontology import.

#### Inspiration Semantica

**Priority:** Needs triage<br>
**Status:** Needs status

Steal ideas from Semantica without depending on that package or becoming a document-NER SDK. Evaluation items: triple-level PROV-O provenance; instance-value conflicts at sync; auditable decision/action events; MCP “why” tools (`explain_inference`, subgraph export, retrieve-by-entity); `ontobricks doctor` operator healthcheck.

---

## Cross-Release Dependencies

1. **Knowledge Store → Auto-Map document evidence**<br>
   Auto-Map should not preserve a second on-demand extraction path.

2. **Validated Generate entities → entity-first Auto-Map**<br>
   Entity review/synonyms and selective mapping should converge on one stable entity identity contract.

3. **Packaging analysis (closed in v0.9.0) → one-command deployment → Marketplace**<br>
   Distribution should reuse a single deploy/bootstrap model rather than fork a Marketplace-only installer.

4. **Stable identity/RBAC → federation**<br>
   Cross-workspace access requires durable domain identity, authorization, and audit semantics.

5. **Stable graph-store contract → Neptune/Cosmos connectors**<br>
   Connector work must preserve the same build/read/reasoning capabilities and declare unsupported features explicitly.

6. **External API write design → API keys and stable v1**<br>
   Draft/agentic operations should not ship before authentication, authorization, idempotency, and deprecation rules are defined.

7. **Genie/Domain/Ontos boundaries**<br>
   Overlapping platform ontology surfaces need one asset and conflict model before bidirectional write.

---

## Feature Matrix

| Capability | v0.8.0 | v0.9.0 | v0.10.0 direction |
| --- | --- | --- | --- |
| Ontology-only publication | Delivered | Harden | Stable contract (later wave) |
| Lakehouse / Lakebase / Neo4j | Delivered | RT queries, companions | Migration and scale (triage) |
| Per-domain MCP policy | Delivered | Entity DQ + SWRL tools | Retrieval/auth evolution |
| Virtual attributes | Delivered | Studio Attrs. tab | Evaluate rule integration |
| Knowledge Store / parse once | — | Delivered | Foundation for Auto-Map |
| Human-reviewed Generate | — | Delivered | Stable workflow |
| Selective Auto-Map | Not available | Not available | **High priority** |
| SPARQL playground | — | Delivered | Enumeration / editor extras |
| Job class actions | — | Delivered | Close Asana parent |
| GitHub mirror | — | Delivered | Operate |
| Metric Views | — | Delivered | Operate |
| Genie/Databricks Domain exchange | Not available | Not available | Assigned to 0.10.0, unprioritized |
| Ontos integration | Not available | Not available | Needs triage |
| Admin LLM allow-list | Partial (No LLM + Gateway picker) | Partial | Assigned to 0.10.0 |
| Fine-grained API/RBAC/audit | Partial | Partial | Needs triage |
| Marketplace one-click | Not available | Packaging closed | 0.10.0 + 1.0.0-branch overlap |
| Stable REST API v1 policy | Partial | Prepare | Later wave in 0.10.0 column |

---

## Graph Engine Comparison

| Capability | Lakehouse | Lakebase | Neo4j | No Backend |
| --- | --- | --- | --- | --- |
| Storage | Delta table or live UC view | Postgres graph tables | Native property graph | None |
| Source governance | Full Unity Catalog | UC source + managed Postgres | UC source + external graph | Ontology in registry |
| Graph query | SQL/SPARQL translation, GraphQL | SQL/SPARQL subset, GraphQL | Cypher adapter, GraphQL | Not available |
| Reasoning materialization | Generated Delta companion | Generated Postgres companion | Backend-dependent; purge unsupported | Not available |
| Graph Analytics | UC snapshot/job | UC snapshot/job | UC snapshot/job | Not available |
| Best fit | Governed/live Lakehouse data | Databricks-native graph persistence | Existing graph-database estates | Semantic contracts and MCP ontology |

---

## Open Decisions

1. **Auto-Map candidate source** — whether mapping selection starts from the validated Generate entity set, the full ontology, or both with explicit provenance.
2. **Genie/Domain/Ontos boundaries** — one asset and conflict model across platform ontology surfaces.
3. **Domain storage naming and migration** — fixed generated schema names versus operator selection, plus safe adoption for existing multi-domain installations.
4. **GraphRAG definition** — ranked retrieval over structured graph entities versus classic chunk embeddings/community summaries.
5. **External write authentication** — whether DRAFT/agentic APIs wait for scoped API keys or use a narrower Databricks-authenticated contract first.
6. **Virtual attributes in rules** — live evaluation versus explicit materialization, with deterministic audit and performance limits.
7. **LLM availability policy** — global allow-list, feature-specific compatibility matrix, and fallback behavior.
8. **Industry-catalog go/no-go** — which of the 32 evaluation targets become first-class Import catalogs.
9. **Job-action Asana close-out** — remaining spec (parameter mapping) versus already-shipped v0.9.0 behaviour.
10. **Internationalization ownership** — target languages, source-key extraction, translation review, and release validation.

---

## How to Contribute

The most useful contributions map to active, prioritized Asana work or help triage an unprioritized request with a concrete user case.

- For **Auto-Map**, provide examples where entity-first keys materially improve relationship mapping.
- For **Genie Pages, Databricks Domains, or Ontos**, document the desired direction of exchange and conflict behavior.
- For **industry catalogs**, supply licence, canonical OWL, and a Lakehouse mapping sketch.
- For **GraphRAG / Semantica “why” tools**, distinguish structured graph retrieval from unstructured chunk retrieval.
- For **new graph connectors**, implement the graph-store abstraction and declare capability differences explicitly.
- For **enterprise governance**, provide API credential, RBAC, audit retention, and cross-workspace requirements.

Feature discussions should identify the domain, expected outcome, security boundary, scale, and acceptance signal. That evidence is what allows an Asana item marked **Needs triage** to become a credible release priority.
