"""DeepSeek web-search + fetch provider for Hermes (native plugin).

Routes Hermes' web_search / web_extract tools through DeepSeek's Search & Fetch
tools APIs: POST https://api.deepseek.com/v1/tools/search and /v1/tools/fetch.
Billing: $0.75 / 1,000 successful requests (failures and empty results free);
5 QPS. Key: DEEPSEEK_API_KEY (declared in plugin.yaml requires_env).

Self-contained on purpose: the bundled helpers (plugins/web/_common.py) are
internal to the shipped tree; a user plugin imports only agent.web_search_provider
and httpx (both guaranteed present — every sibling provider uses httpx).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent.web_search_provider import WebSearchProvider, get_provider_env

logger = logging.getLogger(__name__)

# DEEPSEEK_BASE_URL exists so the test suite can point the provider at a local
# mock server. It is validated rather than trusted: an override may only be the
# real endpoint over https, or a loopback test host. Anything else is refused
# with a warning and the default endpoint is used — a freely redirectable base
# URL would send the bearer key wherever whoever controls the environment chose.
_DEFAULT_BASE = "https://api.deepseek.com"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _safe_base() -> str:
    raw = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return _DEFAULT_BASE
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    no_userinfo = not (parsed.username or parsed.password)
    if no_userinfo and host == "api.deepseek.com" and parsed.scheme == "https":
        return raw
    if no_userinfo and host in _LOOPBACK_HOSTS and parsed.scheme in {"http", "https"}:
        return raw
    logger.warning(
        "ignoring DEEPSEEK_BASE_URL=%r: only https://api.deepseek.com or a loopback test "
        "host is accepted; using %s", raw[:120], _DEFAULT_BASE,
    )
    return _DEFAULT_BASE


_BASE = _safe_base()
_SEARCH_URL = f"{_BASE}/v1/tools/search"
_FETCH_URL = f"{_BASE}/v1/tools/fetch"
_TIMEOUT = 30.0
_SEARCH_LIMIT_CAP = 50  # DeepSeek count: 1-50

# Unpaired surrogates (U+D800-U+DFFF) are not encodable in UTF-8: httpx's JSON
# serialization raises before the request is sent. Replace them the way any
# UTF-8 transport would, so one bad code point cannot kill a call.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _utf8_safe(text: str) -> str:
    """Return *text* with unpaired surrogates replaced by U+FFFD."""
    return _SURROGATE_RE.sub("\ufffd", text)


# The fetch endpoint escapes every ASCII punctuation character in the page text:
# headings arrive as a literal backslash-hash, links as escaped brackets and
# colons, table pipes as escaped pipes. Left alone, "Markdown" renders as
# punctuation instead of structure. The escaping is uniform, so its inverse is
# too — only backslash + ASCII punctuation ([!-/:-@[-`{-~]) is unescaped, which
# leaves letter escapes such as a literal backslash-n or a regex like
# backslash-d untouched, because letters are not punctuation.
_UNESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _unescape_markdown(text: str) -> str:
    """Undo the fetch endpoint's backslash escaping of page Markdown."""
    return _UNESCAPE_RE.sub(r"\1", text)


# Search returns metadata only unless the API is asked for page content. Setting
# `need_content` makes every result carry its page text as clean Markdown — and
# costs 2-5 KB per result, straight into the tool result and the context window.
# So it is opt-in: DEEPSEEK_SEARCH_NEED_CONTENT=1 (or true/yes/on).
_NEED_CONTENT_ENV = "DEEPSEEK_SEARCH_NEED_CONTENT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _need_content_enabled() -> bool:
    """True when the operator opted into page content on search results.

    Read through get_provider_env so a value set in Hermes' config layer works
    like one exported in the environment. Any failure to read the flag counts as
    "off" — a capability must never fail over a preference.
    """
    try:
        raw = get_provider_env(_NEED_CONTENT_ENV) or ""
    except Exception:  # never raise out of a capability
        return False
    return raw.strip().lower() in _TRUTHY


def _missing_key_error() -> str:
    return (
        "DEEPSEEK_API_KEY is not set. Add it for the deepseek-search-fetch plugin "
        "(hermes tools -> Web Search) or unset the DeepSeek web backend."
    )


class DeepSeekWebSearchProvider(WebSearchProvider):
    """DeepSeek Search & Fetch APIs as a Hermes web provider."""

    @property
    def name(self) -> str:
        return "deepseek"

    @property
    def display_name(self) -> str:
        return "DeepSeek"

    def is_available(self) -> bool:
        return bool(get_provider_env("DEEPSEEK_API_KEY"))

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {get_provider_env('DEEPSEEK_API_KEY')}",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            detail = (resp.text or "").strip()[:300] or resp.reason_phrase or ""
            raise RuntimeError(f"HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except ValueError as exc:  # non-JSON body (HTML from a proxy/WAF/maintenance page)
            snippet = (resp.text or "").strip()[:200]
            raise RuntimeError(f"non-JSON response ({exc}); body: {snippet!r}") from exc

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not self.is_available():
            return {"success": False, "error": _missing_key_error()}
        try:
            query = _utf8_safe(query) if isinstance(query, str) else query
            count = min(max(int(limit), 1), _SEARCH_LIMIT_CAP)
            want_content = _need_content_enabled()
            payload: dict[str, Any] = {"query": query, "count": count}
            if want_content:
                payload["need_content"] = True
            data = self._post(_SEARCH_URL, payload)
            rows = data.get("results") or []
            hits: list[dict[str, Any]] = []
            for i, r in enumerate(rows):
                hit: dict[str, Any] = {
                    "title": r.get("title") or "",
                    "url": r.get("url") or "",
                    "description": r.get("summary") or r.get("snippet") or "",
                    "position": i + 1,
                    # Quality, freshness and provenance signals, so a caller can
                    # filter or attribute a result set without paying to fetch
                    # it. None when the vendor omits them — the keys are always
                    # present.
                    "rerank_score": r.get("rerank_score"),
                    "authority": r.get("authority"),
                    "date": r.get("date"),
                    "site_name": r.get("site_name"),
                    "site_icon": r.get("site_icon"),
                }
                if want_content:
                    # The search endpoint returns page text as clean Markdown —
                    # no backslash escaping, unlike the fetch endpoint.
                    hit["content"] = r.get("content") or ""
                hits.append(hit)
            return {"success": True, "data": {"web": hits}}
        except Exception as exc:  # never raise out of a capability
            logger.warning("DeepSeek search failed: %s", exc)
            return {"success": False, "error": f"DeepSeek search failed: {exc}"}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        # DeepSeek's fetch endpoint is single-URL; fetch sequentially.
        # Unknown kwargs (format, include_raw, ...) are ignored per the contract.
        urls = [_utf8_safe(u) if isinstance(u, str) else u for u in urls]
        if not self.is_available():
            err = _missing_key_error()
            return [{"url": u, "title": "", "content": "", "error": err} for u in urls]
        out: list[dict[str, Any]] = []
        for url in urls:
            try:
                data = self._post(_FETCH_URL, {"url": url})
                title = data.get("title") or ""
                # Both fields hold the unescaped Markdown on purpose: Hermes
                # prefers raw_content when it derives the model-visible content
                # and when it writes its extract cache, so leaving the vendor's
                # escaping in raw_content would reintroduce it in every cached
                # and budgeted result.
                content = _unescape_markdown(data.get("content") or "")
                out.append(
                    {
                        "url": url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {"sourceURL": url, "title": title},
                    }
                )
            except Exception as exc:
                logger.warning("DeepSeek fetch failed for %s: %s", url, exc)
                out.append(
                    {"url": url, "title": "", "content": "", "error": f"DeepSeek fetch failed: {exc}"}
                )
        return out

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid · per-request",
            "tag": "Search + fetch via DeepSeek's Search & Fetch tools ($0.75/1k successful requests).",
            "env_vars": [
                {
                    "key": "DEEPSEEK_API_KEY",
                    "prompt": "DeepSeek API key",
                    "url": "https://platform.deepseek.com/api_keys",
                }
            ],
        }


def register(ctx) -> None:
    ctx.register_web_search_provider(DeepSeekWebSearchProvider())
