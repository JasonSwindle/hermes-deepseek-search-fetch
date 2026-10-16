# 🔍 deepseek-search-fetch — DeepSeek Search & Fetch as a Hermes web provider

[![tests](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml/badge.svg)](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml)

Routes [Hermes Agent](https://hermes-agent.nousresearch.com/docs)'s `web_search` and `web_extract` tools through DeepSeek's [Search & Fetch APIs](https://api-docs.deepseek.com/guides/search/). One file, no dependencies, and the invariants that would otherwise fail silently are pinned by the tests in `tests/`.

- **Search** — `POST /v1/tools/search` → ranked hits with `title`, `url`, `description`, `position`
- **Fetch** — `POST /v1/tools/fetch` → Markdown per URL as `title` + `content`
- **Your key, your account** — calls go to `api.deepseek.com` and nowhere else; nothing is proxied, nothing phones home

## At a glance

| | |
|---|---|
| Plugin name | `deepseek-search-fetch` |
| Registered provider | `deepseek` — the value `web.search_backend` / `web.extract_backend` hold |
| Kind | `backend` — registers one web provider: no tools, no hooks |
| Dependencies | none beyond the standard library and the `httpx` Hermes already ships |
| Tests | 24 stdlib unit + contract tests, 6 Hypothesis property tests — offline, keyless, unbilled |
| Licence | MIT |
| Affiliation | Community plugin by Jason Swindle. **Not affiliated with DeepSeek.** DeepSeek is a trademark of its owner; this is an independent client. |

## Why this exists

Hermes ships several web backends. Its free defaults work until they don't: the managed search route and the anonymous keyless ring are shared, rate-limited, and outside your control — and when a page is blocked or a result set is thin, there is nothing to fall back *to*.

This plugin adds a **keyed, paid, deterministic** backend under your own account. You pay DeepSeek per successful call, you get the same provider every time, and if DeepSeek is unreachable or your key is rejected the call is still served — Hermes rescues it one-shot from the free keyless ring. The failure mode of enabling this is *billing*, not *breakage*.

## What actually changes when you enable it

| | Before | After |
|---|---|---|
| `web_search` | managed free Perplexity | `deepseek` |
| `web_extract` | free keyless ring (exa / parallel / firecrawl, rotating) | `deepseek` |
| On failure | — | one-shot rescue back to the free keyless ring |
| Cost | $0 | ≈$0.002 per successful call |

With both keys pinned, Hermes' `_managed_web_search()` returns `False` — the managed free route is bypassed. Reverting restores the previous routes exactly; nothing else in your config moves.

## Install

```bash
git clone https://github.com/JasonSwindle/hermes-deepseek-search-fetch \
  ~/.hermes/plugins/web/deepseek-search-fetch

hermes plugins enable web/deepseek-search-fetch
hermes config set web.search_backend deepseek
hermes config set web.extract_backend deepseek

export DEEPSEEK_API_KEY=***        # or store it in your Hermes secrets
```

Requires Hermes Agent and a DeepSeek API key; no extra packages. Changes take effect on the **next session** — a running session keeps its routing until then. Confirm with `hermes plugins list` (expect `enabled … deepseek-search-fetch`) and `hermes tools`.

**To revert:**

```bash
hermes config unset web.search_backend
hermes config unset web.extract_backend
hermes plugins disable web/deepseek-search-fetch
```

## How it works

- Hermes resolves the provider from `web.search_backend` / `web.extract_backend` (falling back to `web.backend`, then its own preference walks). This plugin registers the name `deepseek`.
- `search()` posts `{query, count}` and maps `results[]` into Hermes' hit shape, preferring each result's `summary` and falling back to `snippet`.
- `extract()` posts `{url}` **one URL per call** (the endpoint is single-URL), so multi-URL extracts run sequentially and preserve input order. Each entry carries `title`, `content`, `raw_content` and `metadata.sourceURL`.
- Failures never raise: a bad call returns an error envelope (or a per-entry `error`), which is exactly what lets Hermes' rescue logic see it and fall over to the free ring.
- `is_available()` is an environment check only — Hermes calls it on every `hermes tools` paint, so it must never touch the network.

## Costs, limits and behaviour

**Billing.** DeepSeek charges **$0.75 per 1,000 successful requests**; failed calls, empty results and timeouts are free. Measured on 2026-10-16: **≈$0.002 per successful call**, about 2.7× the list rate — the balance endpoint reports two decimals, so treat $0.00075 as a floor and budget ~3×. A ~50-call research day lands near $0.10. Check your own usage with `GET https://api.deepseek.com/user/balance`.

**Limits.** Queries are capped at **128 characters** by the API (longer → HTTP 400, free). `limit` is clamped to **1–50**. **5 requests/second** — a 429 is surfaced as an error rather than retried, and the rescue path covers it. Multi-URL extracts are bounded by Hermes' **120 s** per-provider cap.

**Behaviour worth knowing.**

- Unpaired surrogate code points in a query or URL are replaced with U+FFFD first: `httpx` raises while *encoding* the body otherwise, so the call would die before leaving the process.
- Empty search results are success with an empty list, not an error.
- `extract()` returns a **bare list** of entries. Parts of the Hermes docs show a `{"success": …, "data": […]}` envelope instead; the bundled providers return a bare list and the dispatcher consumes it that way, so this matches bundled behaviour deliberately — pinned by a test.
- A rescued **search** carries `rescued_from` / `backend_error`; a rescued **extract** does not, because Hermes drops entry metadata during serialization. Judge extract provenance from the log line (`Web extract via …`).

## Security

- The API key is read through Hermes' provider env helper (`get_provider_env`), is **never logged**, and error text is truncated to 300 characters.
- `DEEPSEEK_BASE_URL` exists for tests and is **validated**: only `https://api.deepseek.com` (https) or a loopback host (`127.0.0.1`, `localhost`, `::1`) is accepted. Another host, plaintext to the real host, link-local addresses, userinfo tricks, suffix lookalikes and non-http(s) schemes are refused with a warning, and the default endpoint is used instead. A freely redirectable base URL would hand the bearer key to whoever set the variable, so there is deliberately **no escape hatch** — anyone who can set one variable can set two.
- No file I/O, no subprocesses, no dynamic imports, no telemetry. Two outbound endpoints, both on `api.deepseek.com`.
- Like every Hermes web provider it runs inside the plugin host with the agent's environment. Treat installing any plugin as running code with your credentials.

## FAQ

**Is this official or affiliated with DeepSeek?**
No. It is an independent client of DeepSeek's public API, published as a community plugin.

**Will enabling this break my free searches?**
It changes the default, not the fallback: the free routes stop being used *first*, but a failed DeepSeek call is rescued one-shot by the free keyless ring. The real trade is paying per call.

**What if my key expires or is revoked?**
DeepSeek returns HTTP 401/403, which the plugin surfaces as an error and the dispatcher rescues from the ring. If the key is *missing*, `is_available()` is `False` and Hermes routes to a different provider instead.

**Does it send anything anywhere else?**
No. Requests go to `api.deepseek.com` only — no analytics, no telemetry, no update check. The plugin cannot move its own code, by design.

**Why is a query over 128 characters rejected?**
That is DeepSeek's API limit. The plugin passes it through and returns the 400 verbatim (free of charge) rather than silently truncating your query.

**Can I use the API without Hermes?**
Yes — the endpoints are plain HTTP with a bearer key, and this provider is a thin wrapper. See the API docs linked at the top.

**Does it work on Windows and macOS?**
There are no platform-specific imports, and CI exercises the suite on Linux, macOS and Windows (Python 3.11 and 3.14) on every push — so portability is checked rather than assumed.

**How do I run the tests?**
See [Tests](#tests) — they need no key, no network and no Hermes install.

## Tune it

| What | Where | Notes |
|---|---|---|
| Endpoint | `DEEPSEEK_BASE_URL` | Tests only; validated as described above. |
| Result cap | `_SEARCH_LIMIT_CAP` in `__init__.py` | `50` — DeepSeek's own maximum. |
| Timeout | `_TIMEOUT` in `__init__.py` | `30.0` s per HTTP call. |
| Provider name | `provides_web_providers` in `plugin.yaml` | `deepseek`; changing it changes what your config must say. |

## Tests

Offline, keyless and unbilled: a loopback mock server stands in for the vendor, and a small stub lets the plugin import **without a Hermes install**, so the suite runs anywhere.

```bash
# Zero-dependency tier (stdlib unittest only) — 25 tests
python -m unittest discover -s tests

# Full tier, adding Hypothesis property tests — 30 tests
uv venv .venv-dev
uv pip install --python .venv-dev/bin/python -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests
```

The property tier generates arbitrary Unicode — including unpaired surrogates, which Hypothesis' default `st.text()` alphabet deliberately excludes — and asserts the invariant that made it necessary: whatever a caller passes, a UTF-8-encodable value reaches the wire. Point `DEEPSEEK_PLUGIN_PATH` at another copy of the plugin to test that copy; the falsification procedure and the invariants that must not break live in [AGENTS.md](AGENTS.md).

## Keywords

hermes-agent, hermes-plugin, deepseek, deepseek-api, web-search, web-extract, search-api, fetch-api, provider-plugin, agent-tools, llm-tools, rag, python

## License

MIT — see [LICENSE](LICENSE).
