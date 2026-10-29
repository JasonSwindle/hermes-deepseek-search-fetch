![deepseek-search-fetch](docs/logo.png)

# DeepSeek Search & Fetch as a Hermes web provider

[![tests](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml/badge.svg)](https://github.com/JasonSwindle/hermes-deepseek-search-fetch/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hermes Agent plugin](https://img.shields.io/badge/Hermes-Agent%20plugin-8A63D2.svg)](https://github.com/NousResearch/hermes-agent)
[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)

Wires DeepSeek's [Search & Fetch APIs](https://api-docs.deepseek.com/guides/search/) into [Hermes Agent](https://hermes-agent.nousresearch.com/docs)'s `web_search` and `web_extract`. One file, no dependencies, and the invariants that would otherwise fail silently are pinned by the tests in `tests/`.

- **Search** — `POST /v1/tools/search` → ranked hits with `title`, `url`, `description`, `position`, plus the vendor's quality, freshness and provenance signals: `rerank_score` (0–3; ≥1.5 is high quality), `authority` (0–4; `-1` unknown, ≥3 authoritative), `date` (RFC 3339 UTC), `site_name` and `site_icon` — all five are `null` when the vendor omits them
- **Fetch** — `POST /v1/tools/fetch` → Markdown per URL as `title` + `content`, with the vendor's punctuation escaping reversed so headings, links and tables arrive as Markdown instead of literal `\#` / `\[`
- **Your key, your account** — the plugin's calls go to `api.deepseek.com` and nowhere else; nothing is proxied, nothing phones home (Hermes' optional keyless rescue is a separate, upstream call — see the security section)

## At a glance

| | |
|---|---|
| Plugin name | `deepseek-search-fetch` |
| Registered provider | `deepseek` — the value `web.search_backend` / `web.extract_backend` hold |
| Kind | `backend` — registers one web provider: no tools, no hooks |
| Dependencies | none beyond the standard library and the `httpx` Hermes already ships |
| Tests | 112 stdlib unit + contract tests, 11 Hypothesis property tests — offline, keyless, unbilled — plus `dsweb-integration.py`, 22 checks against a real Hermes install |
| Licence | MIT |
| Affiliation | Community plugin by Jason Swindle. **Not affiliated with DeepSeek.** DeepSeek is a trademark of its owner; this is an independent client. |

## Why this exists

Hermes ships several web backends. Its free defaults work until they don't: the managed search route and the anonymous keyless ring are shared, rate-limited, and outside your control — and when a page is blocked or a result set is thin, there is nothing to fall back *to*.

This plugin adds a **keyed, paid, deterministic** backend under your own account. You pay DeepSeek per successful call, you get the same provider every time, and if DeepSeek is unreachable or your key is rejected the call can still be served — Hermes' optional, one-shot keyless rescue may fall back to the free ring. The failure mode of enabling this is *billing*, not *breakage*.

## What actually changes when you enable it

| | Before | After |
|---|---|---|
| `web_search` | managed free Perplexity | `deepseek` |
| `web_extract` | free keyless ring (exa / parallel / firecrawl, rotating) | `deepseek` |
| On failure | — | one-shot rescue back to the free keyless ring (when `web.keyless_rescue` is on) |
| Cost | $0 | DeepSeek's list price: $0.75 per 1,000 successful calls |

With both keys pinned, Hermes' `_managed_web_search()` returns `False` — the managed free route is bypassed. Un-pinning returns those two keys to Hermes' default/autodetect; if you had other backends pinned, set them back deliberately.

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

Requires **Hermes Agent 0.22.0 or newer** (the manifest declares `requires_hermes: ">=0.22.0"`, matching the catalog entry, because the settings use the 0.22 plugin-settings surface; an older host refuses to load it instead of silently dropping the settings) and a DeepSeek API key; no extra packages. Installing or enabling the plugin takes effect on the **next session**; changing `web.search_backend` / `web.extract_backend` or a setting on an already-loaded plugin is picked up on the next tool call (subject to the search cache). Confirm with `hermes plugins list` (expect `enabled … deepseek-search-fetch`) and `hermes tools`.

**To revert:**

```bash
hermes config unset web.search_backend
hermes config unset web.extract_backend
hermes plugins disable deepseek-search-fetch
```

That removes the DeepSeek pins and returns those keys to Hermes' default/autodetect; it does not restore a different backend you had pinned before.

## How it works

- Hermes resolves the provider from `web.search_backend` / `web.extract_backend` (falling back to `web.backend`, then its own preference walks). This plugin registers the name `deepseek`.
- `search()` posts `{query, count}`, plus `need_content: true` when the `need_content` setting (or, unset, `DEEPSEEK_SEARCH_NEED_CONTENT`) is on, and `localization` when the setting holds an IP. The query is forwarded verbatim, including DeepSeek's documented query operators (`site:`, `-site:`, `lang:`, `after:`, `before:`, quoted phrases and excluded terms) — the plugin only replaces unpaired surrogates. It maps `results[]` into Hermes' hit shape, preferring each result's `summary` and falling back to `snippet`. Every hit also carries the vendor's `rerank_score`, `authority`, `date`, `site_name` and `site_icon` (`None` when the vendor omits them), so a result set can be filtered and attributed before anything is fetched. With the opt-in on, each hit carries the page's `content` as clean Markdown too.
- `extract()` posts `{url}` **one URL per call** (the endpoint is single-URL), so multi-URL extracts run sequentially and preserve input order. Each entry carries `title`, `content`, `raw_content` and `metadata.sourceURL`, with **both** content fields holding the unescaped Markdown (see below). A bare string is treated as one URL and a non-iterable as one entry, so a caller mistake cannot cause a raise or a fetch per character.
- Failures never raise: a bad call returns an error envelope (or a per-entry `error`), which is exactly what lets Hermes' rescue logic see it and fall over to the free ring.
- `is_available()` is an environment check only — Hermes calls it on every `hermes tools` paint, so it must never touch the network. A profile-scoped secret refusal from `get_provider_env` is reported as unavailable/error, not raised.

## Costs, limits and behaviour

**Billing.** DeepSeek bills **$0.75 per 1,000 successful requests**; failed calls, empty results and timeouts are free. At that rate a 50-call research day is about $0.04, and a thousand calls is 75 cents. Rates change — check DeepSeek's pricing page — and check your own usage with `GET https://api.deepseek.com/user/balance`. **No per-call figure of our own is published here:** the account this plugin was developed against also carries other traffic, so a balance delta cannot be attributed to these calls.

**Limits.** Queries are capped at **128 characters** by the API (longer → HTTP 400, free). `limit` is clamped to **1–50**; Hermes' tool schema allows up to 100, so a request above 50 returns 50. The official guide (checked 2026-10-29) lists **100 QPS for Search and 25 QPS for Fetch** per account, with a per-domain cap on Fetch. Those documented caps were raised from **5/5** around 2026-10-19 (the archived 2026-10-18 guide says 5/5), so the earlier QPS-5 429 predates the raise rather than reflecting a lower per-account ceiling. HTTP 408, 429 and any 5xx are retried up to twice on that same call: the waits are 1s and then 2s, each plus up to 0.25s of jitter. The **40 s budget bounds scheduling, not a single attempt** — a retry is only scheduled when the attempt it schedules still fits, so a slow 5xx can end at its own 30 s timeout but the call will not spend a further 30 s attempt past the budget. A `Retry-After` in seconds is honoured whenever the call can afford the wait, replacing that attempt's wait entirely; when it asks for more time than the budget has left, the call stops retrying rather than waiting less than the server asked. (An HTTP-date, zero or a negative value is ignored and the backoff stands. DeepSeek sent **no `Retry-After`** on the live 429s below; the handling is defensive.) A 402 is sent once and tells you to top up the account. Every other 4xx is sent once. Whatever still fails comes back as an error envelope, which Hermes can rescue from the free ring.

**429s, observed live (2026-10-19).** A 30-request same-domain Fetch burst produced three 429s whose body said `Your current QPS: 25` — matching the guide — with no `Retry-After` header. The 2026-10-18 QPS-5 observation predates the documented 5→25/100 raise, so it is not evidence of a lower account ceiling. In a separate 40-request concurrent `extract()` burst, 13 calls hit a real 429 and **all 40 recovered on the first 1s+jitter retry** (observed sleeps 1.03–1.21s). These probes were run by hand against a real key, never in CI.

A multi-URL extract stops starting requests at the **effective deadline** (the `extract_deadline` setting, 110 s by default, clamped to 5–115, and additionally clamped to `web.extract_timeout − 1.5 s` when that public setting is readable — numeric strings and values below 5 s included, matching Hermes' own `float()` coercion; 0 or negative means Hermes has no cap). The request itself is **streamed and checked against a wall clock**, because `httpx` timeouts are per connect/read: a body that trickles in can outlive a nominal 30 s timeout. Each attempt gets only the time the deadline has left, and an in-flight body is aborted at that deadline, so worst case is the effective deadline plus the 0.5 s floor — inside Hermes' **120 s** default, so pages already fetched are returned instead of the batch being discarded. Lower `extract_deadline` if you want a shorter upper bound; the web-timeout clamp is automatic when Hermes' public config is readable.

**Behaviour worth knowing.**

- Unpaired surrogate code points in a query or URL are replaced with U+FFFD first: `httpx` raises while *encoding* the body otherwise, so the call would die before leaving the process.
- **Response text is repaired the same way.** A vendor/buggy response carrying a lone surrogate (JSON `\ud800`) is mapped through U+FFFD too, so Hermes' own `json.dumps(..., ensure_ascii=False)`/UTF-8 serialization cannot fail after the plugin returns. A malformed row is skipped rather than discarding the whole result set, and a non-object body / non-list `results` is a clear `DeepSeek search failed: …` error.
- **Fetched Markdown arrives backslash-escaped, and this plugin reverses it.** `/v1/tools/fetch` escapes every ASCII punctuation character in the page text — a page's `# Heading` comes back as `\# Heading`, links as `\[text\]\(https\:\/\/…\)`, table pipes as `\|` — so the text renders as punctuation rather than structure. This was measured live (1,416 escapes on a 21,407-character page, issue #1); **DeepSeek does not document the escaping**, so it is unversioned vendor behaviour that no changelog entry would flag — the live acceptance's raw-wire assertion is the tripwire. `extract()` unescapes backslash-plus-punctuation in **both** `content` and `raw_content`. Both, not one: Hermes prefers `raw_content` when it derives the model-visible content and when it writes its extract cache, so a half-fix would keep serving escaped text out of the cache. Letter escapes survive untouched (`\n`, `\d+`, `C:\path`), and a page whose own source deliberately escaped punctuation will have that escape removed too — the vendor's escaping is uniform, so no rule can tell the two apart.
- **`ori_question` is the one documented Search field still not forwarded.** It is "the user's original question that triggered the search", and Hermes' provider interface hands `search()` only `(query, limit)`: the question never reaches the plugin, so echoing `query` into `ori_question` would add nothing. `localization` is now forwarded from the plugin's settings (see [Settings](#settings)); `need_content` likewise.
- Empty search results are success with an empty list, not an error.
- **Search returns metadata only unless you ask for content.** `need_content` is a per-request option on the Search API, and this plugin sends it only when the `need_content` setting is on (or, unset, when `DEEPSEEK_SEARCH_NEED_CONTENT` is truthy) — one search then carries the pages' text, where getting the same text otherwise costs one Fetch call per URL. It is off by default because that text is large — **a live run on 2026-10-18 returned 20,176 characters for a single hit** (typically a few KB, but budget for the outlier) — and Hermes' own `web_search` contract is metadata-only ("use `web_extract` for page content"), which budgets and truncates what `web_search` does not. When it is on, the search endpoint's `content` needs no unescaping: issue #2 measured zero escapes, unlike the fetch endpoint. A failed settings read is **off**, not an environment fallback.
- `extract()` returns a **bare list** of entries. Parts of the Hermes docs show a `{"success": …, "data": […]}` envelope instead; the bundled providers return a bare list and the dispatcher consumes it that way, so this matches bundled behaviour deliberately — pinned by a test.
- A rescued **search** carries `rescued_from` / `backend_error`; a rescued **extract** does not, because Hermes drops entry metadata during serialization. Judge extract provenance from the log line (`Web extract via …`).

## Security

- The API key is read through Hermes' provider env helper (`get_provider_env`), is **never logged**, and the vendor's error text is truncated to 300 characters. The HTTP status, the 402 top-up sentence and a retry suffix are written after that cap. A profile-scoped refusal (`UnscopedSecretError`) becomes an unavailable/error result, never a traceback and never another profile's key.
- `DEEPSEEK_BASE_URL` exists for tests and is **validated**: only the exact origin `https://api.deepseek.com` (https, no userinfo, port 443/absent, no path/query/fragment) or a loopback host (`127.0.0.1`, `localhost`, `::1`) is accepted. Another host, plaintext to the real host, link-local addresses, userinfo tricks, suffix lookalikes, malformed values and non-http(s) schemes are refused with a warning, and the default endpoint is used instead. The warning names only the scheme/host, never the raw value, which could carry a credential. A freely redirectable base URL would hand the bearer key to whoever set the variable, so there is deliberately **no escape hatch** — anyone who can set one variable can set two.
- No file I/O in the plugin's own code, no subprocesses, no dynamic imports, no telemetry. Two outbound endpoints, both on `api.deepseek.com`; key resolution itself goes through Hermes' config/.env layer.
- Like every Hermes web provider it runs inside the plugin host with the agent's environment. Treat installing any plugin as running code with your credentials. Hermes' keyless rescue, if enabled, is an upstream call that may send a failed query/URL to the free ring.

## FAQ

**Is this official or affiliated with DeepSeek?**
No. It is an independent client of DeepSeek's public API, published as a community plugin.

**Will enabling this break my free searches?**
It changes the default, not the fallback: the free routes stop being used *first*, but a failed DeepSeek call can be rescued one-shot by the free keyless ring when `web.keyless_rescue` is on. The real trade is paying per call.

**What if my key expires or is revoked?**
An expired/revoked key returns HTTP 401 (auth) and the plugin surfaces it as an error; a Fetch 403 is the vendor's security-policy refusal, not an auth error. If you installed with the explicit `web.search_backend` / `web.extract_backend` pins (the instructions above), Hermes does **not** probe availability or reroute: the tool call returns the plugin's error (or the optional keyless rescue serves it). If the key is *missing* on an autodetected install, `is_available()` is `False` and Hermes picks a different provider.

**Does it send anything anywhere else?**
The plugin's own calls go to `api.deepseek.com` only — no analytics, no telemetry, no update check. If a call fails and Hermes' keyless rescue is enabled, Hermes (not the plugin) may send that query or those URLs to the free ring providers; disable `web.keyless_rescue` if that matters to you. The plugin cannot move its own code, by design.

**Why is a query over 128 characters rejected?**
That is DeepSeek's API limit. The plugin passes it through and surfaces the vendor's 400 message (prefixed and capped at 300 characters, free of charge) rather than silently truncating your query.

**Can I use the API without Hermes?**
Yes — the endpoints are plain HTTP with a bearer key, and this provider is a thin wrapper. See the API docs linked at the top.

**Does it work on Windows and macOS?**
There are no platform-specific imports, and CI exercises the suite on Linux, macOS and Windows (Python 3.11 and 3.14) on pushes to `main` and on pull requests — so portability is checked rather than assumed.

**How do I run the tests?**
See [Tests](#tests) — they need no API key, no network and no Hermes install, but do need `httpx`.

## Settings

The plugin declares its own settings, so they appear in Hermes' plugin form (Desktop → Settings → Plugins → DeepSeek Search & Fetch, and the TUI's `plugins.manage settings` action) rather than being hand-written into `config.yaml`. In a terminal, set one with `hermes config set plugins.entries.<id>.settings.<key> <value>`; `hermes plugins list` shows the installation-specific `<id>`. (`hermes plugins manage settings` is not a CLI subcommand.)

| Setting | Type | Default | Effect |
|---|---|---|---|
| `need_content` | boolean | off | Sends `need_content: true`: every search hit carries its page text as clean Markdown. Costs kilobytes per result — typically a few KB, with a measured 20,176-character outlier — so a `limit` of 5 can reach tens of KB of context. |
| `localization` | string | empty | An IP address the Search API localises results to. Empty sends nothing; a value that is not an IP is ignored with a warning rather than sent and billed as a 400. A query already in the search cache keeps its old localization until the TTL expires too. |
| `extract_deadline` | integer | 110 | Upper bound on the seconds one multi-URL `extract()` may spend; clamped to 5–115 and additionally to `web.extract_timeout − 1.5 s` when that public setting is readable. |

A setting is read on every search, and Hermes' config cache is keyed on the config file's signature, so a change lands on the **next search** — no session restart. One caveat: Hermes caches successful search responses for `web.cache_ttl_minutes` (20 by default) against the query, so a query already answered before the change keeps its old shape/localization until that entry expires.

Settings live under `plugins.entries.<id>.settings.<key>`, and `<id>` depends on how the plugin was installed — a catalog or git install lands as `deepseek-search-fetch`, a manual clone under `plugins/web/` as `web/deepseek-search-fetch`. Use the form, which resolves the id; `hermes plugins list` shows it if you must set a value by path.

## Tune it

| What | Where | Notes |
|---|---|---|
| Endpoint | `DEEPSEEK_BASE_URL` | Tests only; validated as described above. |
| Page content on search results | `need_content` setting, or `DEEPSEEK_SEARCH_NEED_CONTENT` | Off by default. The setting wins when it holds a boolean; an **unset** setting falls back to the environment variable, accepting `1` (or `true`/`yes`/`on`), and a failing settings read is off. Either opt-in sends `need_content: true` — every hit then carries its page text as clean Markdown, typically a few KB per result (measured outlier: 20,176 characters) in the tool result and the context window. |
| Result cap | `_SEARCH_LIMIT_CAP` in `__init__.py` | `50` — DeepSeek's own maximum. Hermes' tool schema allows 100, so a larger request returns 50. |
| Timeout | `_TIMEOUT` in `__init__.py` | `30.0` s per attempt, and streamed against a wall clock rather than only httpx's per-read timer. |
| Transient retries | `_RETRY_BACKOFF_S`, `_RETRY_JITTER_S`, `_RETRY_BUDGET_S` in `__init__.py` | One further attempt per backoff entry (currently 1s then 2s), plus up to 0.25s of jitter. The 40s budget bounds scheduling: a retry is only scheduled when that attempt still fits, so a slow 5xx ends the call instead of spending 30s past the budget. A `Retry-After` in seconds replaces that attempt's wait when affordable, and ends the retries when it is not. |
| Extract batch deadline | `extract_deadline` setting, `_EXTRACT_DEADLINE_S` in `__init__.py` | `110` s upper bound; the effective deadline is `min(setting, web.extract_timeout − 1.5 s)` when the public setting is readable. Later URLs are not requested; pages already fetched stay in the list, and an in-flight body is aborted at the deadline. |
| Provider name | `provides_web_providers` in `plugin.yaml` | `deepseek`; changing it changes what your config must say. The manifest field is catalog metadata — `register()` is what actually makes the provider available. |

## Tests

Offline, keyless and unbilled: a loopback mock server stands in for the vendor, and a small stub lets the plugin import **without a Hermes install**, so the suite runs anywhere.

```bash
# stdlib-unittest tier (no Hypothesis; needs httpx, which Hermes ships) —
# 114 tests (113 + a skip placeholder)
python -m unittest discover -s tests

# Full tier, adding the Hypothesis property tests — 124 tests
uv venv .venv-dev
uv pip install --python .venv-dev/bin/python -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests
```

The tier is "no Hypothesis", not "no dependencies": the plugin imports `httpx`, so install it (Hermes
already has it). On Windows the dev interpreter is `.venv-dev\Scripts\python.exe`.

Neither tier loads Hermes, so neither can prove the plugin matches the provider interface or how the
dispatcher treats it. That is what `dsweb-integration.py` is for: it installs the plugin into a
throwaway `HERMES_HOME` the way a stranger does, points `DEEPSEEK_BASE_URL` at a loopback mock (which
the base-URL guard allows) and drives the real `web_search` / `web_extract` path — 22 checks covering
the registered ABC/setup schema, the settings surface, retry behaviour, exact extract unescaping,
error-first batches, per-URL errors and the search-memo caveat. Offline, keyless and unbilled; run it
by hand before a release:

```bash
python dsweb-integration.py --hermes-home /tmp/dsw-it --hermes /path/to/hermes \
  --install <40-char-sha>
```

`--hermes` is the CLI binary; `--hermes-source` is the Hermes checkout to import the tools from, and
it is auto-detected from `$HERMES_SOURCE`, `~/.hermes/hermes-agent`, then `/opt/hermes`. Pass it if
your checkout lives elsewhere. Without `--install` the harness tests whatever is already installed in
that `HERMES_HOME` and prints a warning.

Neither of those touches the vendor, so neither can show that DeepSeek accepts what we send.
`dsweb-live-accept.py` is the one run that can, and it **spends money** — about $0.003–0.004 for its
four billable calls (the 129-character 400 is free). Run it once before a release, never in CI:

```bash
python dsweb-live-accept.py --spend
```

It proves a live search returns hits with the vendor's signals, that `need_content` really returns
page text and that search content is clean Markdown at the wire, that `localization` is accepted
rather than rejected as an unknown field, that the fetch endpoint's **raw** body is backslash-escaped
and the plugin turns it back into headings/links, and that a 129-character query is refused with HTTP
400. The raw-response capture is what makes the unescape check falsifiable; asserting after the
transform would pass on a no-op.

The property tier generates arbitrary Unicode — including unpaired surrogates, which Hypothesis' default `st.text()` alphabet deliberately excludes — and asserts the invariant that made it necessary: whatever a caller passes, a UTF-8-encodable value reaches the wire. Point `DEEPSEEK_PLUGIN_PATH` at another copy of the plugin to test that copy; the falsification procedure and the invariants that must not break live in [AGENTS.md](AGENTS.md).

## Keywords

hermes-agent, hermes-plugin, deepseek, deepseek-api, web-search, web-extract, search-api, fetch-api, provider-plugin, agent-tools, llm-tools, rag, python

## License

MIT — see [LICENSE](LICENSE).
