#!/usr/bin/env python3
"""Property-based tests (Hypothesis) — the dev-only tier.

Run (from the checks dir):

    .venv-dev/bin/python -m unittest discover -s tests -v

Skipped automatically when hypothesis is absent, so the stdlib tier still runs
under the Hermes venv with zero dependencies (see requirements-dev.txt).

Why these exist: the chaos pass found, by hand, that a query containing an
unpaired surrogate made httpx raise *before* the request left the process —
a total failure for that input. Hypothesis generates exactly that class of input
on request (`st.text()`'s default alphabet deliberately excludes surrogates,
which is why the docs point at `st.characters()` for surrogate bugs) and then
shrinks a failure to the minimal reproducing string. Invariants here, not
examples.
"""
import importlib.util
import itertools
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HAVE_HYPOTHESIS = True
except ImportError:  # pragma: no cover - dev extra not installed
    HAVE_HYPOTHESIS = False

# Repo default: the plugin sits at the repo root, one level up from this tests/ dir.
PLUGIN = Path(os.environ.get("DEEPSEEK_PLUGIN_PATH") or Path(__file__).resolve().parent.parent / "__init__.py")
DUMMY_KEY = "property-test-key"
DEFAULT_PAYLOAD = json.dumps(
    {"results": [{"title": "t", "url": "https://x.invalid/", "summary": "s"}]}
).encode()
_counter = itertools.count()


def install_stub() -> None:
    if "agent.web_search_provider" in sys.modules:
        return
    import types

    pkg = types.ModuleType("agent")
    pkg.__path__ = []  # type: ignore[attr-defined]
    mod = types.ModuleType("agent.web_search_provider")

    class WebSearchProvider:
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


def load(base_url: str):
    os.environ["DEEPSEEK_BASE_URL"] = base_url
    os.environ["DEEPSEEK_API_KEY"] = DUMMY_KEY
    install_stub()
    name = f"prop_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pure_module():
    """A loaded module for the pure (no-HTTP) properties; loaded once."""
    if not hasattr(pure_module, "_mod"):
        pure_module._mod = load("http://127.0.0.1:9")  # never used by pure properties
    return pure_module._mod


# ── dynamic mock vendor ───────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    seen: list = []
    payload: bytes = DEFAULT_PAYLOAD

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        type(self).seen.append({"path": self.path, "body": body})
        if self.path.endswith("/tools/fetch"):
            try:
                url = json.loads(body.decode("utf-8")).get("url", "")
            except Exception:  # noqa: BLE001
                url = ""
            out = json.dumps({"title": "t", "content": f"c:{url}"}).encode()
        else:
            out = type(self).payload
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if HAVE_HYPOTHESIS:
    # st.text() alone excludes category Cs (surrogates); st.characters() includes them.
    ANY_TEXT = st.text(alphabet=st.characters())
    UTF8_TEXT = st.text(alphabet=st.characters(codec="utf-8"))
    FAST = settings(deadline=None, max_examples=200)
    HTTP_OK = settings(deadline=None, max_examples=25, suppress_health_check=[HealthCheck.too_slow])

    class TestUnicodeInvariants(unittest.TestCase):
        """The invariants the chaos pass had to discover by hand."""

        @classmethod
        def setUpClass(cls):
            cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=cls.server.serve_forever, daemon=True).start()
            cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

        @classmethod
        def tearDownClass(cls):
            cls.server.shutdown()
            cls.server.server_close()

        def setUp(self):
            Handler.payload = DEFAULT_PAYLOAD
            Handler.seen.clear()

        # -- pure -------------------------------------------------------------
        @given(ANY_TEXT)
        @settings(FAST)
        def test_sanitizer_is_idempotent_and_encodable(self, text):
            once = pure_module()._utf8_safe(text)
            self.assertEqual(pure_module()._utf8_safe(once), once)     # idempotent
            once.encode("utf-8")                                       # encodable
            self.assertFalse(
                any(0xD800 <= ord(c) <= 0xDFFF for c in once),
                "no unpaired surrogate may survive sanitising",
            )

        @given(UTF8_TEXT)
        @settings(FAST)
        def test_valid_text_is_untouched(self, text):
            self.assertEqual(pure_module()._utf8_safe(text), text)

        # -- wire -------------------------------------------------------------
        @given(ANY_TEXT)
        @settings(HTTP_OK)
        def test_search_sends_an_encodable_query(self, query):
            """Whatever the caller passes, a UTF-8-encodable query reaches the wire."""
            mod = load(self.base)
            out = mod.DeepSeekWebSearchProvider().search(query, limit=3)
            self.assertTrue(out["success"], out)
            sent = json.loads(Handler.seen[-1]["body"].decode("utf-8"))["query"]
            self.assertEqual(sent, mod._utf8_safe(query))
            sent.encode("utf-8")

        @given(ANY_TEXT)
        @settings(HTTP_OK)
        def test_extract_sends_an_encodable_url(self, path):
            mod = load(self.base)
            url = f"https://example.invalid/{path}"
            out = mod.DeepSeekWebSearchProvider().extract([url])
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["url"], mod._utf8_safe(url))
            json.loads(Handler.seen[-1]["body"].decode("utf-8"))["url"].encode("utf-8")

        @given(st.integers())
        @settings(HTTP_OK)
        def test_limit_is_always_within_the_api_range(self, limit):
            out = load(self.base).DeepSeekWebSearchProvider().search("q", limit=limit)
            self.assertTrue(out["success"], out)
            sent = json.loads(Handler.seen[-1]["body"].decode("utf-8"))
            self.assertEqual(sent["count"], min(max(limit, 1), 50))
            self.assertGreaterEqual(sent["count"], 1)
            self.assertLessEqual(sent["count"], 50)

        @given(st.lists(st.tuples(ANY_TEXT, ANY_TEXT), max_size=4))
        @settings(HTTP_OK)
        def test_response_text_is_mapped_verbatim(self, rows):
            results = [
                {"title": title, "url": f"https://x.invalid/{i}", "summary": summary}
                for i, (title, summary) in enumerate(rows)
            ]
            Handler.payload = json.dumps({"results": results}, ensure_ascii=True).encode()
            out = load(self.base).DeepSeekWebSearchProvider().search("q", limit=3)
            self.assertTrue(out["success"], out)
            web = out["data"]["web"]
            self.assertEqual([h["title"] for h in web], [r["title"] for r in results])
            self.assertEqual([h["description"] for h in web], [r["summary"] for r in results])
            self.assertEqual([h["position"] for h in web], list(range(1, len(results) + 1)))

else:  # pragma: no cover - exercised under the Hermes venv, which has no hypothesis
    class TestUnicodeInvariants(unittest.TestCase):
        @unittest.skip("hypothesis not installed — dev extra: requirements-dev.txt")
        def test_property_tier_skipped(self):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
