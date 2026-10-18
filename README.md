![deepseek-search-fetch](docs/logo.png)

# DeepSeek Search & Fetch as a Hermes web provider

[![tests](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml/badge.svg)](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hermes Agent plugin](https://img.shields.io/badge/Hermes-Agent%20plugin-8A63D2.svg)](https://github.com/NousResearch/hermes-agent)

Wires DeepSeek's [Search & Fetch APIs](https://api-docs.deepseek.com/guides/search/) into [Hermes Agent](https://hermes-agent.nousresearch.com/docs)'s `web_search` and `web_extract`. One file, no dependencies, and the invariants that would otherwise fail silently are pinned by the tests in `tests/`.

- **Search** — `POST /v1/tools/search` → ranked hits with `title`, `url`, `description`, `position`, plus the vendor's quality, freshness and provenance signals: `rerank_score` (0–3; ≥1.5 is high quality), `authority` (0–4; `-1` unknown, ≥3 authoritative), `date` (RFC 3339 UTC), `site_name` and `site_icon` — the last four `null` when the vendor omits them
- **Fetch** — `POST /v1/tools/fetch` → Markdown per URL as `title` + `content`, with the vendor's punctuation escaping reversed so headings, links and tables arrive as Markdown instead of literal `\#` / `\[`
- **Your key, your account** — calls go to `api.deepseek.com` and nowhere else; nothing is proxied, nothing phones home

## At a glance

| | |
|---|---|
| Plugin name | `deepseek-search-fetch` |
| Registered provider | `deepseek` — the value `web.search_backend` / `web.extract_backend` hold |
| Kind | `backend` — registers one web provider: no tools, no hooks |
| Dependencies | none beyond the standard library and the `httpx` Hermes already ships |
| Tests | 66 stdlib unit + contract tests, 8 Hypothesis property tests — offline, keyless, unbilled — plus `dsweb-integration.py`, 19 checks against a real Hermes install |
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
| Cost | $0 | DeepSeek's list price: $0.75 per 1,000 successful calls |

With both keys pinned, Hermes' `_managed_web_search()` returns `False` — the managed free route is bypassed. Reverting restores the previous routes exactly; nothing else in your config moves.

## Install

**From the plugin catalog** — community tier, find it with `hermes plugins search deepseek`:

```bash
hermes plugins install deepseek-search-fetch --enable
```

**Straight from this repository** (add `--ref <40-character sha>` to pin a specific reviewed commit):

```bash
hermes plugins install JasonSwindle/hermes-deepseek-search-fetch --enable
```

A git or catalog install lands as a **top-level plugin**, so it is enabled under the plugin name.
A manual clone behaves the same if you clone to `~/.hermes/plugins/deepseek-search-fetch`; cloning
under `plugins/web/` instead means the enable key is `web/deepseek-search-fetch`.

```bash
git clone https://github.com/JasonSwindle/hermes-deepseek-search-fetch \
  ~/.hermes/plugins/deepseek-search-fetch
hermes plugins enable deepseek-search-fetch
```

Then point the web tools at the provider and supply the key:

```bash
hermes config set web.search_backend deepseek
hermes config set web.extract_backend deepseek
export DEEPSEEK_API_KEY=***        # or store it in your Hermes secrets
```

Requires Hermes Agent and a DeepSeek API key; no extra packages. Changes take effect on the **next session** — a running session keeps its routing until then. Confirm with `hermes plugins list` (expect `enabled … deepseek-search-fetch`) and `hermes tools`.

**To revert:**

```bash
hermes config unset web.search_backend
hermes config unset web.extract_backend
hermes plugins disable deepseek-search-fetch
```

## How it works

- Hermes resolves the provider from `web.search_backend` / `web.extract_backend` (falling back to `web.backend`, then its own preference walks). This plugin registers the name `deepseek`.
- `search()` posts `{query, count}` — plus `need_content: true` when `DEEPSEEK_SEARCH_NEED_CONTENT` is set — and maps `results[]` into Hermes' hit shape, preferring each result's `summary` and falling back to `snippet`. Every hit also carries the vendor's `rerank_score`, `authority`, `date`, `site_name` and `site_icon` (`None` when the vendor omits them), so a result set can be filtered and attributed before anything is fetched. With the opt-in on, each hit carries the page's `content` as clean Markdown too.
- `extract()` posts `{url}` **one URL per call** (the endpoint is single-URL), so multi-URL extracts run sequentially and preserve input order. Each entry carries `title`, `content`, `raw_content` and `metadata.sourceURL`, with **both** content fields holding the unescaped Markdown (see below).
- Failures never raise: a bad call returns an error envelope (or a per-entry `error`), which is exactly what lets Hermes' rescue logic see it and fall over to the free ring.
- `is_available()` is an environment check only — Hermes calls it on every `hermes tools` paint, so it must never touch the network.

## Costs, limits and behaviour

**Billing.** DeepSeek bills **$0.75 per 1,000 successful requests**; failed calls, empty results and timeouts are free. At that rate a 50-call research day is about $0.04, and a thousand calls is 75 cents. Rates change — check DeepSeek's pricing page — and check your own usage with `GET https://api.deepseek.com/user/balance`. **No per-call figure of our own is published here:** the account this plugin was developed against also carries other traffic, so a balance delta cannot be attributed to these calls.

**Limits.** Queries are capped at **128 characters** by the API (longer → HTTP 400, free). `limit` is clamped to **1–50**. Search and Fetch are each **5 requests/second** per account. HTTP 408, 429 and any 5xx are retried up to twice on that same call: the waits are 1s and then 2s, each plus up to 0.25s of jitter. The **40 s budget bounds the whole call, not just the wait** — a retry is only scheduled when the attempt it schedules still fits, so a search that meets a slow 5xx ends in about 40 s rather than spending a further 30 s attempt past it. A `Retry-After` in seconds is honoured whenever the call can afford the wait, replacing that attempt's wait entirely; when it asks for more time than the budget has left, the call stops retrying rather than waiting less than the server asked. (An HTTP-date, zero or a negative value is ignored and the backoff stands.) A 402 is sent once and tells you to top up the account. Every other 4xx is sent once. Whatever still fails comes back as an error envelope, which Hermes can rescue from the free ring.

A multi-URL extract stops starting requests **110 s** after it began, and each request is given only the time the deadline has left — `httpx` timeouts are per connect/read, not wall-clock, so a body that trickles in can outlive a nominal 30 s timeout. Worst case is 110 s plus the 0.5 s floor, inside Hermes' default **120 s** cap, so pages already fetched are returned instead of the batch being discarded. That margin is **only safe while `web.extract_timeout` is at least 120**: the plugin cannot read that setting, so if you lower it, lower the `extract_deadline` setting to match (it clamps to 5–115 s).

**Behaviour worth knowing.**

- Unpaired surrogate code points in a query or URL are replaced with U+FFFD first: `httpx` raises while *encoding* the body otherwise, so the call would die before leaving the process.
- **Fetched Markdown arrives backslash-escaped, and this plugin reverses it.** `/v1/tools/fetch` escapes every ASCII punctuation character in the page text — a page's `# Heading` comes back as `\# Heading`, links as `\[text\]\(https\:\/\/…\)`, table pipes as `\|` — so the text renders as punctuation rather than structure. `extract()` unescapes backslash-plus-punctuation in **both** `content` and `raw_content`. Both, not one: Hermes prefers `raw_content` when it derives the model-visible content and when it writes its extract cache, so a half-fix would keep serving escaped text out of the cache. Letter escapes survive untouched (`\n`, `\d+`, `C:\path`), and a page whose own source deliberately escaped punctuation will have that escape removed too — the vendor's escaping is uniform, so no rule can tell the two apart.
- **`ori_question` is the one documented Search field still not forwarded.** It is "the user's original question that triggered the search", and Hermes' provider interface hands `search()` only `(query, limit)`: the question never reaches the plugin, so echoing `query` into `ori_question` would add nothing. `localization` is now forwarded from the plugin's settings (see [Settings](#settings)); `need_content` likewise.
- Empty search results are success with an empty list, not an error.
- **Search returns metadata only unless you ask for content.** `need_content` is a per-request option on the Search API, and this plugin sends it only when the `need_content` setting is on (or, unset, when `DEEPSEEK_SEARCH_NEED_CONTENT` is truthy) — one search then carries the pages' text, where getting the same text otherwise costs one Fetch call per URL. It is off by default because that text is large — **a live run on 2026-10-18 returned 20,176 characters for a single hit** — and Hermes' own `web_search` contract is metadata-only ("use `web_extract` for page content"), which budgets and truncates what `web_search` does not. When it is on, the search endpoint's `content` needs no unescaping — that escaping is a fetch-endpoint behaviour.
- `extract()` returns a **bare list** of entries. Parts of the Hermes docs show a `{"success": …, "data": […]}` envelope instead; the bundled providers return a bare list and the dispatcher consumes it that way, so this matches bundled behaviour deliberately — pinned by a test.
- A rescued **search** carries `rescued_from` / `backend_error`; a rescued **extract** does not, because Hermes drops entry metadata during serialization. Judge extract provenance from the log line (`Web extract via …`).

## Security

- The API key is read through Hermes' provider env helper (`get_provider_env`), is **never logged**, and the vendor's error text is truncated to 300 characters. The HTTP status, the 402 top-up sentence and a retry suffix are written after that cap.
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

## Settings

The plugin declares its own settings, so they appear in Hermes' plugin form — Desktop → Settings → Plugins → DeepSeek Search & Fetch, or `hermes plugins manage settings` in the terminal — rather than being hand-written into `config.yaml`.

| Setting | Type | Default | Effect |
|---|---|---|---|
| `need_content` | boolean | off | Sends `need_content: true`: every search hit carries its page text as clean Markdown. Costs kilobytes per result — **a live run returned 20 KB for a single hit** — so a `limit` of 5 can reach tens of KB of context. |
| `localization` | string | empty | An IP address the Search API localises results to. Empty sends nothing; a value that is not an IP is ignored with a warning rather than sent and billed as a 400. |
| `extract_deadline` | integer | 110 | Seconds one multi-URL `extract()` may spend starting requests. Clamped to 5–115. Lower it to match a lowered `web.extract_timeout`; the plugin cannot read that setting. |

A setting is read on every search, and Hermes' config cache is keyed on the config file's signature, so a change lands on the **next search** — no session restart. One caveat: Hermes caches successful search responses for `web.cache_ttl_minutes` (20 by default) against the query, so a query already answered before the change keeps its old shape until that entry expires.

Settings live under `plugins.entries.<id>.settings.<key>`, and `<id>` depends on how the plugin was installed — a catalog or git install lands as `deepseek-search-fetch`, a manual clone under `plugins/web/` as `web/deepseek-search-fetch`. Use the form, which resolves the id; `hermes plugins list` shows it if you must set a value by path.

## Tune it

| What | Where | Notes |
|---|---|---|
| Endpoint | `DEEPSEEK_BASE_URL` | Tests only; validated as described above. |
| Page content on search results | `need_content` setting, or `DEEPSEEK_SEARCH_NEED_CONTENT` | Off by default. The setting wins when it is set; the environment variable is the fallback for hosts without a settings surface, accepting `1` (or `true`/`yes`/`on`). Either one sends `need_content: true` — every hit then carries its page text as clean Markdown, at 2–5 KB per result in the tool result and the context window. |
| Result cap | `_SEARCH_LIMIT_CAP` in `__init__.py` | `50` — DeepSeek's own maximum. |
| Timeout | `_TIMEOUT` in `__init__.py` | `30.0` s per HTTP call. |
| Transient retries | `_RETRY_BACKOFF_S`, `_RETRY_JITTER_S`, `_RETRY_BUDGET_S` in `__init__.py` | One further attempt per backoff entry (currently 1s then 2s), plus up to 0.25s of jitter. The 40s budget bounds the call: a retry is only scheduled when that attempt still fits, so a slow 5xx ends the call instead of spending 30s past the budget. A `Retry-After` in seconds replaces that attempt's wait when affordable, and ends the retries when it is not. |
| Extract batch deadline | `extract_deadline` setting, `_EXTRACT_DEADLINE_S` in `__init__.py` | `110` s from the start of one `extract()`, and each request gets only the time left (httpx timeouts are per read, not wall-clock). Later URLs are not requested; pages already fetched stay in the list. Must stay below `web.extract_timeout − 0.5`. |
| Provider name | `provides_web_providers` in `plugin.yaml` | `deepseek`; changing it changes what your config must say. |

## Tests

Offline, keyless and unbilled: a loopback mock server stands in for the vendor, and a small stub lets the plugin import **without a Hermes install**, so the suite runs anywhere.

```bash
# Zero-dependency tier (stdlib unittest only) — 67 tests (66 + a skip placeholder)
python -m unittest discover -s tests

# Full tier, adding Hypothesis property tests — 74 tests
uv venv .venv-dev
uv pip install --python .venv-dev/bin/python -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests
```

Neither tier loads Hermes, so neither can prove the plugin matches the provider interface or how the
dispatcher treats it. That is what `dsweb-integration.py` is for: it installs the plugin into a
throwaway `HERMES_HOME` the way a stranger does, points `DEEPSEEK_BASE_URL` at a loopback mock (which
the base-URL guard allows) and drives the real `web_search` / `web_extract` path — 19 checks covering
the settings surface, retry behaviour, extract unescaping, per-URL errors and the search-memo caveat.
Offline, keyless and unbilled; run it by hand before a release:

```bash
python dsweb-integration.py --hermes-home /tmp/dsw-it --hermes /path/to/hermes \
  --install <40-char-sha>
```

Neither of those touches the vendor, so neither can show that DeepSeek accepts what we send.
`dsweb-live-accept.py` is the one run that can, and it **spends money** — about $0.004 for its five
calls at list price. Run it once before a release, never in CI:

```bash
python dsweb-live-accept.py --spend
```

It proves a live search returns hits with the vendor's signals, that `need_content` really returns
page text, that `localization` is accepted rather than rejected as an unknown field, that fetched
Markdown comes back unescaped, and that a 129-character query is refused with HTTP 400.

The property tier generates arbitrary Unicode — including unpaired surrogates, which Hypothesis' default `st.text()` alphabet deliberately excludes — and asserts the invariant that made it necessary: whatever a caller passes, a UTF-8-encodable value reaches the wire. Point `DEEPSEEK_PLUGIN_PATH` at another copy of the plugin to test that copy; the falsification procedure and the invariants that must not break live in [AGENTS.md](AGENTS.md).

## Keywords

hermes-agent, hermes-plugin, deepseek, deepseek-api, web-search, web-extract, search-api, fetch-api, provider-plugin, agent-tools, llm-tools, rag, python

## License

MIT — see [LICENSE](LICENSE).
