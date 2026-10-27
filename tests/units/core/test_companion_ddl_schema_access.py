"""Unit tests for the graph-schema access check in the Lakebase companion DDL.

Postgres silently drops a schema the session cannot ``USAGE`` from the
``search_path``. When the graph schema is owned by another role, an unqualified
``CREATE TABLE`` then lands in ``public`` and fails with ``permission denied for
schema public`` — which names the wrong schema. The check fails first with the
real cause and the fix.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from back.core.errors import InfrastructureError
from back.core.graphdb.lakebase._companion_ddl import (
    ensure_companion,
    ensure_schema_writable,
)


def _statements(cur: MagicMock) -> list[str]:
    return [str(c.args[0]) for c in cur.execute.call_args_list]


@pytest.mark.unit
class TestEnsureSchemaWritable:
    def test_checks_usage_and_create_on_the_graph_schema(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = {"?column?": 1}  # dict_row cursor

        ensure_schema_writable(cur, "graph_schema")

        sql, params = cur.execute.call_args.args
        assert "has_schema_privilege" in sql
        assert "'USAGE'" in sql and "'CREATE'" in sql
        assert params == ("graph_schema", "graph_schema")

    def test_raises_with_schema_name_and_fix_when_access_is_missing(self) -> None:
        cur = MagicMock()
        cur.fetchone.return_value = None

        with pytest.raises(InfrastructureError, match="graph_schema") as exc:
            ensure_schema_writable(cur, "graph_schema")
        assert "lakebase-perms.sh -i <project> -b <branch> -d <database> -s graph_schema" in str(
            exc.value
        )
        # make bootstrap-lakebase grants on the registry schema, not the graph schema.
        assert "bootstrap-lakebase" not in str(exc.value)


@pytest.mark.unit
def test_companion_is_not_created_when_schema_is_not_writable() -> None:
    cur = MagicMock()
    # pgcrypto digest() resolves, then the schema access check fails.
    cur.fetchone.side_effect = [(1,), None]

    with pytest.raises(InfrastructureError):
        ensure_companion(cur, "graph_schema", "domain_v1__app")
    assert not any("CREATE TABLE" in s for s in _statements(cur))
