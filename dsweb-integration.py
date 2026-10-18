#!/usr/bin/env python3
"""Integration harness: the *installed* plugin, running inside Hermes' real web dispatch.

`tests/` proves the plugin's own logic with a stub in place of Hermes. It cannot prove that the
plugin matches the provider interface, how the dispatcher treats its return shapes, whether the
settings surface is read at call time, or whether a retry behaves the way the README says. This
script does, against a real Hermes install:

  1. the plugin is installed the way a stranger installs it — `hermes plugins install <repo> --ref
     <sha>` into a throwaway `HERMES_HOME` (pass `--install <sha>`; otherwise it uses what is there);
  2. a loopback mock stands in for api.deepseek.com, addressed through `DEEPSEEK_BASE_URL`, which
     the plugin's base-URL guard explicitly allows for loopback;
  3. the real `web_search_tool` / `web_extract_tool` are called, so the registry, the dispatcher, the
     search memo and the extract wrapper are all in the path.

Offline, keyless and unbilled: the key is a placeholder and every request lands on the mock. Nothing
here touches the real API — a live-acceptance run against api.deepseek.com is a separate, manual act
that spends money and needs a real key.

    python dsweb-integration.py --hermes-home /tmp/dsw-it --install <40-char-sha>
    python dsweb-integration.py --hermes-home /tmp/dsw-it          # re-run against what is installed

Requires a Hermes install to point at: `--hermes /path/to/hermes` when the CLI is not on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = "JasonSwindle/hermes-deepseek-search-fetch"
PLUGIN_ID = "deepseek-search-fetch"
ESCAPED = "\\# Heading\\n\\[link\\]\\(https\\:\\/\\/example\\.com\\) \\| a \\| b \\|"

RESULTS: list[tuple[str, bool, str]] = []
SEEN: list[dict] = []
ATTEMPTS: dict[str, int] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Handler(BaseHTTPRequestHandler):
    """A mock vendor with switchable behaviour, keyed off the query or the URL."""

    def log_message(self, format, *args):  # noqa: A002 - matches BaseHTTPRequestHandler
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        SEEN.append({"path": self.path, "body": body})
        ATTEMPTS[self.path] = ATTEMPTS.get(self.path, 0) + 1
        attempt = ATTEMPTS[self.path]

        if self.path.endswith("/search"):
            query = str(body.get("query", ""))
            if query.startswith("ratelimit") and attempt <= 2:
                return self._json(429, {"error": {"message": "Rate limit reached",
                                                  "type": "rate_limit_error", "code": "rate_limit_error"}})
            if query.startswith("always500"):
                return self._json(500, {"error": {"message": "upstream exploded",
                                                  "type": "server_error", "code": "server_error"}})
            if query.startswith("badjson"):
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"<html>not json</html>")
                return
            hits = [
                {"title": "One", "url": "https://example.com/1", "summary": "first",
                 "rerank_score": 2.1, "authority": 3, "date": "2026-10-01T00:00:00Z",
                 "site_name": "Example", "site_icon": "https://example.com/i.png"},
                {"title": "Two", "url": "https://example.com/2", "snippet": "second",
                 "rerank_score": 0.4, "authority": -1, "date": None,
                 "site_name": None, "site_icon": None},
            ]
            if body.get("need_content"):
                for index, hit in enumerate(hits):
                    hit["content"] = f"# Doc {index}"
            return self._json(200, {"results": hits})

        if self.path.endswith("/fetch"):
            url = str(body.get("url", ""))
            if "boom" in url:
                return self._json(500, {"error": {"message": "fetch failed", "type": "server_error"}})
            return self._json(200, {"title": f"Title of {url}", "content": ESCAPED})

        return self._json(404, {"error": {"message": "no such path"}})

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hermes-home", required=True,
                        help="throwaway HERMES_HOME the plugin is (or will be) installed into")
    parser.add_argument("--hermes", default=None,
                        help="path to the hermes CLI (default: the one on PATH)")
    parser.add_argument("--install", metavar="SHA",
                        help="install the plugin from GitHub pinned at this 40-character sha first")
    parser.add_argument("--hermes-source", default="/opt/hermes",
                        help="Hermes source tree to import the tools from (default: /opt/hermes)")
    args = parser.parse_args()

    hermes = args.hermes or shutil.which("hermes")
    if not hermes:
        raise SystemExit("could not find the hermes CLI on PATH; pass --hermes /path/to/hermes")
    args.hermes = hermes

    home = os.path.abspath(args.hermes_home)
    env = {**os.environ, "HERMES_HOME": home}

    def cfg(*values: str) -> None:
        run = subprocess.run([args.hermes, "config", *values], capture_output=True, text=True,
                             env=env, timeout=120)
        if run.returncode != 0:
            raise SystemExit(f"hermes config {' '.join(values)} failed: {run.stdout}{run.stderr}")

    if args.install:
        os.makedirs(home, exist_ok=True)
        run = subprocess.run([args.hermes, "plugins", "install", REPO, "--ref", args.install,
                              "--enable"], capture_output=True, text=True, env=env, timeout=600)
        if run.returncode != 0:
            raise SystemExit(f"install failed: {run.stdout}{run.stderr}")
        print(f"installed {REPO} @ {args.install} into {home}\n")

    installed = os.path.join(home, "plugins", PLUGIN_ID)
    if not os.path.isdir(installed):
        raise SystemExit(f"no plugin at {installed} — pass --install <sha> into a fresh --hermes-home")

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # The plugin reads these at import; nothing below may set them later.
    os.environ["HERMES_HOME"] = home
    os.environ["DEEPSEEK_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["DEEPSEEK_API_KEY"] = "integration-placeholder-key"
    os.environ.pop("DEEPSEEK_SEARCH_NEED_CONTENT", None)
    sys.path.insert(0, args.hermes_source)

    cfg("set", "web.search_backend", "deepseek")
    cfg("set", "web.extract_backend", "deepseek")
    cfg("set", "web.cache_enabled", "false")
    cfg("set", "web.keyless_fallback", "false")
    cfg("set", "web.keyless_rescue", "false")

    from tools.web_tools import web_extract_tool, web_search_tool  # noqa: PLC0415

    def search(query: str, limit: int = 2) -> dict:
        SEEN.clear()
        return json.loads(web_search_tool(query, limit=limit))

    def setting(key: str, value: str) -> None:
        cfg("set", f"plugins.entries.{PLUGIN_ID}.settings.{key}", value)

    setting("need_content", "true")
    out = search("setting on")
    body = SEEN[-1]["body"] if SEEN else {}
    hits = out.get("data", {}).get("web", [])
    check("installed plugin answers a search", out.get("success") is True, json.dumps(out)[:300])
    check("setting need_content=true reaches the wire", body.get("need_content") is True, json.dumps(body))
    check("hits carry the vendor signals",
          bool(hits) and hits[0].get("rerank_score") == 2.1 and hits[0].get("site_name") == "Example",
          json.dumps(hits[:1]))
    check("content is mapped when the vendor returns it",
          bool(hits) and hits[0].get("content") == "# Doc 0", json.dumps(hits[:1]))

    setting("need_content", "false")
    search("setting off")
    body = SEEN[-1]["body"] if SEEN else {}
    check("setting need_content=false keeps the wire clean", "need_content" not in body, json.dumps(body))

    setting("localization", "203.0.113.7")
    search("localised")
    body = SEEN[-1]["body"] if SEEN else {}
    check("localization reaches the wire", body.get("localization") == "203.0.113.7", json.dumps(body))
    setting("localization", "")

    ATTEMPTS.clear()
    out = search("ratelimit please")
    check("429 is retried and then succeeds", out.get("success") is True, json.dumps(out)[:300])
    check("429 took exactly three attempts", ATTEMPTS.get("/v1/tools/search") == 3,
          f"attempts={ATTEMPTS.get('/v1/tools/search')}")

    ATTEMPTS.clear()
    out = search("always500 please")
    check("a persistent 500 ends as an error envelope", out.get("success") is False, json.dumps(out)[:200])
    check("a persistent 500 stopped at three attempts", ATTEMPTS.get("/v1/tools/search") == 3,
          f"attempts={ATTEMPTS.get('/v1/tools/search')}")

    out = search("badjson please")
    check("a non-JSON 200 is an error, not a crash", out.get("success") is False, json.dumps(out)[:200])

    out = json.loads(asyncio.run(web_extract_tool(["https://example.com/a", "https://example.com/b"])))
    pages = out.get("results", out.get("data")) or []
    check("extract returns both pages in order",
          [page.get("url") for page in pages] == ["https://example.com/a", "https://example.com/b"],
          json.dumps(pages)[:300])
    if pages:
        check("content is unescaped", "\\#" not in (pages[0].get("content") or ""),
              repr(pages[0].get("content"))[:200])
        check("raw_content is unescaped too", "\\#" not in (pages[0].get("raw_content") or ""),
              repr(pages[0].get("raw_content"))[:200])
        check("markdown structure survives", "# Heading" in (pages[0].get("content") or ""),
              repr(pages[0].get("content"))[:200])

    out = json.loads(asyncio.run(web_extract_tool(["https://example.com/ok", "https://example.com/boom"])))
    pages = out.get("results", out.get("data")) or []
    ok_page = [page for page in pages if page.get("url") == "https://example.com/ok"]
    bad_page = [page for page in pages if page.get("url") == "https://example.com/boom"]
    check("a failed page does not sink the batch", bool(ok_page) and bool(ok_page[0].get("content")),
          json.dumps(pages)[:300])
    check("the failed page carries an error", bool(bad_page) and bool(bad_page[0].get("error")),
          json.dumps(pages)[:300])

    # The README's caveat, measured instead of asserted: with Hermes' search memo on, a query answered
    # before a settings flip is served from cache, in its old shape, until the TTL expires.
    cfg("set", "web.cache_enabled", "true")
    setting("need_content", "false")
    ATTEMPTS.clear()
    search("memo probe")
    setting("need_content", "true")
    second = search("memo probe")
    check("memo: the cached search is served without a second request",
          ATTEMPTS.get("/v1/tools/search") == 1, f"attempts={ATTEMPTS.get('/v1/tools/search')}")
    check("memo: the stale entry keeps its old shape (no content after the flip)",
          "content" not in json.dumps(second.get("data", {})), json.dumps(second)[:200])

    server.shutdown()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
