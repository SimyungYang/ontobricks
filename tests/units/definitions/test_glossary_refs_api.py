"""Manual UC Page references via the entity edit API, and the read endpoint."""

from types import SimpleNamespace

import pytest

from back.core.errors import NotFoundError, ValidationError
from back.objects.ontology import Editor
from back.objects.ontology.definitions import DefinitionReferences
from tests.units.definitions.conftest import SALES_URI, build_ontology

pytestmark = pytest.mark.unit


@pytest.fixture
def session(domain_session):
    domain_session._data["ontology"].update(build_ontology())
    return domain_session


def _items(session):
    return DefinitionReferences.items(session.ontology)


def test_class_glossary_ref_is_set(session):
    Editor(session).update_class({"uri": SALES_URI, "glossary_ref": "page-1"})
    assert _items(session)[SALES_URI]["glossary_ref"] == "page-1"


def test_attribute_glossary_ref_is_set(session):
    Editor(session).update_class(
        {"uri": SALES_URI, "attribute_glossary_refs": {"totalRevenue": "page-2"}}
    )
    assert _items(session)[f"{SALES_URI}|totalRevenue"]["glossary_ref"] == "page-2"


def test_empty_value_clears_the_reference(session):
    editor = Editor(session)
    editor.update_class({"uri": SALES_URI, "glossary_ref": "page-1"})
    editor.update_class({"uri": SALES_URI, "glossary_ref": ""})
    assert SALES_URI not in _items(session)


def test_unknown_attribute_is_rejected_without_side_effects(session):
    with pytest.raises(NotFoundError):
        Editor(session).update_class(
            {"uri": SALES_URI, "glossary_ref": "p", "attribute_glossary_refs": {"nope": "x"}}
        )
    assert SALES_URI not in _items(session)


def test_too_long_reference_is_rejected(session):
    with pytest.raises(ValidationError):
        Editor(session).update_class({"uri": SALES_URI, "glossary_ref": "x" * 3000})


def test_referenced_class_text_stays_uc_owned(session):
    DefinitionReferences.set_glossary_ref(session.ontology, SALES_URI, "", "page-1")
    _items(session)[SALES_URI]["synced"] = {
        "page": {"definition": "UC text", "synonyms": ["u"], "source": "uc_page:page-1"}
    }
    Editor(session).update_class({"uri": SALES_URI, "description": "user edit"})
    assert session.ontology["classes"][0]["description"] == "UC text"


def test_non_referenced_class_keeps_manual_editing(session):
    Editor(session).update_class({"uri": SALES_URI, "description": "user edit"})
    assert session.ontology["classes"][0]["description"] == "user edit"


async def test_definitions_endpoint_lists_references(monkeypatch, session):
    from api.routers.internal import ontology as routes

    DefinitionReferences.set_glossary_ref(session.ontology, SALES_URI, "", "page-1")
    monkeypatch.setattr(routes, "get_domain", lambda mgr: session)
    body = await routes.get_definition_references(session_mgr=SimpleNamespace())
    assert [i["key"] for i in body["items"]] == [SALES_URI]
