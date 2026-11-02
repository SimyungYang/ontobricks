"""The graph engine reuses one Lakebase auth per explicit branch.

With ``graph_engine_config.lakebase_branch`` set, every request built a new
:class:`BranchLakebaseAuth`, so each explorer search or chat call created a
WorkspaceClient and re-resolved the endpoint host (and token) over the REST
API before its query. ``get_branch_lakebase_auth`` keeps one instance per
``(branch, database)`` per process, like ``get_lakebase_auth`` does for the
bound instance.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from back.core.databricks.lakebase import BranchLakebaseAuth, get_branch_lakebase_auth
from back.core.graphdb.GraphDBFactory import GraphDBFactory
# The package re-exports the class under the module's name, so fetch the module itself.
_auth_mod = importlib.import_module("back.core.databricks.lakebase.BranchLakebaseAuth")

BRANCH = "projects/p/branches/production"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    _auth_mod._branch_auths.clear()
    yield
    _auth_mod._branch_auths.clear()


def test_same_branch_and_database_share_one_instance():
    first = get_branch_lakebase_auth(BRANCH, "graph_db")
    assert isinstance(first, BranchLakebaseAuth)
    assert get_branch_lakebase_auth(f" {BRANCH} ", " graph_db ") is first


def test_a_different_database_gets_its_own_instance():
    assert get_branch_lakebase_auth(BRANCH, "a") is not get_branch_lakebase_auth(BRANCH, "b")


def test_reused_instance_resolves_the_endpoint_once():
    api = MagicMock()
    api.do.return_value = {"endpoints": [{
        "name": f"{BRANCH}/endpoints/primary",
        "status": {"hosts": {"host": "ep.example.com"}},
    }]}
    for _ in range(3):
        auth = get_branch_lakebase_auth(BRANCH)
        auth._w = SimpleNamespace(api_client=api)
        assert auth.host == "ep.example.com"
    assert api.do.call_count == 1


def _create(factory):
    domain = SimpleNamespace(settings={"registry": {}}, info={"name": "Dom"})
    settings = SimpleNamespace(
        registry_catalog="", registry_schema="", registry_volume="",
        lakebase_schema="ontobricks_registry", lakebase_database="", registry_volume_path="",
    )
    factory.create(domain, settings, engine="lakebase",
                   engine_config={"schema": "g", "lakebase_branch": BRANCH, "database": "gdb"})


def test_factory_hands_every_store_the_same_branch_auth():
    factory = GraphDBFactory()
    with (
        patch("back.core.graphdb.lakebase.LAKEBASE_AVAILABLE", True),
        patch.object(BranchLakebaseAuth, "_resolved_host", return_value="ep.example.com"),
        patch("back.objects.registry.RegistryCfg.RegistryCfg.from_domain",
              return_value=MagicMock(catalog="c", schema="s", volume="v")),
        patch("back.core.graphdb.lakebase.LakebaseFlatStore.LakebaseFlatStore") as store_cls,
    ):
        store_cls.return_value = MagicMock()
        _create(factory)
        _create(factory)
    first, second = (c.args[0] for c in store_cls.call_args_list)
    assert first is second
    assert first is get_branch_lakebase_auth(BRANCH, "gdb")
