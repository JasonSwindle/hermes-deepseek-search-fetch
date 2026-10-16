#!/usr/bin/env python3
"""Unit tests for the web/deepseek plugin — stdlib `unittest`, no Hermes install.

Run from this directory:

    python -m unittest discover -s tests -v

The plugin imports only `httpx` plus two names from `agent.web_search_provider`,
so this suite installs a minimal stub for that module and exercises the plugin's
own logic: the base-URL guard, UTF-8 sanitising, request construction, response
mapping, hostile response shapes and every error path. A local mock HTTP server
stands in for the vendor, so the suite is offline, deterministic, unbilled and
needs no API key.

What this tier deliberately does NOT prove: that the plugin matches the *real*
Hermes provider interface, and how the dispatcher treats it (memo, disk cache,
rescue, provenance). Those need a Hermes install and live in the integration
tier (`dsweb-integration.py`, `dsweb-live-accept.py`).
"""
import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Repo default: the plugin sits at the repo root, one level up from this tests/ dir.
PLUGIN = Path(os.environ.get("DEEPSEEK_PLUGIN_PATH") or Path(__file__).resolve().parent.parent / "__init__.py")
DEFAULT_BASE = "https://api.deepseek.com"
DUMMY_KEY = "unit-test-key"
_URL = "/v1/tools/search"
_FETCH = "/v1/tools/fetch"


def install_stub() -> None:
    """Minimal `agent.web_search_provider` so the plugin imports anywhere."""
    if "agent.web_search_provider" in sys.modules:
        return
    pkg = types.ModuleType("agent")
    pkg.__path__ = []  # type: ignore[attr-defined]
    mod = types.ModuleType("agent.web_search_provider")

    class WebSearchProvider:  # noqa: D401 - stub of the Hermes ABC
        name = "stub"

        def is_available(self) -> bool:
            return True

        def supports_search(self) -> bool:
            return True

        def supports_extract(self) -> bool:
            return True

    def get_provider_env(key: str, default: str = "") -> str:
        return os.environ.get(key, default)

    mod.WebSearchProvider = WebSearchProvider
    mod.get_provider_env = get_provider_env
    sys.modules["agent"] = pkg
    sys.modules["agent.web_search_provider"] = mod


def load_plugin(name: str, base_url=None, api_key: str = DUMMY_KEY):
    """Import the plugin fresh, so module-level env reads happen per test."""
    if base_url is None:
        os.environ.pop("DEEPSEEK_BASE_URL", None)
    else:
        os.environ["DEEPSEEK_BASE_URL"] = base_url
    os.environ["DEEPSEEK_API_KEY"] = api_key
    install_stub()
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def provider(name: str, base_url=None, api_key: str = DUMMY_KEY):
    return load_plugin(name, base_url, api_key).DeepSeekWebSearchProvider()


# ── mock vendor ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    requests: list = []
    search_body: bytes = json.dumps({"results": [
        {"title": "Mock Title", "url": "https://example.invalid/1",
         "summary": "Mock Summary", "snippet": "ignored"},
        {"title": "Second", "url": "https://example.invalid/2", "snippet": "Only Snippet"},
    ]}).encode()
    fetch_body: bytes = json.dumps({
        "title": "Mock Page",
        # Escaped exactly as /v1/tools/fetch returns it — every punctuation
        # character backslashed — so the unescaping is falsifiable here.
        "content": "\\# Mock content\n\nSee \\[the docs\\]\\(https\\:\\/\\/example\\.invalid\\/d\\) for more\\.",
    }).encode()

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        prefix = self.path.split("/v1/")[0]
        if prefix.startswith("/r429"):
            return self._send(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}})
        if prefix.startswith("/err"):
            return self._send(500, {"error": {"message": "boom"}})
        if prefix.startswith("/html"):
            return self._send_raw(200, b"<html>not json</html>")
        if prefix.startswith("/shape_null"):
            return self._send(200, {"results": None})
        if prefix.startswith("/shape_str"):
            return self._send(200, {"results": "notalist"})
        if prefix.startswith("/shape_list"):
            return self._send(200, {"results": ["oops", 42, None]})
        if prefix.startswith("/shape_toplist"):
            return self._send_raw(200, json.dumps([1, 2, 3]).encode())
        if self.path.endswith(_FETCH):
            return self._send_raw(200, type(self).fetch_body)
        return self._send_raw(200, type(self).search_body)

    def _send(self, code, obj):
        self._send_raw(code, json.dumps(obj).encode())

    def _send_raw(self, code, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockVendor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.requests.clear()

    def last_body(self):
        return json.loads(Handler.requests[-1]["body"])


# ── unit: base-URL guard ──────────────────────────────────────────────────────
class TestBaseUrlGuard(unittest.TestCase):
    ACCEPTED = [
        (None, DEFAULT_BASE),
        ("", DEFAULT_BASE),
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://api.deepseek.com/", "https://api.deepseek.com"),
        ("http://127.0.0.1:9/ok", "http://127.0.0.1:9/ok"),
        ("http://localhost:8888/mock", "http://localhost:8888/mock"),
        ("http://[::1]:9000", "http://[::1]:9000"),
    ]
    REFUSED = [
        "https://evil.example",
        "http://api.deepseek.com",
        "http://169.254.169.254",
        "https://api.deepseek.com@evil.example",
        "https://user:pass@api.deepseek.com",
        "https://api.deepseek.com.evil.example",
        "https://api.deepseek.com./",
        "ftp://api.deepseek.com",
    ]

    def test_accepted_overrides(self):
        for i, (value, expected) in enumerate(self.ACCEPTED):
            with self.subTest(env=value):
                mod = load_plugin(f"guard_ok_{i}", value)
                self.assertEqual(mod._BASE, expected)
                self.assertTrue(mod._SEARCH_URL.endswith(_URL))
                self.assertTrue(mod._FETCH_URL.endswith(_FETCH))

    def test_refused_overrides_fall_back(self):
        for i, value in enumerate(self.REFUSED):
            with self.subTest(env=value):
                mod = load_plugin(f"guard_no_{i}", value)
                self.assertEqual(mod._BASE, DEFAULT_BASE)


# ── unit: UTF-8 sanitising ────────────────────────────────────────────────────
class TestUtf8Safe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_plugin("utf8_mod", None)

    def test_unpaired_surrogates_replaced(self):
        self.assertEqual(self.mod._utf8_safe("a\ud800b"), "a\ufffdb")
        self.assertEqual(self.mod._utf8_safe("\udfff"), "\ufffd")

    def test_valid_text_untouched(self):
        for text in ["plain", "🚀 👨‍👩‍👧‍👦", "الذكاء الاصطناعي", "搜索与抓取", "a\x00b"]:
            self.assertEqual(self.mod._utf8_safe(text), text)

    def test_result_is_encodable(self):
        self.mod._utf8_safe("lone \ud800 surrogate").encode("utf-8")


# ── unit: fetch unescaping ────────────────────────────────────────────────────
class TestUnescape(unittest.TestCase):
    """The fetch endpoint escapes every punctuation character in page text.
    `__init__.py` reverses exactly that, and nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_plugin("unescape_mod", None)

    def test_punctuation_escapes_are_removed(self):
        u = self.mod._unescape_markdown
        self.assertEqual(u("\\# Heading"), "# Heading")
        self.assertEqual(u("a \\| b"), "a | b")
        self.assertEqual(u("\\- item"), "- item")
        self.assertEqual(u("100\\% done"), "100% done")

    def test_link_markdown_is_restored(self):
        u = self.mod._unescape_markdown
        self.assertEqual(
            u("\\[the docs\\]\\(https\\:\\/\\/example\\.invalid\\/d\\)"),
            "[the docs](https://example.invalid/d)",
        )

    def test_letter_escapes_survive(self):
        """Only punctuation is unescaped, so escapes and regexes in prose stay."""
        u = self.mod._unescape_markdown
        for text in ["a\\nb", "\\d+ digits", "\\u2014 dash", "C:\\path"]:
            with self.subTest(text=text):
                self.assertEqual(u(text), text)

    def test_text_without_escapes_is_untouched(self):
        u = self.mod._unescape_markdown
        for text in ["plain", "# already clean", "🚀 emoji", "搜索与抓取"]:
            with self.subTest(text=text):
                self.assertEqual(u(text), text)


# ── contract: search ──────────────────────────────────────────────────────────
class TestSearchContract(MockVendor):
    def test_limit_clamped_and_payload_shape(self):
        p = provider("s_clamp", f"{self.base}/ok")
        for given, expected in ((0, 1), (7, 7), (999, 50), (1, 1), (50, 50)):
            with self.subTest(limit=given):
                Handler.requests.clear()
                p.search("q", limit=given)
                self.assertEqual(self.last_body(), {"query": "q", "count": expected})

    def test_auth_and_content_type_headers(self):
        p = provider("s_hdr", f"{self.base}/ok")
        p.search("q")
        h = Handler.requests[-1]["headers"]
        self.assertEqual(h.get("Authorization"), f"Bearer {DUMMY_KEY}")
        self.assertIn("application/json", h.get("Content-Type", ""))

    def test_result_mapping_prefers_summary_then_snippet(self):
        p = provider("s_map", f"{self.base}/ok")
        out = p.search("q", limit=2)
        self.assertTrue(out["success"])
        self.assertEqual(out["data"]["web"][0], {
            "title": "Mock Title", "url": "https://example.invalid/1",
            "description": "Mock Summary", "position": 1})
        self.assertEqual(out["data"]["web"][1]["description"], "Only Snippet")
        self.assertEqual(out["data"]["web"][1]["position"], 2)

    def test_query_is_utf8_sanitised_before_send(self):
        p = provider("s_sur", f"{self.base}/ok")
        p.search("lone \ud800 query")
        self.assertEqual(self.last_body()["query"], "lone \ufffd query")

    def test_empty_results_is_success(self):
        p = provider("s_null", f"{self.base}/shape_null")
        out = p.search("q")
        self.assertTrue(out["success"])
        self.assertEqual(out["data"]["web"], [])

    def test_hostile_shapes_are_graceful(self):
        for name, prefix in (("list", "/shape_list"), ("str", "/shape_str"), ("toplist", "/shape_toplist")):
            with self.subTest(shape=name):
                out = provider(f"s_{name}", f"{self.base}{prefix}").search("q")
                self.assertFalse(out["success"])
                self.assertTrue(out["error"])


# ── contract: extract ─────────────────────────────────────────────────────────
class TestExtractContract(MockVendor):
    def test_success_mapping(self):
        p = provider("e_ok", f"{self.base}/ok")
        out = p.extract(["https://example.invalid/x"])
        self.assertEqual(len(out), 1)
        self.assertNotIn("error", out[0])
        self.assertEqual(out[0]["title"], "Mock Page")
        self.assertEqual(out[0]["content"], "# Mock content\n\nSee [the docs](https://example.invalid/d) for more.")
        self.assertEqual(out[0]["raw_content"], out[0]["content"])
        self.assertEqual(out[0]["metadata"]["sourceURL"], "https://example.invalid/x")
        self.assertTrue(Handler.requests[-1]["path"].endswith(_FETCH))

    def test_fetched_markdown_is_unescaped_in_both_fields(self):
        """Hermes reads `raw_content` first — it derives the model-visible content
        and the disk cache from it — so leaving the vendor's escaping there would
        reintroduce it in every cached and budgeted result."""
        out = provider("e_unesc", f"{self.base}/ok").extract(["https://example.invalid/x"])
        for field in ("content", "raw_content"):
            text = out[0][field]
            with self.subTest(field=field):
                self.assertNotIn("\\#", text)
                self.assertNotIn("\\[", text)
                self.assertRegex(text, r"^# Mock content")
                self.assertIn("[the docs](https://example.invalid/d)", text)

    def test_empty_url_list(self):
        self.assertEqual(provider("e_empty", f"{self.base}/ok").extract([]), [])

    def test_per_url_errors_do_not_abort_the_batch(self):
        p = provider("e_mixed", f"{self.base}/ok")
        out = p.extract(["https://example.invalid/1", "https://example.invalid/2"])
        self.assertEqual(len(out), 2)
        self.assertEqual([e["url"] for e in out],
                         ["https://example.invalid/1", "https://example.invalid/2"])

    def test_failure_shape_is_per_entry(self):
        p = provider("e_err", f"{self.base}/err")
        out = p.extract(["https://example.invalid/x"])
        self.assertIn("error", out[0])
        self.assertIn("HTTP 500", out[0]["error"])

    def test_urls_are_utf8_sanitised(self):
        p = provider("e_sur", f"{self.base}/ok")
        out = p.extract(["https://example.invalid/\ud800p"])
        self.assertEqual(out[0]["url"], "https://example.invalid/\ufffdp")


# ── errors ────────────────────────────────────────────────────────────────────
class TestErrorPaths(MockVendor):
    def test_missing_key_is_graceful_for_both_capabilities(self):
        p = provider("err_nokey", f"{self.base}/ok", api_key="")
        self.assertFalse(p.is_available())
        s = p.search("q")
        self.assertFalse(s["success"])
        self.assertIn("DEEPSEEK_API_KEY", s["error"])
        e = p.extract(["https://example.invalid/x"])
        self.assertIn("DEEPSEEK_API_KEY", e[0]["error"])

    def test_rate_limit_shape(self):
        p = provider("err_429", f"{self.base}/r429")
        out = p.search("q")
        self.assertFalse(out["success"])
        self.assertIn("HTTP 429", out["error"])
        self.assertEqual(len(Handler.requests), 1)  # no retry

    def test_non_200_carries_status_and_detail(self):
        p = provider("err_500", f"{self.base}/err")
        out = p.search("q")
        self.assertIn("HTTP 500", out["error"])
        self.assertIn("boom", out["error"])

    def test_non_json_200_is_labelled(self):
        p = provider("err_html", f"{self.base}/html")
        out = p.search("q")
        self.assertFalse(out["success"])
        self.assertIn("non-JSON response", out["error"])

    def test_none_limit_is_graceful(self):
        p = provider("err_none", f"{self.base}/ok")
        out = p.search("q", limit=None)
        self.assertFalse(out["success"])
        self.assertTrue(out["error"])


# ── upstream contract (docs: developer-guide/web-search-provider-plugin) ──────
class TestProviderContract(MockVendor):
    """Rules the Hermes docs state for every web-search provider plugin."""

    def test_is_available_makes_no_network_calls(self):
        """Docs: the availability gate "MUST NOT make network calls (runs on every
        `hermes tools` paint)". Proven by making any HTTP use blow up."""
        class Exploding:
            def post(self, *a, **k):
                raise AssertionError("is_available() must not touch the network")

            def get(self, *a, **k):
                raise AssertionError("is_available() must not touch the network")

        mod = load_plugin("contract_avail", f"{self.base}/ok")
        mod.httpx = Exploding()
        p = mod.DeepSeekWebSearchProvider()
        self.assertIs(p.is_available(), True)          # key present
        os.environ["DEEPSEEK_API_KEY"] = ""
        try:
            self.assertIs(p.is_available(), False)     # key absent, still no calls
        finally:
            os.environ["DEEPSEEK_API_KEY"] = DUMMY_KEY

    def test_search_envelope_matches_the_documented_shape(self):
        out = provider("contract_env", f"{self.base}/ok").search("q", limit=2)
        self.assertEqual(set(out), {"success", "data"})
        self.assertEqual(set(out["data"]), {"web"})
        for hit in out["data"]["web"]:
            self.assertEqual(set(hit), {"title", "url", "description", "position"})

    def test_extract_returns_a_bare_list_not_an_envelope(self):
        """Deliberate divergence, pinned so a future change is a conscious one: the
        docs show extract success as {"success": True, "data": [...]}, but the
        bundled providers (exa, firecrawl) return a bare list — and the dispatcher
        consumes ours that way end-to-end (verified live). Bundled parity wins."""
        out = provider("contract_ext", f"{self.base}/ok").extract(["https://example.invalid/x"])
        self.assertIsInstance(out, list)
        self.assertTrue(all(isinstance(e, dict) for e in out))
        self.assertTrue({"url", "title", "content", "raw_content"} <= set(out[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
