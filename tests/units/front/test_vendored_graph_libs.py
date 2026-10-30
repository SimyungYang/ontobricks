"""Graph and GraphQL libraries are served by the app, not by public CDNs.

Corporate networks block hosts such as ``d3js.org``; the browser then never
defines ``d3`` and the ontology map, explorer and GraphQL views break. These
libraries ship under ``static/global/vendor/`` instead.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONT = REPO_ROOT / "src/front"
STATIC = FRONT / "static"

BLOCKABLE_CDN_HOSTS = ("d3js.org", "cdnjs.cloudflare.com", "unpkg.com")
GRAPH_LIB_PACKAGES = ("d3@", "graphology", "sigma", "graphiql", "react@", "react-dom@")

VENDOR_REF = re.compile(r"/static/(global/vendor/[^'\"?\s)]+)")
URL_FOR_VENDOR_REF = re.compile(r"filename='(global/vendor/[^']+)'")

pytestmark = pytest.mark.unit


def _front_sources():
    for path in sorted(FRONT.rglob("*")):
        if path.suffix in {".html", ".js"} and "/vendor/" not in path.as_posix():
            yield path


def test_no_front_source_loads_from_a_blockable_cdn():
    offenders = [
        f"{path.relative_to(FRONT)}: {host}"
        for path in _front_sources()
        for host in BLOCKABLE_CDN_HOSTS
        if host in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_front_source_loads_graph_libs_from_jsdelivr():
    offenders = [
        f"{path.relative_to(FRONT)}: {url}"
        for path in _front_sources()
        for url in re.findall(r"https://cdn\.jsdelivr\.net/npm/[^'\"\s]+",
                              path.read_text(encoding="utf-8"))
        if any(pkg in url for pkg in GRAPH_LIB_PACKAGES)
    ]
    assert offenders == []


def test_every_vendor_reference_points_to_a_shipped_file():
    refs = set()
    for path in _front_sources():
        text = path.read_text(encoding="utf-8")
        refs.update(VENDOR_REF.findall(text))
        refs.update(URL_FOR_VENDOR_REF.findall(text))
    missing = sorted(ref for ref in refs if not (STATIC / ref).is_file())
    assert refs and missing == []
