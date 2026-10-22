"""SQL data-quality checks against the VIEW triple store.

Extracted from :class:`DigitalTwin` (Fowler Extract Class).
``DigitalTwin`` keeps one-line delegators for callers.
"""

from __future__ import annotations

import time
from typing import Optional

from back.core.helpers import sql_escape as escape_sql_value
from back.core.logging import get_logger
from back.core.w3c.shacl.constants import (
    AGGREGATE_ID_PREFIX,
    DECISION_TABLE_ID_PREFIX,
    SWRL_ID_PREFIX,
    rule_check_id,
)
from back.objects.digitaltwin.constants import RDF_TYPE, RDFS_LABEL

logger = get_logger(__name__)


def _dt():
    from back.objects.digitaltwin.DigitalTwin import DigitalTwin

    return DigitalTwin


class SqlQualityChecks:
    """Classify predicates and run SHACL / SWRL / decision-table / aggregate SQL checks."""

    #: A failed check used to report only that it failed, which left the reader
    #: no way to tell a broken rule from a warehouse problem without the server
    #: log. Long enough for the engine's error code and its first sentence.
    _SQL_ERROR_MAX_CHARS = 300

    def __init__(self, domain) -> None:
        self._domain = domain

    def classify_predicates(self, top_predicates: list) -> list:
        """Classify predicates into 'attribute' or 'relationship' kinds."""
        domain = self._domain
        attr_predicates = {
            RDF_TYPE,
            RDFS_LABEL,
            "http://www.w3.org/2000/01/rdf-schema#comment",
            "http://www.w3.org/2000/01/rdf-schema#seeAlso",
        }
        rel_predicates = {"http://www.w3.org/2002/07/owl#sameAs"}

        obj_prop_uris = set()
        data_prop_uris = set()
        for p in domain.get_properties():
            p_uri = p.get("uri", "")
            if p.get("type") == "ObjectProperty":
                obj_prop_uris.add(p_uri)
            else:
                data_prop_uris.add(p_uri)

        classified = []
        for r in top_predicates:
            uri = r["predicate"]
            cnt = int(r["cnt"])
            if uri in attr_predicates or uri in data_prop_uris:
                kind = "attribute"
            elif uri in rel_predicates or uri in obj_prop_uris:
                kind = "relationship"
            else:
                kind = "relationship"
            classified.append({"uri": uri, "count": cnt, "kind": kind})
        return classified

    @staticmethod
    def _count_class_population_sql(
        store, table: str, class_uri: str, cache: dict = None
    ) -> Optional[int]:
        """Count distinct subjects of a given rdf:type class in the triple store."""
        if not class_uri:
            return None
        if cache is None:
            cache = {}
        key = (table, class_uri)
        if key in cache:
            return cache[key]
        try:
            sql = (
                f"SELECT COUNT(DISTINCT subject) AS cnt FROM {table} "
                f"WHERE predicate = '{RDF_TYPE}' AND object = '{escape_sql_value(class_uri)}'"
            )
            rows = store.execute_query(sql) or []
            total = int(rows[0]["cnt"]) if rows else 0
            cache[key] = total
            return total
        except Exception:
            return None

    @staticmethod
    def _enrich_with_population(result: dict, total_population: Optional[int]) -> dict:
        """Add total_population and pass_pct to a check result dict.

        Uses ``violation_total`` (the true, uncapped count) when present,
        falling back to ``len(violations)`` only when the full result set
        was returned without truncation.
        """
        if total_population is not None and total_population > 0:
            vt = result.get("violation_total")
            violation_count = (
                vt if vt is not None else len(result.get("violations") or [])
            )
            pass_pct = max(
                0.0,
                round(
                    ((total_population - violation_count) / total_population) * 100,
                    1,
                ),
            )
            if violation_count > 0:
                pass_pct = min(pass_pct, 99.9)
            result["total_population"] = total_population
            result["pass_pct"] = pass_pct
            if violation_count > 0:
                result["message"] = (
                    f"{violation_count} violations found — "
                    f"{pass_pct}% pass on {total_population} entities"
                )
        return result

    @staticmethod
    def _count_violations_sql(store, sql: str) -> Optional[int]:
        """Run a ``COUNT(*)`` over a violation SQL to get the true total.

        Only called when the ``LIMIT``-ed result hit the cap, so the
        extra round-trip only happens when needed.
        """
        try:
            count_sql = f"SELECT COUNT(*) AS cnt FROM ({sql.rstrip().rstrip(';')})"
            rows = store.execute_query(count_sql)
            if rows:
                return int(rows[0].get("cnt", 0))
        except Exception as exc:
            logger.warning("COUNT(*) fallback failed: %s", exc)
        return None

    @staticmethod
    def _apply_sql_violation_limit(store, sql: str, violation_limit, row_mapper=None):
        """Execute a violation SQL with optional LIMIT, count the true total, and truncate.

        Returns ``(violations, violation_total, status, message)`` or raises
        on unrecoverable query errors.

        *row_mapper* converts raw rows to violation dicts.  Defaults to
        identity (pass rows through unchanged).
        """
        unlimited_sql = sql
        if violation_limit is not None:
            sql = sql.rstrip().rstrip(";") + f" LIMIT {violation_limit + 1}"
        rows = store.execute_query(sql) or []
        violations = [row_mapper(r) for r in rows] if row_mapper else list(rows)
        violation_total = len(violations)
        if violation_limit is not None and violation_total > violation_limit:
            true_count = SqlQualityChecks._count_violations_sql(store, unlimited_sql)
            if true_count is not None:
                violation_total = true_count
            violations = violations[:violation_limit]
        status = "error" if violation_total > 0 else "success"
        msg = (
            f"{violation_total} violations found"
            if violation_total
            else "No violations"
        )
        return violations, violation_total, status, msg

    @staticmethod
    def _load_predicates_from_table(store, table: str) -> set:
        """Query distinct predicates from the triplestore table for URI resolution."""
        try:
            rows = store.execute_query(f"SELECT DISTINCT predicate FROM {table}") or []
            preds = {r.get("predicate", "") for r in rows if r.get("predicate")}
            logger.info("Loaded %d distinct predicates from %s", len(preds), table)
            return preds
        except Exception as exc:
            logger.warning("Could not load predicates from %s: %s", table, exc)
            return set()

    @staticmethod
    def _resolve_shape_uri_for_sql(shape: dict, available_predicates: set) -> dict:
        """Return a shallow copy of *shape* with property_uri resolved against *available_predicates*."""
        from back.core.w3c import resolve_prop_uri

        prop_uri = shape.get("property_uri", "")
        if not prop_uri or not available_predicates:
            return shape

        resolved = resolve_prop_uri(prop_uri, available_predicates)
        if resolved != prop_uri:
            shape = {**shape, "property_uri": resolved}
            logger.info(
                "SQL DQ: resolved property_uri '%s' → '%s' for shape '%s'",
                prop_uri,
                resolved,
                shape.get("label", shape.get("id", "?")),
            )
        return shape

    @staticmethod
    def _sql_error_detail(exc: Exception) -> str:
        """Return the engine's message as a single line fit for a result row."""
        detail = " ".join(str(exc).split())
        if len(detail) > SqlQualityChecks._SQL_ERROR_MAX_CHARS:
            detail = detail[: SqlQualityChecks._SQL_ERROR_MAX_CHARS].rstrip() + "…"
        return detail or exc.__class__.__name__

    @staticmethod
    def _failed_check_result(
        name: str, category: str, check_id, sql: str, exc: Exception
    ) -> dict:
        """Report a check whose query failed, carrying the cause and the SQL."""
        return {
            "name": name,
            "category": category,
            "shape_id": check_id,
            "status": "warning",
            "message": f"Query failed: {SqlQualityChecks._sql_error_detail(exc)}",
            "violations": [],
            "sql": sql,
        }

    @staticmethod
    def _rule_check_id(prefix: str, rule: dict, index: int) -> str:
        """Return the check id for a non-SHACL *rule*.

        A run that selects individual rules hands over a filtered list, which
        renumbers it. The selector stamps ``check_id`` so a rule with no name
        keeps the id it was picked by rather than picking up its neighbour's.
        """
        return rule.get("check_id") or rule_check_id(prefix, rule, index)

    @staticmethod
    def _swrl_target_class_uri(rule, base_uri, uri_map):
        """Return the class URI of the SWRL violation subject."""
        from back.core.reasoning.swrl.SWRLParser import SWRLParser

        ante_atoms = SWRLParser.parse_atoms(rule.get("antecedent", ""))
        cons_atoms = SWRLParser.parse_atoms(rule.get("consequent", ""))
        class_atoms = [
            a
            for a in ante_atoms
            if a["arity"] == 1 and not a.get("builtin") and not a.get("negated")
        ]
        if not class_atoms:
            return None

        viol_var = SWRLParser.determine_violation_subject(cons_atoms, class_atoms)
        for ca in class_atoms:
            if ca["args"][0] == viol_var:
                return SWRLParser.resolve_uri(ca["name"], base_uri, uri_map)
        return SWRLParser.resolve_uri(class_atoms[0]["name"], base_uri, uri_map)

    @staticmethod
    def _swrl_antecedent_population_sql(translator, store, table, params):
        """Count entities matching the SWRL antecedent (the rule's scope)."""
        try:
            count_sql = translator.build_antecedent_count_sql(table, params)
            if not count_sql:
                return None
            rows = store.execute_query(count_sql) or []
            return int(rows[0]["cnt"]) if rows else None
        except Exception:
            return None

    @staticmethod
    def run_sql_checks(
        tm,
        task,
        shapes,
        triplestore_table,
        store,
        t0,
        total,
        swrl_rules=None,
        ontology=None,
        decision_tables=None,
        aggregate_rules=None,
        violation_limit=None,
    ):
        """Execute SHACL shapes, SWRL, decision tables and aggregate rules as SQL against the VIEW backend."""
        from back.core.w3c import SHACLService

        available_predicates = SqlQualityChecks._load_predicates_from_table(
            store, triplestore_table
        )

        pop_cache = {}
        results = []
        for idx, shape in enumerate(shapes):
            label = shape.get("label", shape.get("id", f"Shape {idx + 1}"))
            cat = shape.get("category", "unknown")
            progress = int((idx / total) * 100)
            tm.update_progress(task.id, progress, f"Check {idx + 1}/{total}: {label}")

            resolved_shape = SqlQualityChecks._resolve_shape_uri_for_sql(
                shape, available_predicates
            )
            sql = SHACLService.shape_to_sql(resolved_shape, triplestore_table)
            if not sql:
                results.append(
                    {
                        "name": label,
                        "category": cat,
                        "shape_id": shape.get("id"),
                        "status": "info",
                        "message": "Cannot translate to SQL",
                        "violations": [],
                        "sql": "",
                    }
                )
                continue

            try:
                violations, violation_total, status, msg = (
                    SqlQualityChecks._apply_sql_violation_limit(
                        store,
                        sql,
                        violation_limit,
                    )
                )
                result = {
                    "name": label,
                    "category": cat,
                    "shape_id": shape.get("id"),
                    "status": status,
                    "message": msg,
                    "violations": violations,
                    "sql": sql,
                    "violation_total": violation_total,
                    "severity": shape.get("severity", "sh:Violation"),
                }
                class_uri = shape.get("target_class_uri", "")
                pop = SqlQualityChecks._count_class_population_sql(
                    store, triplestore_table, class_uri, pop_cache
                )
                SqlQualityChecks._enrich_with_population(result, pop)
                results.append(result)
            except Exception as exc:
                err = str(exc)
                if "TABLE_OR_VIEW_NOT_FOUND" in err or "does not exist" in err.lower():
                    tm.fail_task(
                        task.id, f"View {triplestore_table} not found. Build first."
                    )
                    return
                logger.exception("SQL DQ check '%s' failed: %s", label, exc)
                results.append(
                    SqlQualityChecks._failed_check_result(
                        label, cat, shape.get("id"), sql, exc
                    )
                )

        SqlQualityChecks._run_swrl_sql_checks(
            tm,
            task,
            results,
            swrl_rules,
            ontology,
            triplestore_table,
            store,
            total,
            violation_limit=violation_limit,
            offset=len(shapes),
        )

        swrl_count = len(swrl_rules) if swrl_rules else 0
        dt_count = len(decision_tables) if decision_tables else 0
        dt_offset = len(shapes) + swrl_count
        SqlQualityChecks._run_dt_sql_checks(
            tm,
            task,
            results,
            decision_tables,
            ontology,
            triplestore_table,
            store,
            total,
            dt_offset,
            violation_limit=violation_limit,
        )

        agg_offset = dt_offset + dt_count
        SqlQualityChecks._run_agg_sql_checks(
            tm,
            task,
            results,
            aggregate_rules,
            ontology,
            triplestore_table,
            store,
            total,
            agg_offset,
            violation_limit=violation_limit,
        )

        _dt().complete_dq_task(tm, task, results, time.time() - t0)

    @staticmethod
    def _run_swrl_sql_checks(
        tm,
        task,
        results,
        swrl_rules,
        ontology,
        triplestore_table,
        store,
        total,
        violation_limit=None,
        offset=0,
    ):
        if not swrl_rules:
            return
        from back.core.reasoning.swrl.SWRLSQLTranslator import SWRLSQLTranslator
        from back.core.reasoning.swrl.SWRLEngine import SWRLEngine

        translator = SWRLSQLTranslator()
        ontology = ontology or {}
        base_uri = ontology.get("base_uri", "")
        engine = SWRLEngine(ontology=ontology)
        uri_map = engine._build_uri_map()
        for idx, rule in enumerate(swrl_rules):
            if not rule.get("enabled", True):
                continue
            label = rule.get("name", f"SWRL Rule {idx + 1}")
            check_id = SqlQualityChecks._rule_check_id(SWRL_ID_PREFIX, rule, idx)
            progress = int(((offset + idx) / total) * 100)
            tm.update_progress(
                task.id, progress, f"SWRL {idx + 1}/{len(swrl_rules)}: {label}"
            )
            params = {
                "antecedent": rule.get("antecedent", ""),
                "consequent": rule.get("consequent", ""),
                "base_uri": base_uri,
                "uri_map": uri_map,
            }
            sql = translator.build_violation_sql(triplestore_table, params)
            if not sql:
                results.append(
                    {
                        "name": label,
                        "category": "structural",
                        "shape_id": check_id,
                        "status": "info",
                        "message": "Cannot translate to SQL",
                        "violations": [],
                        "sql": "",
                    }
                )
                continue
            _s_mapper = lambda r: {"s": r.get("s", "")}
            try:
                t_rule = time.time()
                violations, violation_total, status, msg = (
                    SqlQualityChecks._apply_sql_violation_limit(
                        store,
                        sql,
                        violation_limit,
                        row_mapper=_s_mapper,
                    )
                )
                elapsed_rule = time.time() - t_rule
                result = {
                    "name": label,
                    "category": "structural",
                    "shape_id": check_id,
                    "status": status,
                    "message": msg,
                    "violations": violations,
                    "sql": "",
                    "severity": "sh:Violation",
                    "violation_total": violation_total,
                }
                pop = None
                if violations:
                    pop = SqlQualityChecks._swrl_antecedent_population_sql(
                        translator, store, triplestore_table, params
                    )
                SqlQualityChecks._enrich_with_population(result, pop)
                logger.info(
                    "SWRL rule '%s': %d violations (%.2fs)",
                    label,
                    violation_total,
                    elapsed_rule,
                )
                results.append(result)
            except Exception as exc:
                logger.exception("SWRL DQ check '%s' SQL failed: %s", label, exc)
                results.append(
                    SqlQualityChecks._failed_check_result(
                        label, "structural", check_id, sql, exc
                    )
                )

    @staticmethod
    def _run_dt_sql_checks(
        tm,
        task,
        results,
        decision_tables,
        ontology,
        triplestore_table,
        store,
        total,
        shape_count,
        violation_limit=None,
    ):
        if not decision_tables:
            return
        from back.core.reasoning.rules.DecisionTableEngine import DecisionTableEngine

        engine = DecisionTableEngine()
        ontology = ontology or {}
        base_uri = ontology.get("base_uri", "")
        uri_map = engine._build_uri_map(ontology)
        for idx, dt in enumerate(decision_tables):
            if not dt.get("enabled", True):
                continue
            dt_name = dt.get("name", f"Decision Table {idx + 1}")
            check_id = SqlQualityChecks._rule_check_id(DECISION_TABLE_ID_PREFIX, dt, idx)
            progress = int(((shape_count + idx) / total) * 100)
            tm.update_progress(
                task.id, progress, f"DT {idx + 1}/{len(decision_tables)}: {dt_name}"
            )
            resolved = engine._resolve_dt(dt, uri_map, base_uri)
            sql = engine.build_violation_sql(resolved, triplestore_table, base_uri)
            if not sql:
                results.append(
                    {
                        "name": dt_name,
                        "category": "conformance",
                        "shape_id": check_id,
                        "status": "info",
                        "message": "Cannot translate to SQL",
                        "violations": [],
                        "sql": "",
                    }
                )
                continue
            _s_mapper = lambda r: {"s": r.get("s", "")}
            try:
                t_rule = time.time()
                violations, violation_total, status, msg = (
                    SqlQualityChecks._apply_sql_violation_limit(
                        store,
                        sql,
                        violation_limit,
                        row_mapper=_s_mapper,
                    )
                )
                elapsed_rule = time.time() - t_rule
                result = {
                    "name": dt_name,
                    "category": "conformance",
                    "shape_id": check_id,
                    "status": status,
                    "message": msg,
                    "violations": violations,
                    "sql": sql,
                    "severity": "sh:Violation",
                    "violation_total": violation_total,
                }
                pop = None
                if violations:
                    class_uri = resolved.get("target_class_uri", "")
                    pop_cache: dict = {}
                    pop = SqlQualityChecks._count_class_population_sql(
                        store, triplestore_table, class_uri, pop_cache
                    )
                SqlQualityChecks._enrich_with_population(result, pop)
                logger.info(
                    "DT rule '%s': %d violations (%.2fs)",
                    dt_name,
                    violation_total,
                    elapsed_rule,
                )
                results.append(result)
            except Exception as exc:
                logger.exception(
                    "Decision table DQ check '%s' SQL failed: %s", dt_name, exc
                )
                results.append(
                    SqlQualityChecks._failed_check_result(
                        dt_name, "conformance", check_id, sql, exc
                    )
                )

    @staticmethod
    def _run_agg_sql_checks(
        tm,
        task,
        results,
        aggregate_rules,
        ontology,
        triplestore_table,
        store,
        total,
        shape_count,
        violation_limit=None,
    ):
        if not aggregate_rules:
            return
        from back.core.reasoning.rules.AggregateRuleEngine import AggregateRuleEngine

        engine = AggregateRuleEngine()
        ontology = ontology or {}
        base_uri = ontology.get("base_uri", "")
        pop_cache: dict = {}
        for idx, rule in enumerate(aggregate_rules):
            if not rule.get("enabled", True):
                continue
            agg_name = rule.get("name", f"Aggregate Rule {idx + 1}")
            check_id = SqlQualityChecks._rule_check_id(AGGREGATE_ID_PREFIX, rule, idx)
            progress = int(((shape_count + idx) / total) * 100)
            tm.update_progress(
                task.id, progress, f"Agg {idx + 1}/{len(aggregate_rules)}: {agg_name}"
            )
            resolved = engine._resolve_rule(dict(rule), ontology)
            sql = engine.build_sql(resolved, triplestore_table, base_uri)
            if not sql:
                results.append(
                    {
                        "name": agg_name,
                        "category": "conformance",
                        "shape_id": check_id,
                        "status": "info",
                        "message": "Cannot translate to SQL",
                        "violations": [],
                        "sql": "",
                    }
                )
                continue
            _agg_mapper = lambda r: {
                "s": r.get("s", ""),
                "agg_val": r.get("agg_val", ""),
            }
            try:
                t_rule = time.time()
                violations, violation_total, status, msg = (
                    SqlQualityChecks._apply_sql_violation_limit(
                        store,
                        sql,
                        violation_limit,
                        row_mapper=_agg_mapper,
                    )
                )
                elapsed_rule = time.time() - t_rule
                result = {
                    "name": agg_name,
                    "category": "conformance",
                    "shape_id": check_id,
                    "status": status,
                    "message": msg,
                    "violations": violations,
                    "sql": sql,
                    "severity": "sh:Violation",
                    "violation_total": violation_total,
                }
                pop = None
                if violations:
                    class_uri = resolved.get("target_class_uri", "")
                    pop = SqlQualityChecks._count_class_population_sql(
                        store, triplestore_table, class_uri, pop_cache
                    )
                SqlQualityChecks._enrich_with_population(result, pop)
                logger.info(
                    "Agg rule '%s': %d violations (%.2fs)",
                    agg_name,
                    violation_total,
                    elapsed_rule,
                )
                results.append(result)
            except Exception as exc:
                logger.exception(
                    "Aggregate rule DQ check '%s' SQL failed: %s", agg_name, exc
                )
                results.append(
                    SqlQualityChecks._failed_check_result(
                        agg_name, "conformance", check_id, sql, exc
                    )
                )
