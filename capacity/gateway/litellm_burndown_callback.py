"""LiteLLM CustomLogger enforcing stateful Redis weighted burndown shares.

`capacity gateway` emits this file with the constants below set for one deployment. It is
kept as a real module, not a string, so the linter and the type checker read it too.
"""

from __future__ import annotations

import time
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover

    class CustomLogger:  # type: ignore[no-redef]
        pass


MODEL_FAMILY = "gemini_1p"
LONG_CONTEXT_THRESHOLD: int | None = 200_000
STD_INPUT_WEIGHT = 1.0
STD_CACHED_WEIGHT = 0.1
STD_OUTPUT_WEIGHT = 4.0
CACHE_WRITE_5M_WEIGHT = 1.0
CACHE_WRITE_1H_WEIGHT = 1.0
LONG_INPUT_WEIGHT = 2.0
LONG_CACHED_WEIGHT = 0.2
LONG_OUTPUT_WEIGHT = 7.5
SPILLOVER_SHARED_HEADER = "priority"
COMMITTED_POOL_BURNDOWN_TPM = 2_418_000
DEFAULT_TEAM_FUNDED_BURNDOWN_TPM = 604_500
PREFLIGHT_FLOOR_TOKENS = 1_024
WINDOW_TTL_SECONDS = 120


def compute_burndown_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """Compute hardware-weighted burndown tokens for 1P Gemini or 3P Claude PT."""
    total_in = max(0, int(prompt_tokens))
    cached = min(max(0, int(cached_tokens)), total_in)
    rem_in = total_in - cached
    w5 = min(max(0, int(cache_write_5m_tokens)), rem_in)
    w1 = min(max(0, int(cache_write_1h_tokens)), rem_in - w5)
    uncached = rem_in - w5 - w1
    decode = max(0, int(completion_tokens)) + max(0, int(reasoning_tokens))
    if LONG_CONTEXT_THRESHOLD is not None and total_in > LONG_CONTEXT_THRESHOLD:
        return (
            (uncached * LONG_INPUT_WEIGHT)
            + (cached * LONG_CACHED_WEIGHT)
            + (w5 * CACHE_WRITE_5M_WEIGHT)
            + (w1 * CACHE_WRITE_1H_WEIGHT)
            + (decode * LONG_OUTPUT_WEIGHT)
        )
    return (
        (uncached * STD_INPUT_WEIGHT)
        + (cached * STD_CACHED_WEIGHT)
        + (w5 * CACHE_WRITE_5M_WEIGHT)
        + (w1 * CACHE_WRITE_1H_WEIGHT)
        + (decode * STD_OUTPUT_WEIGHT)
    )


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        val = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if val is not None:
            return val
    return None


def _extract_usage_counts(usage: Any) -> tuple[int, int, int, int, int, int]:
    """Extract prompt, completion, reasoning, cache read, and 5m/1h cache write tokens."""
    p_det = _field(usage, "prompt_tokens_details")
    c_det = _field(usage, "completion_tokens_details")
    cc = _field(usage, "cache_creation")
    cached = int(
        _field(usage, "cached_tokens", "cached_content_token_count", "cache_read_input_tokens")
        or (_field(p_det, "cached_tokens") if p_det is not None else 0)
        or 0
    )
    reason = int(
        _field(usage, "reasoning_tokens", "thoughts_tokens")
        or (_field(c_det, "reasoning_tokens") if c_det is not None else 0)
        or 0
    )
    w1 = int((_field(cc, "ephemeral_1h_input_tokens") if cc is not None else 0) or 0)
    w5_raw = _field(cc, "ephemeral_5m_input_tokens") if cc is not None else None
    cc_total = int(_field(usage, "cache_creation_input_tokens") or 0)
    w5 = int(w5_raw) if w5_raw is not None else max(0, cc_total - w1)
    raw_prompt = _field(usage, "prompt_tokens")
    raw_input = _field(usage, "input_tokens")
    if raw_prompt is not None:
        p_tok = max(int(raw_prompt), cached + w5 + w1)
    elif raw_input is not None:
        has_3p_cache = (
            _field(usage, "cache_read_input_tokens", "cache_creation_input_tokens") is not None
            or cc is not None
        )
        p_tok = int(raw_input) + cached + w5 + w1 if has_3p_cache else int(raw_input)
    else:
        p_tok = cached + w5 + w1
    c_tok = int(_field(usage, "completion_tokens", "output_tokens") or 0)
    return p_tok, c_tok, reason, cached, w5, w1


class BurndownAndTrafficTypeLogger(CustomLogger):  # type: ignore[misc]
    """Tracks server-side rolling Redis team burndown and flips bursting teams onto spillover."""

    def __init__(self, time_fn: Any = time.time) -> None:
        super().__init__()
        self._time_fn = time_fn
        self._cache_ref: Any = None
        self._local_window: dict[str, float] = {}

    def _window_key(self, team_id: str) -> str:
        minute_bucket = int(self._time_fn() // 60)
        return f"capacity:burndown:{team_id}:{minute_bucket}"

    async def _read_counter(self, cache: Any, key: str) -> float:
        if cache is not None and hasattr(cache, "async_get_cache"):
            val = await cache.async_get_cache(key)
            if val is not None:
                return float(val)
        return float(self._local_window.get(key, 0.0))

    async def _increment_counter(self, cache: Any, key: str, delta: float) -> float:
        if delta == 0.0:
            return await self._read_counter(cache, key)
        if cache is not None and hasattr(cache, "async_increment"):
            updated = await cache.async_increment(key, delta, ttl=WINDOW_TTL_SECONDS)
            if updated is not None:
                self._local_window[key] = float(updated)
                return float(updated)
        new_val = self._local_window.get(key, 0.0) + delta
        self._local_window[key] = new_val
        return new_val

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        if cache is not None:
            self._cache_ref = cache
        team_id = str(getattr(user_api_key_dict, "team_id", None) or "default")
        funded_ceiling_raw = getattr(user_api_key_dict, "max_budget_tpm", None)
        team_funded_ceiling = (
            float(funded_ceiling_raw)
            if funded_ceiling_raw is not None
            else float(DEFAULT_TEAM_FUNDED_BURNDOWN_TPM)
        )
        key = self._window_key(team_id)
        rolling_burndown = await self._read_counter(cache, key)
        # Reserve a pre-SSE floor immediately so concurrent slow streams cannot bypass quota.
        await self._increment_counter(cache, key, float(PREFLIGHT_FLOOR_TOKENS))

        proxy_meta = data.setdefault("litellm_params", {}).setdefault("metadata", {})
        proxy_meta["_burndown_window_key"] = key
        proxy_meta["_burndown_preflight_reserved"] = float(PREFLIGHT_FLOOR_TOKENS)

        if rolling_burndown + PREFLIGHT_FLOOR_TOKENS > team_funded_ceiling:
            headers = dict(data.get("extra_headers") or {})
            headers["X-Vertex-AI-LLM-Request-Type"] = "shared"
            headers["X-Vertex-AI-LLM-Shared-Request-Type"] = SPILLOVER_SHARED_HEADER
            data["extra_headers"] = headers
        return data

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        usage = getattr(response_obj, "usage", None) or {}
        prompt_tok, comp_tok, reason_tok, cached_tok, w5_tok, w1_tok = _extract_usage_counts(usage)
        burndown = compute_burndown_tokens(
            prompt_tok,
            comp_tok,
            reasoning_tokens=reason_tok,
            cached_tokens=cached_tok,
            cache_write_5m_tokens=w5_tok,
            cache_write_1h_tokens=w1_tok,
        )
        litellm_params = kwargs.setdefault("litellm_params", {})
        meta = litellm_params.setdefault("metadata", {})
        team_id = str(meta.get("user_api_key_team_id") or "default")
        key = str(meta.get("_burndown_window_key") or self._window_key(team_id))
        reserved = float(meta.get("_burndown_preflight_reserved", 0.0))
        cache = litellm_params.get("cache") or self._cache_ref
        await self._increment_counter(cache, key, burndown - reserved)

        raw_traffic_type = str(
            kwargs.get("additional_args", {}).get("traffic_type", "PROVISIONED_THROUGHPUT")
        )
        meta["weighted_burndown_tokens"] = round(burndown, 2)
        meta["vertex_traffic_type"] = raw_traffic_type


burndown_handler = BurndownAndTrafficTypeLogger()
