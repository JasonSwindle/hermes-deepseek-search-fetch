# AGENTS.md

Hermes Agent plugin: routes the agent's `web_search` / `web_extract` tools through
DeepSeek's Search & Fetch APIs (`POST api.deepseek.com/v1/tools/{search,fetch}`).
Single-file plugin. Nothing here is loaded outside Hermes; the tests load it directly.

## Commands

```bash
# no-Hypothesis tier (stdlib unittest + httpx; no key, no network, no billing) — must pass
python -m unittest discover -s tests

# full tier, adding the Hypothesis property tests (CI sets REQUIRE_HYPOTHESIS=1
# so a broken import cannot silently reduce this to the zero-dep tier)
uv venv .venv-dev
uv pip install --python .venv-dev/bin/python -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests
```

Run both before declaring any change done. The suite is offline by construction — a
loopback mock server stands in for the vendor and `DEEPSEEK_BASE_URL` is what points
at it. Never aim the tests at the real API; nothing in `tests/` should ever need a key.
The tier is "no Hypothesis", not "no dependencies": the plugin imports `httpx`, which
Hermes ships.

Then the tier that actually proves the plugin fits Hermes (neither unittest tier can):

```bash
python dsweb-integration.py --hermes-home /tmp/dsw-it --hermes /path/to/hermes \
  --hermes-source /path/to/hermes-checkout --install <sha>
```

`--hermes` is the CLI binary; `--hermes-source` is the checkout to import the dispatcher from. It is
auto-detected from `$HERMES_SOURCE`, `~/.hermes/hermes-agent`, then `/opt/hermes`, so pass it only when
the tree lives elsewhere. It installs the plugin into a throwaway HERMES_HOME through `hermes plugins
install --ref <sha>`, points it at a loopback mock, and drives the real dispatcher: settings at call
time, the registered ABC/setup schema, retry counts, exact extract unescaping, error-first batches,
per-URL errors, the search-memo caveat. Run it before every release pin.

`hermes plugins validate` warns "private core imports" on that script (`tools.web_tools` and the
registry import — catalog rule 9). That is expected and only a warning: the script *is* the
integration tier, so it has to drive the real dispatcher, while the plugin's own runtime imports stay
on documented public surfaces. Keep them out of `__init__.py`; the one core module the plugin does
read is `hermes_cli.config` (public allowlist) for `web.extract_timeout`.

And once, before a release pin — the only run that touches the vendor, and the only one that costs
money (~$0.004): `python dsweb-live-accept.py --spend`. It now captures the raw fetch/search bodies
and asserts the vendor-escaping premise before unescaping; it needs a real key; never wire it into CI.

The live acceptance does not normally provoke 429s. A one-off manual probe on 2026-10-19 did: a
30-request same-domain Fetch burst produced three 429s (`Your current QPS: 25`, no `Retry-After`),
and 40 concurrent `extract()` calls produced 13 real 429s — all 40 recovered on the first 1s+jitter
retry. That is recorded evidence for the docs, not a CI test; do not wire load generation into the
suite or run it routinely. The documented QPS was 5/5 on 2026-10-18 and 100/25 by 2026-10-24; the
2026-10-19 Fetch 429 matched 25. Do not describe the old 5 as account variance — it predates the raise.

Vendor assumptions that no mock can pin:
- The vendor **ignores unknown request-body fields**, so a silent field rename would return 200 with
  default behaviour rather than an error. Only the live acceptance can catch that; keep its
  assertions on actual values (signals, content, escaping), not just `success`.
- Fetch content's backslash escaping is **undocumented** and unversioned. `dsweb-live-accept.py`
  captures the raw body and asserts the escape before the plugin reverses it; that raw-wire check is
  the tripwire. Do not replace the unescaper with a heuristic — a wrong heuristic corrupts valid
  Markdown, while the live check tells us when the vendor changes.
- Search content being clean Markdown and `Content-Encoding: gzip` are likewise live observations,
  not documented promises.

Falsification check (do this when you touch the sanitizer, unescaper, guard, key path, deadline, or
response mapping): make a copy of the plugin with the relevant guard neutered — `_utf8_safe` to
`return text`, `_unescape_markdown` to `return text`, `_api_key` to `return os.environ.get(...)`,
`_safe_base` to `return raw`, the `_extract_deadline` clamp to `return value`, or `_request` back to
`httpx.post` — run the suite with `DEEPSEEK_PLUGIN_PATH=<copy>`, and confirm it goes **red**. A suite
that stays green against a broken copy proves nothing.

## Invariants — these are pinned by tests

- `search()` returns `{"success": bool, "data": {"web": [{title, url, description, position,
  rerank_score, authority, date, site_name, site_icon}]}}`, and posts `need_content: true` only when
  the `need_content` setting is on — falling back to `DEEPSEEK_SEARCH_NEED_CONTENT` when the setting
  is unset (a host with no settings surface). Only a real boolean in the setting counts: a string
  `"true"` in a hand-edited config is not consent. A failing settings read is `.off`, never an
  exception out of `search()`. The reader is called per search, so a change lands on the next call
  without a restart. With the opt-in on, each hit also carries `content` — the page text, which the
  search endpoint returns as clean Markdown.
- `extract()` returns a **bare list** of entry dicts (`url`, `title`, `content`,
  `raw_content`, `metadata`, or `error` per entry) — *not* the
  `{"success": …, "data": […]}` envelope the Hermes docs show elsewhere. The bundled
  providers return a bare list and the dispatcher consumes it that way.
- Both `content` and `raw_content` hold the **unescaped** Markdown. The fetch endpoint
  backslash-escapes every punctuation character, and Hermes prefers `raw_content` when it
  derives the model-visible content and when it writes its extract cache — so unescaping
  only one field reintroduces the escaping in every cached and budgeted result. The live
  check captures the raw wire body and asserts the escape happened first.
- Every mapped response string is passed through `_utf8_safe`; a non-object body / non-list
  `results` is a clear error; a non-dict row is skipped, not fatal. A hostile vendor response
  must not make Hermes' own JSON/UTF-8 serialization raise.
- `extract()` accepts a bare string as one URL and a non-iterable as one entry, and never
  raises before its per-URL loop (a `None`/string/scalar input yields entries, not a traceback).
- `is_available()` **must not** touch the network; it runs on every `hermes tools` paint. It
  reads the key through `_api_key()`: stripped, and a raising `get_provider_env` (Hermes'
  `UnscopedSecretError` under profile multiplexing) becomes "unavailable", never a traceback.
- `need_content` and `localization` are read from the plugin's own settings on every search
  (`ctx.get_config`, wired in `register()`); a host without that surface leaves the reader `None`
  and the code falls back to `DEEPSEEK_SEARCH_NEED_CONTENT` for the first and to "not set" for the
  second. A failing read is "off" (not the environment), never an exception out of `search()`.
  `localization` is sent only when `ipaddress.ip_address` accepts it — the API takes an IP, so
  anything else would be a billed 400. The manifest declares `requires_hermes: ">=0.22.0"` (the
  settings surface's floor, matching the catalog entry); direct imports and the validation probe
  still tolerate a missing reader.
- `hermes plugins validate .` on a working tree that carries `.venv-dev/` fails its security scan on
  the virtualenv (`.so` files, symlinks, trio's sockets). That is the dev tree, not the plugin:
  validate a clean export (`git archive HEAD | tar -x -C <dir>`) or the catalog install path. The
  capability probe needs the Hermes source on `PYTHONPATH` (or `--install-deps`).
- Every outgoing query and URL is UTF-8-safe: unpaired surrogates become U+FFFD.
  httpx raises while *encoding* the body otherwise, so the call dies before it is sent.
  Response text is repaired the same way before it is returned.
- `DEEPSEEK_BASE_URL` is accepted only as the **exact origin** `https://api.deepseek.com` (https, no
  userinfo, port 443/absent, no path/query/fragment) or a loopback test host. Everything else —
  including a value `urlsplit` cannot parse — is refused with a warning that names only the
  scheme/host (never the raw value) and the default is used. The guard must never raise at import;
  the bearer key must not be redirectable by an environment variable.
- No `limit` outside 1–50 may reach the API, and the key must never be logged.
- Retries are only for HTTP 408, 429 and 5xx, at most once per entry of
  `_RETRY_BACKOFF_S`. A 200 whose body is not JSON still carries that retry count.
  `_RETRY_BUDGET_S` bounds the **call**, not just the wait: a retry is scheduled
  only when `elapsed + delay + _TIMEOUT` still fits, because an attempt can spend
  the whole 30s timeout. A `Retry-After` in seconds replaces that attempt's wait
  when the budget can afford it, and ends the retries when it cannot — retrying
  sooner than the server asked just earns another 429. Retry log lines carry the
  status and the attempt number, and they follow the key rule above.
- Timeouts are wall-clock, not just httpx's per-read timer. `_request` streams the body and aborts at
  the attempt deadline; `_post` passes `min(now + attempt_timeout, batch_deadline)`. An `extract()`
  attempt is additionally given only the batch time left, and `_extract_deadline` clamps the setting
  (default 110, max 115) to `web.extract_timeout − 1.5 s` when the public `hermes_cli.config` read
  works. Worst case is the effective deadline plus `_REQUEST_FLOOR_S`;
  `_EXTRACT_DEADLINE_MAX_S + _REQUEST_FLOOR_S <= 120` is pinned by a test. A trickle-body regression
  test would fail against the old blocking `httpx.post`.

## Do not

- **No self-updater, no "check for updates", no remote code fetch.** The Hermes plugin
  catalog pins a full commit SHA precisely so installed code cannot move; a listed
  plugin may not replace its own files. Updates ship as a SHA-bump PR to
  `NousResearch/hermes-agent`.
- **No new runtime dependency.** Runtime imports are the standard library, the `httpx`
  Hermes already ships, and the guarded public `hermes_cli.config` read needed to respect
  `web.extract_timeout`. Test-only extras belong in `requirements-dev.txt` and
  must stay optional (the property tier skips itself when Hypothesis is absent; CI sets
  `REQUIRE_HYPOTHESIS=1` so a missing import fails instead of skipping).
- **No `__pycache__`, `.pyc`, virtualenvs or binaries in the commit** — the catalog
  rejects unscannable payloads.
- Don't weaken the base-URL guard, widen what gets logged, or change the extract
  return shape without updating the pinned test and this file.

## Layout and naming

- `__init__.py` — the entire plugin. `plugin.yaml` — manifest (`kind: backend`,
  `provides_web_providers: [deepseek]`, `requires_hermes: ">=0.22.0"`; the catalog entry
  carries the same floor).
- `docs/` — brand assets only (`logo.png` 512×512, `banner.png` 1200×600 for the catalog card's
  2:1 `image` field, `social-preview.png` 1280×640 for GitHub's link-unfurl card). `logo.png` and
  `banner.png` carry **transparency outside the badge** on purpose: one file then reads correctly on
  a light card and a dark one, since there is no per-theme image field in the catalog. Images are not
  code: `hermes plugins validate` passes with them present, and the catalog requires card images on
  `raw.githubusercontent.com` pinned to the entry's sha.
- **The social preview is uploaded by hand, once.** GitHub exposes no REST or GraphQL endpoint for
  it (Settings → General → Social preview is the only route), so the repo cannot set it in CI:
  `docs/social-preview.png` is the source of truth, and it was last uploaded manually.
- **Two names, on purpose:** the plugin/package name is `deepseek-search-fetch`, the
  *registered provider* name is `deepseek` — that second one is what users put in
  `web.search_backend`, and it stays bare because that is how Hermes names web
  providers (tavily, exa, firecrawl).
- Consumers of the plugin's API: Hermes' web dispatcher (`tools/web_tools.py`,
  `web_tools_extract.py`, `web_tools_rescue.py` upstream) plus this repo's tests.
- Human-facing docs live in README.md; the catalog rules this repo must satisfy are in
  `NousResearch/hermes-agent` → `plugin-catalog/README.md`. Read them before proposing
  a catalog change; they are enforced by review, not by CI here.

## Releasing

Bump `version` in `plugin.yaml`, commit, tag if you like, then open a PR to
`NousResearch/hermes-agent` adding/updating `plugin-catalog/deepseek-search-fetch.yaml`
with the new full 40-character commit SHA (tags, branches and short SHAs are rejected).

**Don't tag per edit.** Docs changes land on `main` and ride the next release; a release is
cut when something must be pinned — a plugin code change, or a catalog re-pin. A tag per
README tweak is churn the catalog does not need, and each one is another number the entry
has to explain. **Catalog card images are pinned to the entry's SHA**, so a changed
`docs/banner.png`/`logo.png` does need a new SHA and re-pin (that is why 1.0.4/1.0.5 existed);
an unchanged image URL can keep the current SHA when only text changes. The 1.0.8 re-pin must
also update the catalog entry's `version`, `sha`, and the `image:` URL's SHA, and keep its
`requires_hermes: ">=0.22.0"` in step with `plugin.yaml`.

**Release titles are the bare tag** (`v1.0.3`), never `deepseek-search-fetch v1.0.3`.
GitHub's releases sidebar and the repo home page truncate release names, so a
prefixed title renders as `deepseek-search-fetch v1...` and hides the one thing the
row exists to show. `/releases/latest` and the tag both stay `v1.0.3` either way, so
nothing is lost by keeping the name short.
