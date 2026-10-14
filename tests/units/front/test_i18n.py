"""Contracts for the UI internationalisation layer (English / Korean).

The UI is authored in English. ``global/js/i18n.js`` translates server
markup and script-generated text on the client using the flat dictionary in
``global/js/i18n-ko.js``; the server only decides ``<html lang>`` from the
``ob_lang`` cookie so the first paint already knows the language.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from front.fastapi.dependencies import SUPPORTED_UI_LANGS, resolve_ui_lang

REPO_ROOT = Path(__file__).resolve().parents[3]
STATIC_JS = REPO_ROOT / "src/front/static/global/js"
I18N_JS = STATIC_JS / "i18n.js"
KO_JS = STATIC_JS / "i18n-ko.js"
I18N_CSS = REPO_ROOT / "src/front/static/global/css/i18n.css"
BASE_HTML = REPO_ROOT / "src/front/templates/base.html"
MENU_CONFIG = REPO_ROOT / "src/front/config/menu_config.json"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the i18n module"
)


def _ko_dict() -> dict:
    src = KO_JS.read_text(encoding="utf-8")
    body = src[src.index("= {") + 2 : src.rindex("}") + 1]
    return json.loads(body)


_HARNESS = """
const fs = require('fs');
global.window = {};
global.document = { addEventListener() {} };
eval(fs.readFileSync(__SOURCE__, 'utf8'));
const core = window.OB_I18N_CORE;
const dict = __DICT__;
const out = __INPUTS__.map(s => {
    const hit = core.translateWith(dict, s);
    return hit === null ? null : core.keepPadding(s, hit);
});
process.stdout.write(JSON.stringify(out));
"""


def _translate(dict_: dict, inputs: list) -> list:
    script = (
        _HARNESS.replace("__SOURCE__", json.dumps(str(I18N_JS)))
        .replace("__DICT__", json.dumps(dict_, ensure_ascii=False))
        .replace("__INPUTS__", json.dumps(inputs, ensure_ascii=False))
    )
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# Product / standard names shown as-is in every language.
KEEP_IN_ENGLISH = {
    "API", "Databricks", "Lakebase", "Lakehouse", "Neo4j", "OWL",
    "OntoBricks", "R2RML", "Spark SQL", "UI",
}

SAMPLE = {
    "Save": "저장",
    "{n} entities": "엔티티 {n}개",
    "Error deleting relationship:": "관계 삭제 오류:",
    "Loading": "불러오는 중",
}


# ---------------------------------------------------------------- server side


def test_supported_languages_are_english_and_korean():
    assert SUPPORTED_UI_LANGS == ("en", "ko")


@pytest.mark.parametrize("cookie,expected", [("ko", "ko"), ("en", "en"), (None, "en"), ("fr", "en"), ("", "en")])
def test_resolve_ui_lang_accepts_only_supported_cookie_values(cookie, expected):
    assert resolve_ui_lang(cookie) == expected


# ---------------------------------------------------------------- translation core


@needs_node
def test_exact_match_keeps_surrounding_whitespace():
    assert _translate(SAMPLE, ["  Save "]) == ["  저장 "]


@needs_node
def test_internal_whitespace_is_normalised_before_lookup():
    assert _translate(SAMPLE, ["12\n   entities"]) == ["엔티티 12개"]


@needs_node
def test_number_template_substitutes_digits():
    assert _translate(SAMPLE, ["42 entities"]) == ["엔티티 42개"]


@needs_node
def test_known_prefix_keeps_dynamic_detail_verbatim():
    assert _translate(SAMPLE, ["Error deleting relationship: HTTP 500"]) == ["관계 삭제 오류: HTTP 500"]


@needs_node
def test_trailing_punctuation_falls_back_to_core_text():
    assert _translate(SAMPLE, ["Loading..."]) == ["불러오는 중..."]


@needs_node
def test_unknown_text_and_non_latin_text_are_left_alone():
    assert _translate(SAMPLE, ["Customer_42_xyz", "한국어", "123"]) == [None, None, None]


# ---------------------------------------------------------------- dictionary


def test_korean_dictionary_is_valid_and_non_trivial():
    ko = _ko_dict()
    assert len(ko) > 1000
    assert all(isinstance(v, str) and v.strip() for v in ko.values())


def test_korean_dictionary_preserves_number_placeholders():
    bad = [k for k, v in _ko_dict().items() if k.count("{n}") != v.count("{n}")]
    assert bad == []


def test_korean_dictionary_keys_are_normalised():
    bad = [k for k in _ko_dict() if k != re.sub(r"\s+", " ", k).strip()]
    assert bad == []


def test_every_menu_label_has_a_korean_translation():
    labels = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("label", "title") and isinstance(value, str):
                    labels.add(re.sub(r"\s+", " ", value).strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(MENU_CONFIG.read_text(encoding="utf-8")))
    ko = _ko_dict()
    missing = sorted(label for label in labels - KEEP_IN_ENGLISH if label and label not in ko)
    assert missing == []


@pytest.mark.parametrize(
    "english,korean",
    [
        ("Ontology", "온톨로지"),
        ("Mapping", "매핑"),
        ("Knowledge Graph", "지식 그래프"),
        ("Registry", "레지스트리"),
        ("Settings", "설정"),
        ("Save", "저장"),
        ("Cancel", "취소"),
    ],
)
def test_core_glossary_terms(english, korean):
    assert _ko_dict()[english] == korean


# ---------------------------------------------------------------- wiring


def test_base_html_renders_lang_from_server():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert '<html lang="{{ get_ui_lang() }}"' in html


def test_i18n_scripts_load_after_permissions_and_before_navbar():
    html = BASE_HTML.read_text(encoding="utf-8")
    perms = html.index("global/js/permissions.js")
    ko = html.index("global/js/i18n-ko.js")
    core = html.index("global/js/i18n.js")
    navbar = html.index("global/js/navbar.js")
    assert perms < ko < core < navbar


def test_i18n_css_loads_before_permissions_css():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert html.index("global/css/i18n.css") < html.index("global/css/permissions.css")


def test_user_menu_offers_both_languages():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert 'data-ob-lang="en"' in html
    assert 'data-ob-lang="ko"' in html


def test_hidden_until_translated_has_a_reveal_failsafe():
    css = I18N_CSS.read_text(encoding="utf-8")
    assert ':not(.ob-i18n-ready) body' in css
    assert "animation: ob-i18n-reveal" in css


def test_user_content_regions_are_never_translated():
    js = I18N_JS.read_text(encoding="utf-8")
    for selector in ("'code'", "'pre'", "'textarea'", "'svg'", "'[translate=\"no\"]'", "'[data-i18n-skip]'"):
        assert selector in js


def test_admin_only_navbar_dropdown_items_keep_flex_layout():
    # permissions.css reveals admin-only items with display:block, which broke
    # the icon + label flex row of navbar dropdowns (uneven icon spacing).
    css = (REPO_ROOT / "src/front/static/global/css/permissions.css").read_text(encoding="utf-8")
    rule = (
        'body[data-app-role="admin"] .navbar .dropdown-menu .dropdown-item[data-requires-app="admin"]'
    )
    start = css.index(rule)
    block = css[start : css.index("}", start)]
    assert "display: flex !important;" in block


def test_settings_ui_section_has_language_selector():
    html = (REPO_ROOT / "src/front/templates/settings.html").read_text(encoding="utf-8")
    ui = html[html.index('id="ui-section"') :]
    # Language sits right below Branding and above Theme.
    assert ui.index("Branding</h5>") < ui.index('id="uiLanguageCard"') < ui.index("Theme</h5>")
    card = ui[ui.index('id="uiLanguageCard"') : ui.index("Theme</h5>")]
    assert "data-ob-lang-select" in card
    assert '<option value="en" translate="no">English</option>' in card
    assert '<option value="ko" translate="no">한국어</option>' in card


def test_language_select_switches_on_change():
    js = I18N_JS.read_text(encoding="utf-8")
    assert "[data-ob-lang-select]" in js
    assert "addEventListener('change'" in js


def test_settings_ui_cards_do_not_scroll_internally():
    css = (REPO_ROOT / "src/front/static/global/css/config.css").read_text(encoding="utf-8")
    start = css.index("#ui-section > .content-section > .card {")
    block = css[start : css.index("}", start)]
    assert "flex: 0 0 auto;" in block
    assert "overflow: visible;" in block
