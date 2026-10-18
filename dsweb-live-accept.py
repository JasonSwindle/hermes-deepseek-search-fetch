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
  * `need_content` makes the vendor return each hit's page text;
  * `localization` is accepted by the endpoint rather than rejected as an unknown field;
  * fetch returns Markdown and our unescaper turns the vendor's escaping back into structure;
  * a 129-character query is rejected with HTTP 400 and costs nothing.
It does not prove retry behaviour (that would mean provoking 429s on a real account) or anything
about a Hermes session — `dsweb-integration.py` covers the dispatcher with a mock.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, "/opt/hermes")  # agent.web_search_provider; override with --hermes-source
sys.path.insert(0, str(REPO))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ((" — " + detail) if detail and not ok else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spend", action="store_true",
                        help="acknowledge that this spends money on the DeepSeek account")
    parser.add_argument("--hermes-source", default="/opt/hermes",
                        help="Hermes source tree, for the provider ABC (default: /opt/hermes)")
    parser.add_argument("--query", default="hermes agent web search plugin",
                        help="live search query (default: a phrase with real results)")
    args = parser.parse_args()

    if not args.spend:
        print("refusing to run without --spend: these calls are billed to the DeepSeek account")
        return 2

    sys.path.insert(0, args.hermes_source)
    from agent.web_search_provider import get_provider_env  # noqa: PLC0415

    key = get_provider_env("DEEPSEEK_API_KEY")
    if not key:
        print("no DEEPSEEK_API_KEY: export one, or let Hermes resolve it from its config layer")
        return 2
    print(f"key resolved ({len(key)} chars, not shown)\n")

    import __init__ as plugin  # noqa: PLC0415 - the plugin itself, from this repo

    calls = {"n": 0}
    real_request = plugin._request

    def counting_request(*a, **kw):
        calls["n"] += 1
        return real_request(*a, **kw)

    plugin._request = counting_request

    def provider(**settings):
        return plugin.DeepSeekWebSearchProvider(
            lambda name: settings.get(name)
        )

    # 1. A real search, default settings.
    out = provider().search(args.query, limit=3)
    hits = out.get("data", {}).get("web", []) if out.get("success") else []
    check("live search succeeds", out.get("success") is True, str(out)[:300])
    check("live search returns hits", len(hits) >= 1, f"hits={len(hits)}")
    if hits:
        signals = {k: hits[0].get(k) for k in ("rerank_score", "authority", "date", "site_name")}
        check("hits carry the vendor signals", any(v is not None for v in signals.values()),
              str(signals))
        check("hits carry no content by default", all("content" not in h for h in hits),
              str(hits[0].keys()))

    # 2. need_content, which no mock can confirm.
    out = provider(need_content=True).search(args.query, limit=2)
    hits = out.get("data", {}).get("web", []) if out.get("success") else []
    with_content = [h for h in hits if h.get("content")]
    check("need_content returns page text", bool(with_content),
          f"hits={len(hits)} with_content={len(with_content)}")
    if with_content:
        print(f"     first hit content: {len(with_content[0]['content'])} chars, "
              f"starts {with_content[0]['content'][:60]!r}")

    # 3. localization — the field the plugin never sent before this release.
    out = provider(localization="8.8.8.8").search(args.query, limit=2)
    check("localization is accepted by the endpoint", out.get("success") is True,
          str(out)[:300])

    # 4. A real fetch, and the unescaping that only makes sense on real Markdown.
    pages = provider().extract(["https://example.com/"])
    page = pages[0] if pages else {}
    check("live fetch succeeds", bool(page.get("content")),
          str(page.get("error") or page)[:300])
    content = page.get("content") or ""
    check("fetched text is unescaped", "\\#" not in content and "\\[" not in content,
          repr(content[:200]))
    check("fetched text keeps its structure", "\n" in content or "# " in content,
          repr(content[:200]))

    # 5. The documented 128-character cap: rejected, and free.
    out = provider().search("x" * 129, limit=1)
    check("a 129-character query is rejected", out.get("success") is False, str(out)[:200])
    if out.get("success") is False:
        check("the rejection names HTTP 400", "400" in str(out.get("error", "")),
              str(out.get("error"))[:200])

    plugin._request = real_request
    billable = calls["n"]
    print(f"\n{billable} HTTP call(s) made; at most ${billable * 0.00075:.5f} if all succeeded "
          f"(failures are free)")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
