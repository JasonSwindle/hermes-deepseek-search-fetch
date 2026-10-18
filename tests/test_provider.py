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
_RATE_LIMIT = {
    "error": {
        "message": "Rate limit reached",
        "type": "rate_limit_error",
        "param": None,
        "code": "rate_limit_error",
    }
}


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


def fast_clock(mod, advance: float = 0.0):
    """Replace the retry clock so a backoff does not stall the suite.

    `advance` is added on every POST. A test uses it to pretend the server
    took that long, which is what the 40s budget keys off.
    """
    state = {"now": 0.0, "sleeps": []}
    real_request = mod._request

    def monotonic():
        return state["now"]

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    def request(url, payload, headers):
        state["now"] += advance
        return real_request(url, payload, headers)

    mod._monotonic = monotonic
    mod._sleep = sleep
    mod._uniform = lambda a, b: 0.0
    mod._request = request
    return state


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
        if prefix.startswith("/html429"):
            seen = sum(1 for r in type(self).requests if "/html429" in r["path"])
            if seen == 1:
                return self._send(429, _RATE_LIMIT)
            return self._send_raw(200, b"<html>not json</html>")
        if prefix.startswith("/once429"):
            seen = sum(1 for r in type(self).requests if "/once429" in r["path"])
            if seen == 1:
                return self._send(429, _RATE_LIMIT)
            return self._send_raw(200, type(self).search_body)
        if prefix.startswith("/ra_short"):
            return self._send(429, _RATE_LIMIT, {"Retry-After": "0.5"})
        if prefix.startswith("/ra_long"):
            return self._send(429, _RATE_LIMIT, {"Retry-After": "9"})
        if prefix.startswith("/ra_date"):
            return self._send(429, _RATE_LIMIT, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        if prefix.startswith("/ra_zero"):
            return self._send(429, _RATE_LIMIT, {"Retry-After": "0"})
        if prefix.startswith("/long400"):
            return self._send(400, {"error": {"message": "m" * 400, "type": "invalid_request_error",
                                              "param": None, "code": "invalid_request_error"}})
        if prefix.startswith("/badmsg"):
            return self._send_raw(400, b'{"error": {"message": 1}}')
        if prefix.startswith("/s") and prefix[2:].isdigit():
            code = int(prefix[2:])
            return self._send(code, {"error": {"message": f"status {code}", "type": "error",
                                               "param": None, "code": "error"}})
        if prefix.startswith("/r429"):
            return self._send(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}})
        if prefix.startswith("/err"):
            return self._send(500, {"error": {"message": "boom"}})
        if prefix.startswith("/html"):
            return self._send_raw(200, b"<html>not json</html>")
        if prefix.startswith("/body_content"):
            return self._send(200, {"results": [
                {"title": "Doc", "url": "https://example.invalid/doc1",
                 "summary": "Doc one summary", "content": "# Doc one"},
                {"title": "Empty", "url": "https://example.invalid/doc2",
                 "summary": "Doc two summary", "content": ""},
            ]})
        if prefix.startswith("/body_meta"):
            return self._send(200, {"results": [
                {"title": "Ranked", "url": "https://example.invalid/r1", "summary": "S1",
                 "rerank_score": 2.4, "authority": 3, "date": "2026-06-01T00:00:00+00:00",
                 "site_name": "Example News", "site_icon": "https://example.invalid/favicon.ico"},
                {"title": "Thin", "url": "https://example.invalid/r2", "snippet": "S2"},
            ]})
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

    def _send(self, code, obj, headers=None):
        self._send_raw(code, json.dumps(obj).encode(), headers)

    def _send_raw(self, code, payload: bytes, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
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
            "description": "Mock Summary", "position": 1,
            "rerank_score": None, "authority": None, "date": None,
            "site_name": None, "site_icon": None})
        self.assertEqual(out["data"]["web"][1]["description"], "Only Snippet")
        self.assertEqual(out["data"]["web"][1]["position"], 2)

    def test_quality_freshness_and_provenance_are_mapped(self):
        """A caller can filter and attribute a result set without fetching it."""
        out = provider("s_meta", f"{self.base}/body_meta").search("q", limit=2)
        first, second = out["data"]["web"]
        self.assertEqual(first["rerank_score"], 2.4)
        self.assertEqual(first["authority"], 3)
        self.assertEqual(first["date"], "2026-06-01T00:00:00+00:00")
        self.assertEqual(first["site_name"], "Example News")
        self.assertEqual(first["site_icon"], "https://example.invalid/favicon.ico")
        self.assertEqual(second["title"], "Thin")
        for field in ("rerank_score", "authority", "date", "site_name", "site_icon"):
            with self.subTest(field=field):
                self.assertIn(field, second)
                self.assertIsNone(second[field])

    def test_need_content_is_off_by_default(self):
        """The default stays metadata-only: no flag on the wire, no content key."""
        os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
        p = provider("s_nc_default", f"{self.base}/body_content")
        out = p.search("q", limit=2)
        self.assertEqual(self.last_body(), {"query": "q", "count": 2})
        for hit in out["data"]["web"]:
            self.assertNotIn("content", hit)

    def test_need_content_opt_in_sends_the_flag_and_maps_content(self):
        os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = "1"
        try:
            p = provider("s_nc_on", f"{self.base}/body_content")
            out = p.search("q", limit=2)
            self.assertEqual(self.last_body(),
                             {"query": "q", "count": 2, "need_content": True})
            self.assertEqual([h["content"] for h in out["data"]["web"]],
                             ["# Doc one", ""])
        finally:
            os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)

    def test_need_content_flag_spellings(self):
        mod = load_plugin("s_nc_flag", None)
        for value in ("1", "true", "TRUE", "Yes", "on", " on "):
            with self.subTest(value=value):
                os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = value
                self.assertTrue(mod._need_content_enabled())
        for value in ("", "0", "no", "off", "false", "nope"):
            with self.subTest(value=value):
                os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = value
                self.assertFalse(mod._need_content_enabled())
        os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)

    def test_need_content_setting_sends_the_flag(self):
        """A ticked setting is what puts need_content on the wire."""
        os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
        mod = load_plugin("s_nc_setting_on", f"{self.base}/body_content")
        p = mod.DeepSeekWebSearchProvider(lambda key: True)
        p.search("q", limit=2)
        self.assertEqual(self.last_body(),
                         {"query": "q", "count": 2, "need_content": True})

    def test_need_content_setting_wins_over_the_env_var(self):
        """An ambient variable must not override a choice made in the UI."""
        os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = "1"
        try:
            mod = load_plugin("s_nc_setting_off", f"{self.base}/body_content")
            p = mod.DeepSeekWebSearchProvider(lambda key: False)
            p.search("q", limit=2)
            self.assertEqual(self.last_body(), {"query": "q", "count": 2})
        finally:
            os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)

    def test_unset_setting_falls_back_to_the_env_var(self):
        """None means "not set", not "off": the environment still works."""
        os.environ["DEEPSEEK_SEARCH_NEED_CONTENT"] = "1"
        try:
            mod = load_plugin("s_nc_setting_unset", f"{self.base}/body_content")
            p = mod.DeepSeekWebSearchProvider(lambda key: None)
            p.search("q", limit=2)
            self.assertEqual(self.last_body(),
                             {"query": "q", "count": 2, "need_content": True})
        finally:
            os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)

    def test_a_non_boolean_setting_does_not_enable_content(self):
        """"true" in a hand-edited config is not consent; a bool or nothing."""
        os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
        mod = load_plugin("s_nc_setting_str", f"{self.base}/body_content")
        p = mod.DeepSeekWebSearchProvider(
            lambda key: "true" if key == "need_content" else None)
        p.search("q", limit=2)
        self.assertEqual(self.last_body(), {"query": "q", "count": 2})

    def test_a_failing_settings_reader_does_not_break_search(self):
        """A settings read is a preference, never a capability gate."""
        os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
        mod = load_plugin("s_nc_setting_raises", f"{self.base}/body_content")

        def boom(key):
            raise RuntimeError("config unavailable")

        out = mod.DeepSeekWebSearchProvider(boom).search("q", limit=2)
        self.assertTrue(out["success"])
        self.assertEqual(self.last_body(), {"query": "q", "count": 2})

    def test_localization_setting_is_sent(self):
        """A set IP reaches the wire, next to the query and count."""
        mod = load_plugin("s_loc_on", f"{self.base}/body_content")
        p = mod.DeepSeekWebSearchProvider(
            lambda key: "203.0.113.7" if key == "localization" else None)
        p.search("q", limit=2)
        self.assertEqual(self.last_body(),
                         {"query": "q", "count": 2, "localization": "203.0.113.7"})

    def test_localization_is_not_sent_when_unset(self):
        """Unset, empty and whitespace all mean "no localisation"."""
        for index, value in enumerate((None, "", "   ")):
            with self.subTest(value=value):
                mod = load_plugin(f"s_loc_off_{index}", f"{self.base}/body_content")
                p = mod.DeepSeekWebSearchProvider(lambda key, v=value: v)
                out = p.search("q", limit=2)
                self.assertTrue(out["success"])
                self.assertNotIn("localization", self.last_body())

    def test_a_junk_localization_is_not_sent(self):
        """The API takes an IP; anything else is refused here, not billed."""
        for index, value in enumerate(("not-an-ip", "1.2.3.4.5", "::zzz", 203, True)):
            with self.subTest(value=value):
                os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
                mod = load_plugin(f"s_loc_bad_{index}", f"{self.base}/body_content")
                p = mod.DeepSeekWebSearchProvider(lambda key, v=value: v)
                out = p.search("q", limit=2)
                self.assertTrue(out["success"])
                self.assertNotIn("localization", self.last_body())

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
        mod = load_plugin("e_err", f"{self.base}/err")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().extract(["https://example.invalid/x"])
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

    def test_rate_limit_is_retried_then_reported(self):
        mod = load_plugin("err_429", f"{self.base}/r429")
        clock = fast_clock(mod)
        with self.assertLogs(mod.__name__, level="WARNING") as captured:
            out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(
            out["error"],
            "DeepSeek search failed: HTTP 429: Rate limit reached (after 2 retries)",
        )
        self.assertEqual(len(Handler.requests), 3)
        self.assertEqual(clock["sleeps"], [1.0, 2.0])
        retry_lines = [line for line in captured.output if "retry " in line]
        self.assertEqual(retry_lines, [
            f"WARNING:{mod.__name__}:DeepSeek HTTP 429, retry 1/2",
            f"WARNING:{mod.__name__}:DeepSeek HTTP 429, retry 2/2",
        ])
        self.assertNotIn(DUMMY_KEY, "\n".join(captured.output))

    def test_retry_then_success_keeps_the_result_shape(self):
        mod = load_plugin("retry_ok", f"{self.base}/once429")
        clock = fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q", limit=2)
        self.assertTrue(out["success"], out)
        self.assertEqual(set(out), {"success", "data"})
        self.assertEqual(set(out["data"]), {"web"})
        self.assertEqual(len(Handler.requests), 2)
        self.assertEqual(clock["sleeps"], [1.0])
        self.assertNotIn("retry", json.dumps(out))

    def test_extract_rate_limit_stays_a_per_entry_error(self):
        mod = load_plugin("retry_ext", f"{self.base}/r429")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().extract(["https://example.invalid/x"])
        self.assertEqual(
            out[0]["error"],
            "DeepSeek fetch failed: HTTP 429: Rate limit reached (after 2 retries)",
        )
        self.assertNotIn("blocked_by_policy", out[0])

    def test_terminal_statuses_are_single_shot(self):
        for code in (400, 403, 404):
            with self.subTest(code=code):
                Handler.requests.clear()
                mod = load_plugin(f"term_{code}", f"{self.base}/s{code}")
                clock = fast_clock(mod)
                out = mod.DeepSeekWebSearchProvider().search("q")
                self.assertEqual(
                    out["error"],
                    f"DeepSeek search failed: HTTP {code}: status {code}",
                )
                self.assertEqual(len(Handler.requests), 1)
                self.assertEqual(clock["sleeps"], [])

    def test_payment_required_says_to_top_up(self):
        mod = load_plugin("err_402", f"{self.base}/s402")
        clock = fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(
            out["error"],
            "DeepSeek search failed: HTTP 402: status 402. "
            "Top up the DeepSeek account before retrying.",
        )
        self.assertEqual(len(Handler.requests), 1)
        self.assertEqual(clock["sleeps"], [])

    def test_empty_402_says_insufficient_balance(self):
        mod = load_plugin("err_402empty", f"{self.base}/ok")

        def request(*_a, **_k):
            return mod.httpx.Response(402, content=b"", extensions={"reason_phrase": b""})

        mod._request = request
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(
            out["error"],
            "DeepSeek search failed: HTTP 402: Insufficient balance. "
            "Top up the DeepSeek account before retrying.",
        )

    def test_vendor_message_is_capped_at_300_characters(self):
        mod = load_plugin("err_long", f"{self.base}/long400")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(out["error"], "DeepSeek search failed: HTTP 400: " + ("m" * 300))

    def test_non_string_error_message_falls_back_to_the_body(self):
        mod = load_plugin("err_badmsg", f"{self.base}/badmsg")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(
            out["error"],
            'DeepSeek search failed: HTTP 400: {"error": {"message": 1}}',
        )
        self.assertEqual(len(Handler.requests), 1)

    def test_transient_statuses_are_retried(self):
        for code in (408, 500, 503):
            with self.subTest(code=code):
                Handler.requests.clear()
                mod = load_plugin(f"transient_{code}", f"{self.base}/s{code}")
                clock = fast_clock(mod)
                out = mod.DeepSeekWebSearchProvider().search("q")
                self.assertEqual(
                    out["error"],
                    f"DeepSeek search failed: HTTP {code}: status {code} (after 2 retries)",
                )
                self.assertEqual(len(Handler.requests), 3)
                self.assertEqual(clock["sleeps"], [1.0, 2.0])

    def test_non_200_carries_status_and_detail(self):
        mod = load_plugin("err_500", f"{self.base}/err")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(out["error"], "DeepSeek search failed: HTTP 500: boom (after 2 retries)")
        self.assertEqual(len(Handler.requests), 3)

    def test_transport_error_is_not_retried(self):
        mod = load_plugin("err_conn", f"{self.base}/ok")
        sleeps = []
        mod._sleep = lambda seconds: sleeps.append(seconds)

        def request(*_a, **_k):
            raise mod.httpx.ConnectError("connection refused")

        mod._request = request
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertFalse(out["success"])
        self.assertIn("connection refused", out["error"])
        self.assertNotIn("(after ", out["error"])
        self.assertEqual(sleeps, [])

    def test_transport_error_after_a_retry_keeps_the_count(self):
        mod = load_plugin("err_conn_retry", f"{self.base}/ok")
        clock = fast_clock(mod)
        calls = {"n": 0}

        def request(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                return mod.httpx.Response(429, json=_RATE_LIMIT)
            raise mod.httpx.ConnectError("connection refused")

        mod._request = request
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertIn("connection refused", out["error"])
        self.assertIn("(after 1 retry)", out["error"])
        self.assertEqual(clock["sleeps"], [1.0])
        self.assertEqual(calls["n"], 2)

    def test_retry_after_replaces_a_shorter_wait(self):
        mod = load_plugin("ra_short", f"{self.base}/ra_short")
        clock = fast_clock(mod)
        mod._uniform = lambda _a, _b: 0.25
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertIn("(after 2 retries)", out["error"])
        self.assertEqual(clock["sleeps"], [0.5, 0.5])

    def test_retry_after_is_ignored_unless_it_is_a_short_delay(self):
        for name in ("ra_long", "ra_date", "ra_zero"):
            with self.subTest(name=name):
                Handler.requests.clear()
                mod = load_plugin(name, f"{self.base}/{name}")
                clock = fast_clock(mod)
                mod._uniform = lambda _a, _b: 0.25
                mod.DeepSeekWebSearchProvider().search("q")
                self.assertEqual(clock["sleeps"], [1.25, 2.25])

    def test_retry_stops_when_the_budget_is_spent(self):
        mod = load_plugin("budget_slow", f"{self.base}/err")
        clock = fast_clock(mod, advance=30.0)
        with self.assertLogs(mod.__name__, level="WARNING") as captured:
            out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertIn("(after 1 retry)", out["error"])
        self.assertEqual(len(Handler.requests), 2)
        self.assertEqual(clock["sleeps"], [1.0])
        self.assertEqual(
            [line for line in captured.output if "retry " in line],
            [f"WARNING:{mod.__name__}:DeepSeek HTTP 500, retry 1/2"],
        )

    def test_extract_batch_stops_at_the_shared_deadline(self):
        mod = load_plugin("batch_deadline", f"{self.base}/err")
        clock = fast_clock(mod, advance=30.0)
        urls = [
            "https://example.invalid/1",
            "https://example.invalid/2",
            "https://example.invalid/3",
        ]
        out = mod.DeepSeekWebSearchProvider().extract(urls)
        self.assertEqual([entry["url"] for entry in out], urls)
        self.assertEqual(
            out[0]["error"],
            "DeepSeek fetch failed: HTTP 500: boom (after 1 retry)",
        )
        self.assertEqual(out[1]["error"], "DeepSeek fetch failed: HTTP 500: boom")
        self.assertEqual(
            out[2]["error"],
            "DeepSeek fetch failed: batch deadline reached",
        )
        self.assertNotIn("raw_content", out[2])
        self.assertEqual(len(Handler.requests), 3)
        self.assertEqual(clock["sleeps"], [1.0])

    def test_pages_fetched_before_the_deadline_are_kept(self):
        mod = load_plugin("batch_keep", f"{self.base}/ok")
        # Two separate claims, so neither can hide the other:
        # 1. the shipped deadline is sane — inside Hermes' default 120s extract
        #    cap, and positive (a huge value silently disables the guard, while a
        #    negative one skips every URL);
        self.assertLess(mod._EXTRACT_DEADLINE_S, 120)
        self.assertGreater(mod._EXTRACT_DEADLINE_S, 0)
        # 2. once the deadline is reached the remaining URLs are skipped and the
        #    pages already fetched stay in the list. The deadline is pinned here
        #    and stepped with a fixed 30s, so tuning the constant cannot move the
        #    goalposts while a removed deadline still fails the request count.
        mod.__dict__.update(_EXTRACT_DEADLINE_S=85.0)  # dynamic module; untyped on purpose
        fast_clock(mod, advance=30.0)
        urls = [f"https://example.invalid/{i}" for i in range(4)]
        out = mod.DeepSeekWebSearchProvider().extract(urls)
        self.assertEqual([entry["url"] for entry in out], urls)
        for entry in out[:3]:
            self.assertNotIn("error", entry)
            self.assertRegex(entry["content"], r"^# Mock content")
            self.assertEqual(entry["raw_content"], entry["content"])
        self.assertEqual(out[3].get("error"), "DeepSeek fetch failed: batch deadline reached")
        self.assertEqual(len(Handler.requests), 3)

    def test_fast_rate_limits_still_retry_inside_a_batch(self):
        mod = load_plugin("batch_429", f"{self.base}/r429")
        clock = fast_clock(mod)
        urls = [
            "https://example.invalid/1",
            "https://example.invalid/2",
            "https://example.invalid/3",
        ]
        out = mod.DeepSeekWebSearchProvider().extract(urls)
        self.assertEqual(len(out), 3)
        for entry in out:
            self.assertIn("(after 2 retries)", entry["error"])
            self.assertNotIn("batch deadline", entry["error"])
        self.assertEqual(len(Handler.requests), 9)
        self.assertEqual(clock["sleeps"], [1.0, 2.0, 1.0, 2.0, 1.0, 2.0])

    def test_retry_stops_before_a_sleep_would_cross_the_budget(self):
        mod = load_plugin("budget_edge", f"{self.base}/err")
        clock = fast_clock(mod, advance=39.5)
        with self.assertLogs(mod.__name__, level="WARNING") as captured:
            out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertEqual(out["error"], "DeepSeek search failed: HTTP 500: boom")
        self.assertEqual(len(Handler.requests), 1)
        self.assertEqual(clock["sleeps"], [])
        self.assertFalse(any("retry " in line for line in captured.output))

    def test_non_json_200_is_labelled(self):
        p = provider("err_html", f"{self.base}/html")
        out = p.search("q")
        self.assertFalse(out["success"])
        self.assertIn("non-JSON response", out["error"])
        self.assertNotIn("(after ", out["error"])

    def test_non_json_200_after_a_retry_keeps_the_count(self):
        mod = load_plugin("err_html_retry", f"{self.base}/html429")
        fast_clock(mod)
        out = mod.DeepSeekWebSearchProvider().search("q")
        self.assertFalse(out["success"])
        self.assertIn("non-JSON response", out["error"])
        self.assertTrue(out["error"].endswith("(after 1 retry)"))
        self.assertEqual(len(Handler.requests), 2)

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
        expected = {"title", "url", "description", "position",
                    "rerank_score", "authority", "date", "site_name", "site_icon"}
        for hit in out["data"]["web"]:
            self.assertEqual(set(hit), expected)

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
