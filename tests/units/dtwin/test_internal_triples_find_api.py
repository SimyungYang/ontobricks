"""Backward-compatibility guards for internal /dtwin/triples/find."""

from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.unit


@patch("api.routers.internal.dtwin._graph_query_table", return_value="c.s.graph_V1")
@patch("api.routers.internal.dtwin._require_graph_store")
@patch("api.routers.internal.dtwin.get_domain")
@patch("api.routers.internal.dtwin.DigitalTwin.find_triples_bfs")
async def test_internal_find_missing_has_more_defaults_to_false(
    mock_find,
    mock_get_domain,
    mock_require_store,
    _query_table,
):
    from api.routers.internal.dtwin import dtwin_triples_find

    mock_get_domain.return_value = MagicMock()
    mock_require_store.return_value = MagicMock()
    mock_find.return_value = {
        "seed_count": 1,
        "triples": [{"subject": "s", "predicate": "p", "object": "o"}],
        "count": 1,
        "total": 1,
        "entity_count": 1,
    }

    payload = await dtwin_triples_find(
        search="cust",
        session_mgr=MagicMock(),
        settings=MagicMock(),
    )

    assert payload["has_more"] is False
