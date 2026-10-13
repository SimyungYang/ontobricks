"""While GitHub settings load, only the spinner is visible.

The form, Test connection / Sync all toolbar, info banner and section
header used to paint immediately while GET /settings/github was still
in flight (often >2s). They now live in ``#githubLoaded``, which starts
hidden until the fetch completes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "src/front/templates/partials/settings/_settings_github.html"
SCRIPT = ROOT / "src/front/static/config/js/settings-github.js"


@pytest.fixture(scope="module")
def template() -> str:
    return TEMPLATE.read_text()


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text()


def _between(html: str, start_id: str, end_id: str) -> str:
    start = html.index(f'id="{start_id}"')
    end = html.index(f'id="{end_id}"')
    return html[start:end]


def test_loaded_body_starts_hidden(template):
    assert 'id="githubLoaded" class="d-none"' in template
    assert 'id="githubLoading"' in template
    assert "Loading GitHub settings" in template


def test_toolbar_and_form_live_inside_the_hidden_loaded_body(template):
    before_loaded = _between(template, "githubLoading", "githubLoaded")
    loaded = template[template.index('id="githubLoaded"') :]

    assert "btnTestGithub" not in before_loaded
    assert "githubConfigForm" not in before_loaded
    assert "btnSaveGithub" not in before_loaded
    assert "One repository per instance" not in before_loaded

    assert 'id="btnTestGithub"' in loaded
    assert 'id="githubConfigForm"' in loaded
    assert 'id="btnSaveGithub"' in loaded
    assert 'id="btnSyncAllGithub"' in loaded
    assert 'id="githubStatus"' in loaded
    assert "One repository per instance" in loaded


def test_script_reveals_the_loaded_body_only_after_fetch(script):
    assert "$('githubLoading')?.classList.add('d-none')" in script
    assert "$('githubLoaded')?.classList.remove('d-none')" in script
    assert "form.classList.remove('d-none')" not in script
