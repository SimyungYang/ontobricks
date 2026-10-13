# OntoBricks — Release Notes V0.8.2

**Release window:** October 2026<br>
**Type:** Patch release (v0.8.1 → v0.8.2)<br>
**Test status:** targeted registry + ontology unit tests green (549 passed). The full `uv run --frozen pytest -q -m "not scenario"` run still stalls in `tests/units/api/test_routes.py` on this line (same stall recorded on 2026-10-01).

---

## Highlights

- **Enterprise Initialize no longer 502s on DDL ownership** — when the app service principal is DML-only and `schema.sql` raises `must be owner of table`, Initialize logs a warning, skips DDL, and still upserts the registry identity row (catalog / schema / volume).
- **Ontology generation uses the bound Unity Catalog Volume** — `generate_with_agent` and `generate_rules_with_agent` pass `RegistryCfg.from_domain(...).as_dict()` instead of the seeded session dict (`OntoBricksRegistry`). Document tools no longer look up a non-existent `…ontobricksregistry` volume.

No registry schema migration. No breaking MCP tool names.

---

## Registry (Lakebase Initialize)

Enterprise workspaces often provision Lakebase with a dedicated owner identity. The Databricks App SP then has DML on `ontobricks_registry` but cannot `ALTER` / `CREATE INDEX` on existing tables such as `domains`.

On 0.8.1, `initialize()` wrapped `_apply_ddl()` and the registry-row DML in one `try/except`. A DDL ownership error returned **502 Registry initialization failed** and never pinned the bound Volume.

On 0.8.2, `_apply_ddl()` is non-fatal:

- DDL failure → warning (`Lakebase DDL skipped (schema already provisioned or caller is not table owner)`) and continue.
- Registry row upsert and column migrations still run.
- A **DML** failure still fails Initialize.

**Check Access** can still report the DDL error; Initialize can succeed and pin the Volume.

---

## Ontology generation (bound Volume)

Agent document reads go through the Unity Catalog Files API on the registry Volume. Generation previously forwarded the seeded session registry (`OntoBricksRegistry`), so tools resolved a volume such as `catalog.schema.ontobricksregistry` instead of the bound volume (`catalog.schema.ontobricks` in the Reliance / Jio Home staging layout).

That produced `FILES_API_RESOURCE_NOT_FOUND`, empty metadata (`0 available table(s)`), then a guidelines-only OWL emit that hit the 10-iteration cap (`finish_reason=length`, output not starting with `@prefix`).

Both ontology and business-rules generation now resolve the Volume the same way as the rest of the domain: `RegistryCfg.from_domain(session, settings)`.

---

## Documentation and operator notes

- Package and README version are **0.8.2**.
- Changelogs: `changelogs/v0.8.2/benoitcayladbx_2026-10-01.log` (version bump) and `changelogs/v0.8.2/benoitcayladbx_2026-10-12.log` (Initialize + bound-volume generation).

---

## Upgrade notes

- **Lakebase (pre-provisioned schema):** click **Initialize** again after deploy. A DDL owner error is expected and should no longer block pinning catalog / schema / volume. Confirm the bound Volume on Registry → Location matches the UC Volume that holds domain `documents/`.
- **Lakebase (app SP is table owner):** behaviour is unchanged; `schema.sql` still applies.
- **Ontology generation:** re-run Generate after Initialize so document tools hit the bound Volume. Large specs (hundreds of attributes in one Turtle dump) can still truncate at the model token limit; split by functional domain if that happens.
- **Lakebase / MCP policy:** the v0.8.0 `domains.mcp_policy` migration remains a prerequisite if you are still on 0.7.x. This patch does not add columns.

---

## Bug Fixes

- Fixed Initialize returning 502 when `schema.sql` raised `InsufficientPrivilege: must be owner of table …` on a pre-provisioned Lakebase schema.
- Fixed Initialize aborting before the registry identity row (and bound Volume) was upserted.
- Fixed ontology / rules generation reading documents from the seeded `OntoBricksRegistry` volume name instead of `RegistryCfg.from_domain`.
- Fixed agent `read_document` 404s (`Volume '….ontobricksregistry' does not exist`) on workspaces whose bound Volume is not named after the Lakebase schema.
