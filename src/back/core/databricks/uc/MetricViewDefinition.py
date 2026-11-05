"""Parse the YAML definition of a Unity Catalog metric view. No I/O."""

import re
import textwrap
from typing import Any, Dict, List, Set

from back.core.logging import get_logger

logger = get_logger(__name__)

# ``SHOW CREATE TABLE`` wraps the metric YAML in ``AS $$ ... $$``.
_YAML_BODY_RE = re.compile(r"\$\$(.*)\$\$", re.DOTALL)

_SECTIONS = (("dimensions", "dimension"), ("measures", "measure"))


class MetricViewDefinition:
    """Extract per-column ``expr`` / ``comment`` / ``synonyms`` from a metric view DDL."""

    @staticmethod
    def parse(ddl: str) -> Dict[str, Dict[str, Any]]:
        """Return ``{column_name: {name, role, expr, comment, synonyms, display_name}}``.

        Accepts the full ``SHOW CREATE TABLE`` output or the bare YAML. Soft-fails
        to ``{}`` on a missing or malformed definition so a metadata load never
        crashes on an unexpected DDL.
        """
        document = MetricViewDefinition._load_yaml(ddl)
        columns: Dict[str, Dict[str, Any]] = {}
        for section, role in _SECTIONS:
            for item in document.get(section) or []:
                entry = MetricViewDefinition._column_entry(item, role)
                if entry:
                    columns[entry["name"]] = entry
        return columns

    @staticmethod
    def measure_names(ddl: str) -> Set[str]:
        """Return the names declared under the ``measures:`` block."""
        return {
            name
            for name, entry in MetricViewDefinition.parse(ddl).items()
            if entry["role"] == "measure"
        }

    @staticmethod
    def _load_yaml(ddl: str) -> Dict[str, Any]:
        text = ddl or ""
        match = _YAML_BODY_RE.search(text)
        body = textwrap.dedent(match.group(1) if match else text)
        if not body.strip():
            return {}
        try:
            import yaml  # transitive runtime dependency (mlflow)

            document = yaml.safe_load(body)
        except Exception as exc:  # noqa: BLE001 - soft-fail is the contract
            logger.warning("metric view YAML is not parseable: %s", exc)
            return {}
        return document if isinstance(document, dict) else {}

    @staticmethod
    def _column_entry(item: Any, role: str) -> Dict[str, Any]:
        if not isinstance(item, dict) or not item.get("name"):
            return {}
        return {
            "name": str(item["name"]),
            "role": role,
            "expr": str(item.get("expr") or ""),
            "comment": str(item.get("comment") or ""),
            "synonyms": MetricViewDefinition._as_list(item.get("synonyms")),
            "display_name": str(item.get("display_name") or ""),
        }

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(v).strip() for v in value if str(v or "").strip()]
