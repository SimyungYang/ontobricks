"""filter_expand must not ship edges to entities outside the BFS set.

The graph viewer draws every edge target as a node, so edges pointing past
the ``max_entities`` cap made the explorer render far more nodes than asked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from back.objects.digitaltwin import DigitalTwin
from back.objects.digitaltwin.constants import RDF_TYPE, RDFS_LABEL

B = "https://example.org/"


def _store(neighbors, triples):
    store = MagicMock()
    store.expand_and_fetch_subgraph = None  # exercise the batched path
    store.expand_entity_neighbors.return_value = set(neighbors)
    store.get_triples_for_subjects.side_effect = lambda _g, subs: [
        t for t in triples if t["subject"] in subs
    ]
    return store


def test_edges_to_entities_beyond_cap_are_dropped():
    seed, kept, beyond = B + "Sale/1", B + "Customer/1", B + "Product/9"
    triples = [
        {"subject": seed, "predicate": B + "soldTo", "object": kept},
        {"subject": kept, "predicate": B + "likes", "object": beyond},
    ]
    out = DigitalTwin.filter_expand(
        _store({kept}, triples), "g", [seed], depth=1, max_entities=2
    )
    objects = {r["object"] for r in out["results"]}
    assert kept in objects
    assert beyond not in objects


def test_literals_and_types_are_kept():
    seed = B + "Sale/1"
    triples = [
        {"subject": seed, "predicate": RDF_TYPE, "object": B + "Sale"},
        {"subject": seed, "predicate": RDFS_LABEL, "object": "sale one"},
        {"subject": seed, "predicate": B + "amount", "object": "100"},
    ]
    out = DigitalTwin.filter_expand(
        _store(set(), triples), "g", [seed], depth=1, max_entities=50
    )
    assert len(out["results"]) == 3


def test_single_statement_expand_drops_edges_beyond_the_set():
    seed, kept, beyond = B + "Sale/1", B + "Customer/1", B + "Product/9"
    store = MagicMock()
    store.expand_and_fetch_subgraph.return_value = {
        "results": [
            {"subject": seed, "predicate": B + "soldTo", "object": kept},
            {"subject": kept, "predicate": B + "label", "object": "c one"},
            {"subject": kept, "predicate": B + "likes", "object": beyond},
        ],
        "count": 3,
    }
    out = DigitalTwin.filter_expand(store, "g", [seed], depth=1, max_entities=2)
    objects = {r["object"] for r in out["results"]}
    assert kept in objects and beyond not in objects and out["count"] == 2
