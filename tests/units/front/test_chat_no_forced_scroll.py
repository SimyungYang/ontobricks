"""The in-app chats never force the reader's scroll position.

Graph Chat (Digital Twin) and the Ontology Studio assistant used to pin the
messages pane to the bottom on every streamed step, tool result, reply,
error and restored-history message, so a user who scrolled up to read an
earlier answer was yanked back down. The only programmatic scroll left is
``revealSentMessage``: when the user sends a message, their own message is
brought into view once.

The behaviour tests run the real modules under node against a stubbed DOM
whose messages pane records every ``scrollTop`` write. The static guard keeps
new scroll calls out of the chat modules.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
QUERY_CHAT_JS = REPO_ROOT / "src/front/static/query/js/query-chat.js"
ONTOLOGY_ASSISTANT_JS = REPO_ROOT / "src/front/static/ontology/js/ontology-assistant.js"
CHAT_MODULES = (QUERY_CHAT_JS, ONTOLOGY_ASSISTANT_JS)

USER_SCROLL_POSITION = 7

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the frontend module"
)

# Minimal DOM: elements keep children, classes and listeners; the messages
# pane records which phase each scrollTop write happened in.
_DOM_STUB = """
const fs = require('fs');
const USER_POS = __USER_POS__;
let phase = 'idle';
let focusCount = 0;
const scrollWrites = [];
const listeners = {};

function makeEl(tag) {
    const e = {
        nodeName: String(tag || 'div').toUpperCase(),
        children: [], parentNode: null, style: {}, dataset: {},
        value: '', disabled: false, id: '', innerHTML: '', textContent: '',
        _cls: new Set(), _listeners: {},
    };
    Object.defineProperty(e, 'className', {
        get() { return Array.from(e._cls).join(' '); },
        set(v) { e._cls = new Set(String(v).split(/\\s+/).filter(Boolean)); },
    });
    e.classList = {
        add: (...c) => c.forEach((x) => e._cls.add(x)),
        remove: (...c) => c.forEach((x) => e._cls.delete(x)),
        contains: (c) => e._cls.has(c),
        toggle: (c, on) => {
            const want = on === undefined ? !e._cls.has(c) : on;
            if (want) e._cls.add(c); else e._cls.delete(c);
        },
    };
    e.appendChild = (c) => { c.parentNode = e; e.children.push(c); return c; };
    e.removeChild = (c) => { e.children = e.children.filter((x) => x !== c); c.parentNode = null; return c; };
    e.remove = () => { if (e.parentNode) e.parentNode.removeChild(e); };
    e.setAttribute = (k, v) => { e['attr:' + k] = v; };
    e.getAttribute = (k) => e['attr:' + k];
    e.addEventListener = (t, fn) => { (e._listeners[t] = e._listeners[t] || []).push(fn); };
    e.focus = () => { focusCount += 1; };
    e.querySelector = (sel) => {
        if (!sel.startsWith('.')) return null;
        const cls = sel.slice(1);
        const stack = [...e.children];
        while (stack.length) {
            const n = stack.shift();
            if (n._cls && n._cls.has(cls)) return n;
            stack.push(...(n.children || []));
        }
        return null;
    };
    e.querySelectorAll = () => [];
    return e;
}

const messages = makeEl('div');
let scrollTopValue = 0;
Object.defineProperty(messages, 'scrollTop', {
    get() { return scrollTopValue; },
    set(v) { scrollWrites.push(phase); scrollTopValue = v; },
});
Object.defineProperty(messages, 'scrollHeight', {
    get() { return messages.children.length * 100; },
});
function userScrollsUp() { scrollTopValue = USER_POS; }

const elements = { __MESSAGES_ID__: messages };
['__INPUT_ID__', '__SEND_ID__'].forEach((id) => { elements[id] = makeEl(id === '__INPUT_ID__' ? 'textarea' : 'button'); });

global.window = {};
global.NodeFilter = { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 };
global.renderMarkdown = (text) => String(text);
global.document = {
    getElementById: (id) => elements[id] || null,
    createElement: makeEl,
    createTextNode: (text) => Object.assign(makeEl('#text'), { textContent: String(text) }),
    createTreeWalker: () => ({ nextNode: () => null }),
    querySelectorAll: () => [],
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
};
function fire(type, detail) { (listeners[type] || []).forEach((fn) => fn({ detail })); }

async function waitFor(predicate) {
    for (let i = 0; i < 200 && !predicate(); i += 1) {
        await new Promise((r) => setTimeout(r, 5));
    }
}
function report() {
    process.stdout.write(JSON.stringify({
        scrollWrites, scrollTop: scrollTopValue, messageCount: messages.children.length,
        bubbles: messages.children.map((c) => c.className),
    }));
}
"""

_GRAPH_CHAT_HARNESS = _DOM_STUB + """
const scenario = __SCENARIO__;
const encoder = new TextEncoder();
function sse(events) {
    const chunks = events.map((ev) => encoder.encode('data: ' + JSON.stringify(ev) + '\\n\\n'));
    return {
        getReader: () => ({
            read: async () => {
                phase = 'stream';
                userScrollsUp();
                return chunks.length ? { done: false, value: chunks.shift() } : { done: true };
            },
            releaseLock: () => {},
        }),
    };
}
let historyFetched = false;
global.fetch = async (url, options) => {
    phase = 'response';
    userScrollsUp();
    if (url === '/dtwin/assistant/history') {
        historyFetched = true;
        return { ok: true, json: async () => ({
            success: true, limit: 20,
            messages: [
                { role: 'user', content: 'first question' },
                { role: 'assistant', content: 'first answer' },
                { role: 'user', content: 'second question' },
                { role: 'assistant', content: 'second answer' },
            ],
        }) };
    }
    if (scenario === 'network_error') throw new Error('offline');
    if (scenario === 'http_error') {
        return { ok: false, status: 500, body: null, json: async () => ({ message: 'boom' }) };
    }
    return { ok: true, body: sse([
        { type: 'step', step_type: 'tool_call', tool_name: 'sparql' },
        { type: 'step', step_type: 'tool_result', tool_name: 'sparql', duration_ms: 12 },
        { type: 'step', step_type: 'tool_call', tool_name: 'describe' },
        { type: 'step', step_type: 'tool_result', tool_name: 'describe', duration_ms: 8 },
        { type: 'done', reply: '**answer**',
          tools: [{ name: 'sparql', duration_ms: 12 }],
          pending_action: { token: 'tok', action: 'recompute', entity_label: 'E1' },
          resource_pressure: true, resource_advice: 'heavy load' },
    ]) };
};

eval(fs.readFileSync(__SOURCE__, 'utf8'));

(async () => {
    if (scenario === 'history_restore') {
        fire('DOMContentLoaded');
        fire('sidebarSectionChanged', { section: 'chat' });
        await waitFor(() => historyFetched && messages.children.length === 4);
    } else if (scenario === 'assistant_push') {
        window.appendChatAssistantMessage('Interpretation of the dashboard');
    } else {
        phase = 'send';
        window.chatSendMessage('How many customers?');
        await waitFor(() => focusCount > 0);
    }
    report();
})();
"""

_ONTOLOGY_ASSISTANT_HARNESS = _DOM_STUB + """
const scenario = __SCENARIO__;
global.fetch = async () => {
    phase = 'response';
    userScrollsUp();
    if (scenario === 'network_error') throw new Error('offline');
    return { json: async () => (scenario === 'server_error'
        ? { success: false, message: 'nope' }
        : { success: true, reply: 'Added class **Pump**', ontology_changed: false }) };
};

eval(fs.readFileSync(__SOURCE__, 'utf8'));

(async () => {
    window.initOntologyAssistant();
    const input = elements['__INPUT_ID__'];
    input.value = 'Add a Pump class';
    phase = 'send';
    input._listeners.keydown[0]({ key: 'Enter', shiftKey: false, preventDefault() {} });
    await waitFor(() => focusCount > 0);
    report();
})();
"""


def _run(harness, source, ids, scenario):
    script = (
        harness.replace("__SOURCE__", json.dumps(str(source)))
        .replace("__SCENARIO__", json.dumps(scenario))
        .replace("__USER_POS__", str(USER_SCROLL_POSITION))
        .replace("__MESSAGES_ID__", ids[0])
        .replace("__INPUT_ID__", ids[1])
        .replace("__SEND_ID__", ids[2])
    )
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _graph_chat(scenario):
    return _run(
        _GRAPH_CHAT_HARNESS, QUERY_CHAT_JS, ("chatMessages", "chatInput", "chatSendBtn"), scenario
    )


def _ontology_assistant(scenario):
    return _run(
        _ONTOLOGY_ASSISTANT_HARNESS,
        ONTOLOGY_ASSISTANT_JS,
        ("assistantMessages", "assistantInput", "assistantSendBtn"),
        scenario,
    )


@needs_node
class TestGraphChatScroll:
    def test_sending_scrolls_exactly_once_before_the_response(self):
        outcome = _graph_chat("stream")
        assert outcome["scrollWrites"] == ["send"]

    def test_streamed_steps_and_final_reply_keep_the_reader_position(self):
        """The user scrolled up while the agent ran; nothing pulls them back."""
        outcome = _graph_chat("stream")
        assert outcome["scrollTop"] == USER_SCROLL_POSITION

    def test_stream_finalizes_the_reply_and_advisory(self):
        """Guards the harness: the reply path really ran, not an error path."""
        outcome = _graph_chat("stream")
        assert outcome["bubbles"] == [
            "assistant-msg user-msg",
            "assistant-msg bot-msg",
            "alert alert-warning py-2 px-3 small graph-chat-resource-advisory d-flex align-items-start",
        ]

    def test_http_error_does_not_scroll(self):
        outcome = _graph_chat("http_error")
        assert outcome["scrollWrites"] == ["send"]

    def test_network_error_does_not_scroll(self):
        outcome = _graph_chat("network_error")
        assert outcome["scrollWrites"] == ["send"]

    def test_restored_history_does_not_scroll(self):
        outcome = _graph_chat("history_restore")
        assert outcome["messageCount"] == 4
        assert outcome["scrollWrites"] == []

    def test_pushed_assistant_message_does_not_scroll(self):
        """Analytics "interpret" pushes an answer into the chat; no jump."""
        outcome = _graph_chat("assistant_push")
        assert outcome["messageCount"] == 1
        assert outcome["scrollWrites"] == []


@needs_node
class TestOntologyAssistantScroll:
    def test_sending_scrolls_exactly_once_before_the_reply(self):
        outcome = _ontology_assistant("reply")
        assert outcome["scrollWrites"] == ["send"]

    def test_reply_keeps_the_reader_position(self):
        outcome = _ontology_assistant("reply")
        assert outcome["scrollTop"] == USER_SCROLL_POSITION

    def test_server_error_does_not_scroll(self):
        outcome = _ontology_assistant("server_error")
        assert outcome["scrollWrites"] == ["send"]

    def test_network_error_does_not_scroll(self):
        outcome = _ontology_assistant("network_error")
        assert outcome["scrollWrites"] == ["send"]


# ---------------------------------------------------------------------
# Static guard: no scroll calls outside the send-path helper.
# ---------------------------------------------------------------------

_SCROLL_CALL = re.compile(r"scrollTop\s*=(?!=)|scrollIntoView|scrollTo\s*\(|scrollBy\s*\(")
_HELPER = "function revealSentMessage()"


def _outside_helper(source):
    start = source.index(_HELPER)
    end = source.index("\n    }\n", start)
    return source[:start] + source[end:], source[start:end]


@pytest.mark.parametrize("module", CHAT_MODULES, ids=lambda p: p.name)
def test_chat_module_scrolls_only_inside_the_send_helper(module):
    rest, _ = _outside_helper(module.read_text(encoding="utf-8"))
    assert _SCROLL_CALL.findall(rest) == []


@pytest.mark.parametrize("module", CHAT_MODULES, ids=lambda p: p.name)
def test_send_helper_is_the_one_scroll_to_the_newest_message(module):
    _, helper = _outside_helper(module.read_text(encoding="utf-8"))
    assert "container.scrollTop = container.scrollHeight" in helper


@pytest.mark.parametrize("module", CHAT_MODULES, ids=lambda p: p.name)
def test_send_helper_is_called_only_from_send_message(module):
    source = module.read_text(encoding="utf-8")
    assert source.count("revealSentMessage()") == 2  # definition + one call
    send_start = source.index("async function sendMessage(text)")
    send_end = source.index("\n    }\n", send_start)
    assert "revealSentMessage();" in source[send_start:send_end]
