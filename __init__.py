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

import ipaddress
import json
import logging
import os
import random
import re
import time
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

# Search & Fetch are read-only, and the vendor's rule for them is specific:
# wait and retry HTTP 408, 429 and 5xx; top up on 402; send every other 4xx
# once. The wait is per call. Search and Fetch have separate 5 QPS account
# caps, Fetch adds a per-domain cap, and other processes on the account are
# invisible here, so a queue in this process would not be the account's limit.
# Two quick retries clear a one-second QPS bucket.
#
# The 40s budget bounds the CALL, not just the wait: a retry is only scheduled
# when the attempt it schedules still fits inside the budget, so a slow call
# cannot spend a third 30s attempt past it.
#
# Extract walks URLs one by one, and Hermes abandons the whole batch at 120s —
# pages already fetched included. Two things keep the batch inside that cap.
# httpx timeouts are per connect/read, NOT wall-clock: a body that trickles in
# fast enough to reset the read timer outlives _TIMEOUT, so a deadline that
# gated only the start of an attempt would bound nothing. Each attempt gets
# only the time the deadline has left, capped at _TIMEOUT, and the batch stops
# starting requests at the deadline — worst case deadline + _REQUEST_FLOOR_S.
# The limit is the length of the backoff list, so adding a wait cannot
# index off the end of the tuple.
_RETRY_BACKOFF_S = (1.0, 2.0)
_RETRY_LIMIT = len(_RETRY_BACKOFF_S)
_RETRY_JITTER_S = 0.25
_RETRY_BUDGET_S = 40.0
_REQUEST_FLOOR_S = 0.5
# 110 plus the 0.5s floor sits inside Hermes' default 120s cap with room for the
# thread hop, and leaves the rest of the window to pages that would otherwise
# never be started. Lower it when web.extract_timeout is lowered — the plugin
# cannot read that setting, so the two numbers have to be kept in step by hand.
_EXTRACT_DEADLINE_S = 110.0
_EXTRACT_DEADLINE_SETTING = "extract_deadline"
_EXTRACT_DEADLINE_MIN_S = 5.0
_EXTRACT_DEADLINE_MAX_S = 115.0
_BATCH_DEADLINE = "batch deadline reached"
_DETAIL_CAP = 300
_TOP_UP = "Top up the DeepSeek account before retrying."

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
# So it is opt-in, from the plugin's own setting (Desktop plugin form, or
# `hermes plugins manage settings`), or — for hosts with no settings surface —
# from DEEPSEEK_SEARCH_NEED_CONTENT=1 (or true/yes/on).
_NEED_CONTENT_ENV = "DEEPSEEK_SEARCH_NEED_CONTENT"
_NEED_CONTENT_SETTING = "need_content"
_LOCALIZATION_SETTING = "localization"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_need_content() -> bool:
    """The environment fallback: True when the operator opted in there."""
    try:
        raw = get_provider_env(_NEED_CONTENT_ENV) or ""
    except Exception:  # never raise out of a capability
        return False
    return raw.strip().lower() in _TRUTHY


def _need_content_enabled(get_setting=None) -> bool:
    """True when page content on search results was asked for.

    The plugin setting wins when it holds a boolean: a box ticked in the UI is
    a deliberate choice, and an ambient environment variable must not override
    it. An unset setting falls through to the environment. Any failure to read
    either counts as "off" — a capability must never fail over a preference.
    """
    if get_setting is not None:
        try:
            value = get_setting(_NEED_CONTENT_SETTING)
        except Exception:  # never raise out of a capability
            return _env_need_content()
        if isinstance(value, bool):
            return value
    return _env_need_content()


def _localization(get_setting=None) -> str:
    """The Search API's `localization` IP, or "" when it is not usable.

    The API takes an IP address for this field, so anything else is refused
    here with a warning instead of being sent and billed as a 400. A setting
    that is not a string is ignored the same way; an unavailable settings
    surface is simply "not set".
    """
    if get_setting is None:
        return ""
    try:
        value = get_setting(_LOCALIZATION_SETTING)
    except Exception:  # never raise out of a capability
        return ""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        logger.warning("Ignoring %s setting %r: not an IP address", _LOCALIZATION_SETTING, value)
        return ""
    return value


def _extract_deadline(get_setting=None) -> float:
    """Seconds one ``extract()`` may spend starting requests.

    A setting exists because the number has to be kept below Hermes'
    ``web.extract_timeout`` (120 by default) and the plugin cannot read that
    setting — it is not plugin-scoped. Anything unusable falls back to the
    built-in value, clamped, so a bad setting can never uncap the batch.
    """
    if get_setting is not None:
        try:
            value = get_setting(_EXTRACT_DEADLINE_SETTING)
        except Exception:  # never raise out of a capability
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return min(max(float(value), _EXTRACT_DEADLINE_MIN_S), _EXTRACT_DEADLINE_MAX_S)
    return _EXTRACT_DEADLINE_S


def _missing_key_error() -> str:
    return (
        "DEEPSEEK_API_KEY is not set. Add it for the deepseek-search-fetch plugin "
        "(hermes tools -> Web Search) or unset the DeepSeek web backend."
    )


def _is_retryable(status: int) -> bool:
    """True for the statuses the Search & Fetch guide says to wait out."""
    return status in {408, 429} or 500 <= status <= 599


def _retry_suffix(retries: int) -> str:
    """Empty until a retry has actually happened. One retry stays singular."""
    if retries <= 0:
        return ""
    noun = "retry" if retries == 1 else "retries"
    return f" (after {retries} {noun})"


def _retry_after_seconds(value: str | None) -> float | None:
    """The header's whole value, when it is a positive number of seconds.

    HTTP-dates, zero, negatives, infinities and non-numeric text are ignored.
    The caller honours any affordable value and stops retrying when the wait it
    asks for does not fit inside the call's budget.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds <= 0 or seconds == float("inf") or seconds != seconds:
        return None
    return seconds


def _backoff_delay(retries: int) -> float:
    """The scheduled wait before retry ``retries + 1``, jitter included."""
    return _RETRY_BACKOFF_S[retries] + _uniform(0.0, _RETRY_JITTER_S)


def _error_message(raw: str) -> str | None:
    """`error.message` when the body is the documented OpenAI error object."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    message = err.get("message")
    if not isinstance(message, str):
        return None
    message = message.strip()
    return message or None


def _vendor_text(resp: httpx.Response) -> str:
    """The vendor's own words, capped, with the raw body behind the message."""
    raw = (resp.text or "").strip()
    message = _error_message(raw)
    if message is not None:
        return message[:_DETAIL_CAP]
    return raw[:_DETAIL_CAP] or (resp.reason_phrase or "")


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _uniform(low: float, high: float) -> float:
    return random.uniform(low, high)


def _request(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = _TIMEOUT,
) -> httpx.Response:
    return httpx.post(url, json=payload, headers=headers, timeout=timeout)


def _status_error(resp: httpx.Response, retries: int) -> RuntimeError:
    """Error envelope for a non-200. 402 carries the top-up remedy."""
    detail = _vendor_text(resp)
    if resp.status_code == 402:
        if not detail:
            detail = "Insufficient balance"
        text = f"HTTP 402: {detail}. {_TOP_UP}"
    elif detail:
        text = f"HTTP {resp.status_code}: {detail}"
    else:
        text = f"HTTP {resp.status_code}"
    return RuntimeError(text + _retry_suffix(retries))


class DeepSeekWebSearchProvider(WebSearchProvider):
    """DeepSeek Search & Fetch APIs as a Hermes web provider."""

    def __init__(self, get_setting=None) -> None:
        # Hermes passes a settings reader in register(); the tests construct the
        # provider directly, and a host without a settings surface leaves it None.
        self._get_setting = get_setting

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

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        deadline: float | None = None,
    ) -> dict[str, Any]:
        started = _monotonic()
        retries = 0
        # The last response, so a deadline that arrives before the next send
        # can still report that status. None until the first response.
        held: httpx.Response | None = None
        while True:
            if deadline is not None and _monotonic() >= deadline:
                if held is None:
                    raise RuntimeError(_BATCH_DEADLINE)
                raise _status_error(held, retries)
            attempt_timeout = _TIMEOUT
            if deadline is not None:
                # httpx timeouts are per connect/read, not wall-clock: give the
                # attempt only what the deadline has left, or a body that trickles
                # in fast enough to reset the read timer outlives both.
                attempt_timeout = min(_TIMEOUT, max(_REQUEST_FLOOR_S, deadline - _monotonic()))
            try:
                resp = _request(
                    url,
                    payload,
                    {
                        "Authorization": f"Bearer {get_provider_env('DEEPSEEK_API_KEY')}",
                        "Content-Type": "application/json",
                    },
                    timeout=attempt_timeout,
                )
            except httpx.TransportError as exc:
                # A dropped connection is not one of the documented statuses.
                # Retrying it spends the 30s timeout again. Once a retry has
                # already happened, keep that count on the error Hermes sees.
                if retries == 0:
                    raise
                raise RuntimeError(f"{exc}{_retry_suffix(retries)}") from exc
            held = resp
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:  # non-JSON body (HTML from a proxy/WAF/maintenance page)
                    snippet = (resp.text or "").strip()[:200]
                    raise RuntimeError(
                        f"non-JSON response ({exc}); body: {snippet!r}{_retry_suffix(retries)}"
                    ) from exc
            now = _monotonic()
            elapsed = now - started
            # Same clock reading for the budget and the batch deadline, so a
            # slow response and the sleep that would follow it are judged once.
            if (
                not _is_retryable(resp.status_code)
                or retries >= _RETRY_LIMIT
                or elapsed >= _RETRY_BUDGET_S
            ):
                raise _status_error(resp, retries)
            # Retry-After is an instruction, not a hint: honour it when this call
            # can afford the wait, and stop retrying when it cannot. Waiting less
            # than the server asked would just earn another 429 — sooner.
            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            delay = retry_after if retry_after is not None else _backoff_delay(retries)
            # The budget bounds the call: only schedule an attempt that still
            # fits inside it, or a slow 5xx spends 30s past a 40s budget.
            past_budget = elapsed + delay + _TIMEOUT > _RETRY_BUDGET_S
            past_deadline = deadline is not None and now + delay >= deadline
            if past_budget or past_deadline:
                raise _status_error(resp, retries)
            logger.warning(
                "DeepSeek HTTP %s, retry %d/%d",
                resp.status_code, retries + 1, _RETRY_LIMIT,
            )
            _sleep(delay)
            retries += 1

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not self.is_available():
            return {"success": False, "error": _missing_key_error()}
        try:
            query = _utf8_safe(query) if isinstance(query, str) else query
            count = min(max(int(limit), 1), _SEARCH_LIMIT_CAP)
            want_content = _need_content_enabled(self._get_setting)
            payload: dict[str, Any] = {"query": query, "count": count}
            if want_content:
                payload["need_content"] = True
            localization = _localization(self._get_setting)
            if localization:
                payload["localization"] = localization
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
        deadline = _monotonic() + _extract_deadline(self._get_setting)
        out: list[dict[str, Any]] = []
        for url in urls:
            try:
                data = self._post(_FETCH_URL, {"url": url}, deadline)
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
    # The settings surface (plugins.entries.<id>.settings) is what makes
    # `need_content` live: the reader is called per search, and Hermes' config
    # cache is file-signature keyed, so a settings change lands on the next
    # search without restarting the session. On a host that predates it there is
    # no get_config, and the provider falls back to the environment variable.
    reader = getattr(ctx, "get_config", None)
    get_setting = (lambda key: reader(key, default=None)) if reader is not None else None
    ctx.register_web_search_provider(DeepSeekWebSearchProvider(get_setting))
