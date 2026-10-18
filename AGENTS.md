# AGENTS.md

Hermes Agent plugin: routes the agent's `web_search` / `web_extract` tools through
DeepSeek's Search & Fetch APIs (`POST api.deepseek.com/v1/tools/{search,fetch}`).
Single-file plugin. Nothing here is loaded outside Hermes; the tests load it directly.

## Commands

```bash
# zero-dependency tier (stdlib only, no key, no network, no billing) — must pass
python -m unittest discover -s tests

# full tier, adding the Hypothesis property tests
uv venv .venv-dev
uv pip install --python .venv-dev/bin/python -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests
```

Run both before declaring any change done. The suite is offline by construction — a
loopback mock server stands in for the vendor and `DEEPSEEK_BASE_URL` is what points
at it. Never aim the tests at the real API; nothing in `tests/` should ever need a key.

Then the tier that actually proves the plugin fits Hermes (neither unittest tier can):

```bash
python dsweb-integration.py --hermes-home /tmp/dsw-it --hermes /path/to/hermes --install <sha>
```

It installs the plugin into a throwaway HERMES_HOME through `hermes plugins install --ref <sha>`,
points it at a loopback mock, and drives the real dispatcher: settings at call time, retry counts,
extract unescaping, per-URL errors, the search-memo caveat. Run it before every release pin.

Falsification check (do this when you touch the sanitizer or the guard): make a copy
of the plugin with `_utf8_safe` neutered to `return text`, run the suite with
`DEEPSEEK_PLUGIN_PATH=<copy>`, and confirm it goes **red**. A suite that stays green
against a broken copy proves nothing. Same for the unescaper: neuter
`_unescape_markdown` to `return text` and confirm the extract tests go red.

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
  only one field reintroduces the escaping in every cached and budgeted result.
- `is_available()` **must not** touch the network; it runs on every `hermes tools` paint.
- `need_content` and `localization` are read from the plugin's own settings on every search
  (`ctx.get_config`, wired in `register()`); a host without that surface leaves the reader `None`
  and the code falls back to `DEEPSEEK_SEARCH_NEED_CONTENT` for the first and to "not set" for the
  second. A failing read is "off", never an exception out of `search()`. `localization` is sent only
  when `ipaddress.ip_address` accepts it — the API takes an IP, so anything else would be a billed 400.
- `hermes plugins validate .` on a working tree that carries `.venv-dev/` fails its security scan on
  the virtualenv (`.so` files, symlinks, trio's sockets). That is the dev tree, not the plugin:
  validate a clean export (`git archive HEAD | tar -x -C <dir>`) or the catalog install path.
- Every outgoing query and URL is UTF-8-safe: unpaired surrogates become U+FFFD.
  httpx raises while *encoding* the body otherwise, so the call dies before it is sent.
- `DEEPSEEK_BASE_URL` is accepted only as `https://api.deepseek.com` (https) or a
  loopback test host. Everything else is refused with a warning and the default is
  used — the bearer key must not be redirectable by an environment variable.
- No `limit` outside 1–50 may reach the API, and the key must never be logged.
- Retries are only for HTTP 408, 429 and 5xx, at most once per entry of
  `_RETRY_BACKOFF_S`. A 200 whose body is not JSON still carries that retry count.
  A positive `Retry-After` in seconds replaces an attempt's wait when it is no
  longer than that attempt's backoff. Retry log lines carry the status and the
  attempt number, and they follow the key rule above.
- A multi-URL `extract()` stops starting requests `_EXTRACT_DEADLINE_S` after it
  began, so pages already fetched survive Hermes' extract timeout: they stay in
  the returned list and only the skipped URLs carry an error.

## Do not

- **No self-updater, no "check for updates", no remote code fetch.** The Hermes plugin
  catalog pins a full commit SHA precisely so installed code cannot move; a listed
  plugin may not replace its own files. Updates ship as a SHA-bump PR to
  `NousResearch/hermes-agent`.
- **No new runtime dependency.** Runtime imports are the standard library plus the
  `httpx` Hermes already ships. Test-only extras belong in `requirements-dev.txt` and
  must stay optional (the property tier skips itself when Hypothesis is absent).
- **No `__pycache__`, `.pyc`, virtualenvs or binaries in the commit** — the catalog
  rejects unscannable payloads.
- Don't weaken the base-URL guard, widen what gets logged, or change the extract
  return shape without updating the pinned test and this file.

## Layout and naming

- `__init__.py` — the entire plugin. `plugin.yaml` — manifest (`kind: backend`,
  `provides_web_providers: [deepseek]`).
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

**Don't tag per edit.** Docs and asset changes land on `main` and ride the next release; a
release is cut when something must be pinned — a plugin code change, or a catalog re-pin.
A tag per README or image tweak is churn the catalog does not need, and each one is another
number the entry has to explain.

**Release titles are the bare tag** (`v1.0.3`), never `deepseek-search-fetch v1.0.3`.
GitHub's releases sidebar and the repo home page truncate release names, so a
prefixed title renders as `deepseek-search-fetch v1...` and hides the one thing the
row exists to show. `/releases/latest` and the tag both stay `v1.0.3` either way, so
nothing is lost by keeping the name short.
