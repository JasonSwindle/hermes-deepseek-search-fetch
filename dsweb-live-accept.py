#!/usr/bin/env python3
"""Live acceptance: the plugin against the real DeepSeek Search & Fetch APIs.

Everything else in this repo runs against a mock. This is the one check that the vendor actually
accepts what we send — the settings fields in particular, which no unit test can prove.

It spends money: DeepSeek bills $0.75 per 1,000 successful requests, and this makes a handful of
them. Failed calls are free. Run it only with `--spend`, and only when the code is ready to ship.

    export DEEPSEEK_API_KEY=***          # or let Hermes resolve it from its config layer
    python dsweb-live-accept.py --spend

Deliberately not wired into CI: a test that bills on every push is a test nobody keeps.

What it proves, and what it cannot:
  * search returns ranked hits carrying the vendor's quality/freshness/provenance signals;
  * `need_content` makes the vendor return each hit's page text, and that text is clean
    Markdown at the wire (issue #2), so the deliberate search/fetch asymmetry is measured;
  * `localization` is accepted by the endpoint rather than rejected as an unknown field;
  * fetch returns escaped Markdown at the wire and our unescaper turns it back into structure
    (issue #1) — the raw body is captured before `extract()` rewrites it;
  * a 129-character query is rejected with HTTP 400 and costs nothing.
It does not prove retry behaviour (that would mean provoking 429s on a real account) or anything
about a Hermes session — `dsweb-integration.py` covers the dispatcher with a mock.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# A Markdown-heavy page, the same repro as issue #1: its raw content carries
# hundreds of punctuation escapes, so the unescaper is actually exercised.
FETCH_URL = "https://en.wikipedia.org/wiki/Markdown"
EXPECTED_CHECKS = 13

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))


def _default_hermes_source() -> str:
    """The Hermes checkout to import the provider ABC from.

    ``--hermes`` (the other scripts) is the CLI binary; this is the source tree.
    macOS and per-user installs live under ~/.hermes/hermes-agent, Linux images
    often use /opt/hermes. HERMES_SOURCE overrides both.
    """
    env = os.environ.get("HERMES_SOURCE")
    if env:
        return env
    for candidate in (Path.home() / ".hermes" / "hermes-agent", Path("/opt/hermes")):
        if (candidate / "agent" / "web_search_provider.py").is_file():
            return str(candidate)
    return "/opt/hermes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spend", action="store_true",
                        help="acknowledge that this spends money on the DeepSeek account")
    parser.add_argument("--hermes-source", default=_default_hermes_source(),
                        help="Hermes source tree, for the provider ABC (default: $HERMES_SOURCE, "
                             "~/.hermes/hermes-agent, then /opt/hermes)")
    parser.add_argument("--query", default="hermes agent web search plugin",
                        help="live search query (default: a phrase with real results)")
    args = parser.parse_args()

    if not args.spend:
        print("refusing to run without --spend: these calls are billed to the DeepSeek account")
        return 2
    if not (Path(args.hermes_source) / "agent" / "web_search_provider.py").is_file():
        print(f"{args.hermes_source!r} is not a Hermes source tree; "
              "pass the checkout, not the CLI binary")
        return 2

    sys.path.insert(0, args.hermes_source)
    from agent.web_search_provider import get_provider_env  # noqa: PLC0415

    try:
        key = get_provider_env("DEEPSEEK_API_KEY")
    except Exception as exc:  # UnscopedSecretError under profile multiplexing
        print(f"could not resolve DEEPSEEK_API_KEY: {exc}")
        return 2
    if not key:
        print("no DEEPSEEK_API_KEY: export one, or let Hermes resolve it from its config layer")
        return 2
    print(f"key resolved ({len(key)} chars, not shown)\n")

    import __init__ as plugin  # noqa: PLC0415 - the plugin itself, from this repo

    calls = {"n": 0}
    raw: dict[str, Any] = {"fetch": None, "search": []}
    real_request = plugin._request

    def capturing_request(url, payload, headers, **kw):
        calls["n"] += 1
        resp = real_request(url, payload, headers, **kw)
        try:
            text = resp.text
        except Exception:
            return resp
        if url.endswith("/fetch"):
            raw["fetch"] = text
        elif url.endswith("/search"):
            raw["search"].append(text)
        return resp

    plugin._request = capturing_request

    def provider(**settings):
        return plugin.DeepSeekWebSearchProvider(lambda name: settings.get(name))

    try:
        # 1. A real search, default settings.
        out = provider().search(args.query, limit=3)
        hits = out.get("data", {}).get("web", []) if out.get("success") else []
        first = hits[0] if hits else {}
        check("live search succeeds", out.get("success") is True, str(out)[:300])
        check("live search returns hits", bool(hits), f"hits={len(hits)}")
        signals = {k: first.get(k) for k in ("rerank_score", "authority", "date", "site_name")}
        check("hits carry the vendor signals", bool(hits) and any(v is not None for v in signals.values()),
              str(signals))
        check("hits carry no content by default", bool(hits) and all("content" not in h for h in hits),
              str(first.keys()))

        # 2. need_content, which no mock can confirm, and the search-side
        #    escaping policy: issue #2 measured clean Markdown here.
        out = provider(need_content=True).search(args.query, limit=2)
        hits = out.get("data", {}).get("web", []) if out.get("success") else []
        with_content = [h for h in hits if h.get("content")]
        check("need_content returns page text", bool(with_content),
              f"hits={len(hits)} with_content={len(with_content)}")
        if with_content:
            print(f"     first hit content: {len(with_content[0]['content'])} chars, "
                  f"starts {with_content[0]['content'][:60]!r}")
        raw_search = json.loads(raw["search"][-1]) if raw["search"] else {}
        escaped_hits = [
            h for h in raw_search.get("results", [])
            if "\\#" in (h.get("content") or "") or "\\[" in (h.get("content") or "")
        ]
        check("search content is clean Markdown at the wire (no vendor escaping)",
              bool(with_content) and not escaped_hits, str(escaped_hits[:1])[:300])

        # 3. localization — the field the plugin never sent before 1.0.7.
        out = provider(localization="8.8.8.8").search(args.query, limit=2)
        check("localization is accepted by the endpoint", out.get("success") is True,
              str(out)[:300])

        # 4. A real fetch, captured raw before the plugin unescapes it.
        pages = provider().extract([FETCH_URL])
        page = pages[0] if pages else {}
        content = page.get("content") or ""
        check("live fetch succeeds", bool(content), str(page.get("error") or page)[:300])
        raw_fetch = json.loads(raw["fetch"]) if raw["fetch"] else {}
        raw_content = raw_fetch.get("content") or ""
        check("the vendor's raw body carries the punctuation escaping",
              "\\#" in raw_content or "\\[" in raw_content, repr(raw_content[:200]))
        check("the plugin unescapes the fetched Markdown",
              "\\#" not in content and "\\[" not in content, repr(content[:200]))
        check("fetched Markdown keeps a heading",
              "# " in content or "\n# " in content, repr(content[:200]))

        # 5. The documented 128-character cap: rejected, and free.
        out = provider().search("x" * 129, limit=1)
        check("a 129-character query is rejected", out.get("success") is False, str(out)[:200])
        check("the rejection names HTTP 400",
              out.get("success") is False and "400" in str(out.get("error", "")),
              str(out.get("error"))[:200])
    finally:
        plugin._request = real_request

    billable = calls["n"]
    print(f"\n{billable} HTTP call(s) made; at most ${billable * 0.00075:.5f} if all succeeded "
          f"(failures are free)")

    failed = [name for name, ok, _ in RESULTS if not ok]
    if len(RESULTS) != EXPECTED_CHECKS:
        failed.append(f"check count {len(RESULTS)} != {EXPECTED_CHECKS}")
    passed = len(RESULTS) - len([name for name, ok, _ in RESULTS if not ok])
    print(f"{passed}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
