"""Bold closes before a Korean particle in rendered Markdown.

CommonMark only closes ``**`` when the delimiter run is right-flanking: after
punctuation it must be followed by whitespace or punctuation. Korean particles
attach directly, so ``**[대구지점](/resolve?uri=…)**이`` and ``**대구지점(E1)**이``
stayed literal in Graph Chat replies. ``renderMarkdown`` installs an inline
rule that closes a same-line ``**…**`` pair regardless of flanking; these tests
run the matcher and the marked wiring under node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UTILS_JS = REPO_ROOT / "src/front/static/global/js/utils.js"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("node") is None, reason="node is required"),
]

_HARNESS = """
const noop = () => {};
global.document = {documentElement: {dataset: {}}, addEventListener: noop,
    querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
    createElement: () => ({})};
global.window = {fetch: () => Promise.resolve(), addEventListener: noop};
global.fetch = window.fetch;
eval(require('fs').readFileSync(__SOURCE__, 'utf8'));
__BODY__
"""


def _run(body: str):
    script = _HARNESS.replace("__SOURCE__", json.dumps(str(UTILS_JS))).replace("__BODY__", body)
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _match(src: str):
    return _run(f"process.stdout.write(JSON.stringify(_strongAnyFlankMatch({json.dumps(src)})));")


@pytest.mark.parametrize(
    "src, text",
    [
        ("**[대구지점](/resolve?uri=x)**이 1위입니다.", "[대구지점](/resolve?uri=x)"),
        ("**대구지점(E1)**이 1위", "대구지점(E1)"),
        ("**대구지점**이", "대구지점"),
        ("**a *b* c** tail", "a *b* c"),
    ],
)
def test_closes_a_same_line_pair_before_any_character(src, text):
    assert _match(src)["text"] == text


@pytest.mark.parametrize(
    "src",
    [
        "** spaced**",  # opener followed by whitespace
        "**spaced **",  # closer preceded by whitespace
        "***bold italic***",  # triple runs stay with the default rules
        "**across\nlines**",  # soft breaks stay with the default rules
        "**unclosed",
        "plain **bold**",  # only matches at the start of the source
    ],
)
def test_leaves_other_runs_to_the_default_rules(src):
    assert _match(src) is None


def test_render_markdown_registers_the_rule_once_and_emits_strong_tokens():
    out = _run(
        """
const used = [];
window.marked = {use: (opt) => used.push(opt), parse: (s) => s};
window.DOMPurify = {sanitize: (s) => s};
renderMarkdown('a'); renderMarkdown('b');
const ext = used[0].extensions[0];
const token = ext.tokenizer.call(
    {lexer: {inlineTokens: (t) => [{type: 'text', raw: t, text: t}]}},
    '**대구지점(E1)**이');
process.stdout.write(JSON.stringify({
    calls: used.length, level: ext.level, start: ext.start('ab **c**'),
    type: token.type, raw: token.raw, inner: token.tokens[0].text,
    miss: ext.tokenizer.call({lexer: {}}, 'no bold') === undefined}));
"""
    )
    assert out == {
        "calls": 1, "level": "inline", "start": 3, "type": "strong",
        "raw": "**대구지점(E1)**", "inner": "대구지점(E1)", "miss": True,
    }
