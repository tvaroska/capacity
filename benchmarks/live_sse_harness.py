"""Track B: Live loopback HTTP/1.1 SSE streaming gateway & weighted-burndown protocol harness.

Spins up a real `uvicorn` ASGI HTTP/1.1 Server-Sent Events (SSE) server on an ephemeral loopback
socket (`127.0.0.1:0`), executes `generate_gateway_bundle().litellm_burndown_callback_py`
against an async Redis-compatible counter store, and streams bimodal (`8K` standard + `240K`
long-context with `thought_signature`) requests over `httpx.AsyncClient(trust_env=False)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from capacity.burst import compute_weighted_burndown_tokens
from capacity.gateway import generate_gateway_bundle


class _InMemoryAsyncRedisCache:
    """Async Redis-compatible atomic counter store (`async_get_cache` + `async_increment`)."""

    def __init__(self) -> None:
        self._data: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def async_get_cache(self, key: str) -> float | None:
        async with self._lock:
            return self._data.get(key)

    async def async_increment(self, key: str, value: float, ttl: int = 120) -> float:
        _ = ttl
        async with self._lock:
            updated = self._data.get(key, 0.0) + float(value)
            self._data[key] = updated
            return updated


def _compile_generated_logger(provisioned_gsus: int = 4) -> Any:  # noqa: ANN401
    bundle = generate_gateway_bundle(
        model="gemini-3.8-flash",
        provisioned_gsus=provisioned_gsus,
        business_unit_shares=2,
    )
    ns: dict[str, Any] = {}
    code_obj = compile(bundle.litellm_burndown_callback_py, "litellm_burndown_callback.py", "exec")
    exec(code_obj, ns)  # noqa: S102
    return ns["BurndownAndTrafficTypeLogger"]


def _build_live_sse_gateway_app(arm: str) -> tuple[FastAPI, dict[str, Any]]:
    """Build a live streaming HTTP SSE gateway + upstream testbed for a specific gateway arm."""
    app = FastAPI()
    redis_cache = _InMemoryAsyncRedisCache()
    logger_cls = _compile_generated_logger(provisioned_gsus=20)
    # Fixed clock inside a single benchmark minute window so all burst trials share one window.
    fixed_now = 1_762_880_000.0
    burndown_logger = logger_cls(time_fn=lambda: fixed_now)

    # Pool physics: 20 GSUs = 2,418,000 burndown TPM across 2 funded BUs.
    pool_pt_ceiling_tpm = 2_418_000.0
    bu_funded_share_by_team = {
        "bu-burst": 850_000.0,
        "bu-steady": 1_568_000.0,
    }

    state: dict[str, Any] = {
        "redis": redis_cache,
        "pool_pt_used": 0.0,
        "unweighted_raw_by_bu": {"bu-burst": 0.0, "bu-steady": 0.0},
        "ground_truth_burndown_total": 0.0,
        "gateway_recorded_burndown_total": 0.0,
        "peer_pt_starvation_events": 0,
        "router_blackhole_drops": 0,
        "router_blackholed_until_seq": -1,
        "replica_prefix_seen": [set() for _ in range(8)],
        "rr_cursor": 0,
        "effective_cached_tokens_sum": 0,
        "total_prompt_tokens_sum": 0,
        "lock": asyncio.Lock(),
    }

    @app.post("/v1/chat/completions", response_model=None)
    async def sse_completions(request: Request) -> StreamingResponse | JSONResponse:
        body = await request.json()
        seq = int(body["seq"])
        bu = str(body["bu"])
        prompt_tokens = int(body["prompt_tokens"])
        static_prefix_tokens = int(body["static_prefix_tokens"])
        static_prefix_id = str(body["static_prefix_id"])
        has_thought_sig = bool(body.get("has_thought_signature", True))
        output_tokens = int(body["output_tokens"])
        reasoning_tokens = int(body["reasoning_tokens"])
        inject_microburst_429 = bool(body.get("inject_microburst_429", False))

        bu_share_tpm = bu_funded_share_by_team[bu]
        extra_headers: dict[str, str] = {}
        data_payload: dict[str, Any] = {"extra_headers": extra_headers, "litellm_params": {}}

        if arm == "capacity_two_phase_gateway":
            api_key_obj = SimpleNamespace(team_id=bu, max_budget_tpm=bu_share_tpm)
            data_payload = await burndown_logger.async_pre_call_hook(
                api_key_obj, redis_cache, data_payload, "acompletion"
            )
            extra_headers = dict(data_payload.get("extra_headers") or {})
        elif arm == "unweighted_60s_default":
            async with state["lock"]:
                # Naive raw-token counter ignores 2x/7.5x >200K weights and reasoning tokens.
                if state["unweighted_raw_by_bu"][bu] > bu_share_tpm:
                    extra_headers["X-Vertex-AI-LLM-Request-Type"] = "shared"
                    extra_headers["X-Vertex-AI-LLM-Shared-Request-Type"] = "priority"

        # Router cooldown behavior after a transient 1s micro-burst 429 at seq == 10
        async with state["lock"]:
            if inject_microburst_429:
                # Default LiteLLM 60s cooldown blackholes the PT pool across subsequent requests;
                # capacity's 5s cooldown + instant Priority fallback spills cleanly with 0 drops.
                if arm == "unweighted_60s_default":
                    state["router_blackholed_until_seq"] = seq + 45
                    state["router_blackhole_drops"] += 1
                    return JSONResponse(status_code=429, content={"error": "PT micro-burst 429"})
                extra_headers["X-Vertex-AI-LLM-Request-Type"] = "shared"
                extra_headers["X-Vertex-AI-LLM-Shared-Request-Type"] = "priority"
            elif arm == "unweighted_60s_default" and seq <= state["router_blackholed_until_seq"]:
                state["router_blackhole_drops"] += 1
                return JSONResponse(
                    status_code=503, content={"error": "PT deployment in 60s cooldown"}
                )

            # Prefix-cache replica routing physics (sticky epp vs 1/8 round-robin + stripped sig)
            if arm == "stateless_rr_stripped":
                rep_idx = state["rr_cursor"] % 8
                state["rr_cursor"] += 1
                sig_ok = False
            else:
                rep_idx = hash(static_prefix_id) % 8
                sig_ok = has_thought_sig

            replica_cache: set[str] = state["replica_prefix_seen"][rep_idx]
            if static_prefix_id in replica_cache and sig_ok:
                cached_tokens = static_prefix_tokens
            elif static_prefix_id in replica_cache and not sig_ok:
                cached_tokens = int(static_prefix_tokens * 0.01)
            else:
                cached_tokens = 0
                replica_cache.add(static_prefix_id)

            true_burndown = compute_weighted_burndown_tokens(
                input_tokens=prompt_tokens,
                output_tokens=output_tokens,
                thoughts_tokens=reasoning_tokens,
                cached_tokens=cached_tokens,
            )
            req_type = extra_headers.get("X-Vertex-AI-LLM-Request-Type", "unset")
            if req_type != "shared":
                over_pool = state["pool_pt_used"] + true_burndown > pool_pt_ceiling_tpm
                if over_pool and bu == "bu-steady":
                    state["peer_pt_starvation_events"] += 1
                state["pool_pt_used"] += true_burndown
                traffic_type = "PROVISIONED_THROUGHPUT"
            else:
                traffic_type = "ON_DEMAND_PRIORITY"

            state["ground_truth_burndown_total"] += true_burndown
            state["effective_cached_tokens_sum"] += cached_tokens
            state["total_prompt_tokens_sum"] += prompt_tokens

        async def _sse_stream() -> AsyncIterator[bytes]:
            yield b'data: {"delta":"chunk-1"}\n\n'
            await asyncio.sleep(0.001)
            usage_dict = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": output_tokens,
                "prompt_tokens_details": {"cached_tokens": cached_tokens},
                "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
                "traffic_type": traffic_type,
            }
            yield f"data: {json.dumps({'usage': usage_dict})}\n\n".encode()

            if arm == "capacity_two_phase_gateway":
                lp = data_payload.setdefault("litellm_params", {})
                lp.setdefault("metadata", {})["user_api_key_team_id"] = bu
                lp["cache"] = redis_cache
                kw = {
                    "litellm_params": lp,
                    "additional_args": {"traffic_type": traffic_type},
                }
                resp_obj = SimpleNamespace(usage=usage_dict)
                await burndown_logger.async_log_success_event(kw, resp_obj, fixed_now, fixed_now)
                logged = float(lp["metadata"]["weighted_burndown_tokens"])
                async with state["lock"]:
                    state["gateway_recorded_burndown_total"] += logged
            else:
                raw_sum = float(prompt_tokens + output_tokens)
                async with state["lock"]:
                    state["unweighted_raw_by_bu"][bu] += raw_sum
                    state["gateway_recorded_burndown_total"] += raw_sum

            yield b"data: [DONE]\n\n"

        return StreamingResponse(_sse_stream(), media_type="text/event-stream")

    return app, state


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _run_single_gateway_arm(arm: str, n_requests: int) -> dict[str, Any]:
    app, state = _build_live_sse_gateway_app(arm)
    port = _pick_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    latencies_ms: list[float] = []
    status_counts: dict[int, int] = {200: 0, 429: 0, 503: 0}

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=15.0,
        ) as client:
            sem = asyncio.Semaphore(32)

            async def _send_one(idx: int) -> None:
                # 70% of traffic comes from `bu-burst` (including >200K reasoning bursts);
                # 30% is steady 16K traffic from `bu-steady` testing peer PT protection.
                is_burst_bu = idx % 10 not in (0, 1, 2)
                bu = "bu-burst" if is_burst_bu else "bu-steady"
                is_long = is_burst_bu and (idx % 2 == 0)
                prompt_tok = 220_000 if is_long else 16_384
                static_tok = 40_960 if is_long else 12_288
                out_tok = 800 if is_long else 400
                reason_tok = 6_000 if is_long else 200
                payload = {
                    "seq": idx,
                    "bu": bu,
                    "prompt_tokens": prompt_tok,
                    "static_prefix_tokens": static_tok,
                    "static_prefix_id": f"prefix-{idx % 4}",
                    "has_thought_signature": True,
                    "output_tokens": out_tok,
                    "reasoning_tokens": reason_tok,
                    "inject_microburst_429": idx == min(12, max(1, n_requests // 3)),
                }
                async with sem:
                    t0 = time.perf_counter()
                    async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                        code = resp.status_code
                        status_counts[code] = status_counts.get(code, 0) + 1
                        async for _chunk in resp.aiter_bytes():
                            pass
                    latencies_ms.append((time.perf_counter() - t0) * 1_000.0)

            # Warm prefix cache + build initial burst ledger sequentially for first 16 requests,
            # then fire the remaining requests concurrently over live HTTP/1.1 SSE streams.
            warmup = min(16, n_requests)
            for i in range(warmup):
                await _send_one(i)
            if n_requests > warmup:
                await asyncio.gather(*(_send_one(i) for i in range(warmup, n_requests)))
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve_task, timeout=3.0)

    gt = max(1.0, float(state["ground_truth_burndown_total"]))
    rec = float(state["gateway_recorded_burndown_total"])
    drift_pct = abs(rec - gt) / gt * 100.0
    tot_prompt = max(1, int(state["total_prompt_tokens_sum"]))
    cache_hit_pct = (int(state["effective_cached_tokens_sum"]) / tot_prompt) * 100.0
    sorted_lat = sorted(latencies_ms)
    p95_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))
    p95_lat = sorted_lat[p95_idx] if sorted_lat else 0.0

    return {
        "requests_sent": n_requests,
        "http_200_streams": status_counts.get(200, 0),
        "router_blackhole_drop_rate_pct": round(
            (int(state["router_blackhole_drops"]) / max(1, n_requests)) * 100.0, 2
        ),
        "burndown_accounting_drift_pct": round(drift_pct, 4),
        "peer_pt_starvation_events": int(state["peer_pt_starvation_events"]),
        "effective_prefix_cache_hit_pct": round(cache_hit_pct, 2),
        "p95_sse_stream_latency_ms": round(p95_lat, 2),
    }


async def run_track_b_live_sse(*, pilot: bool = False) -> dict[str, Any]:
    """Run all 3 gateway arms over live loopback HTTP/1.1 SSE (`N=16` pilot or `N=500` prod)."""
    n_per_arm = 16 if pilot else 500
    arms: dict[str, Any] = {}
    for arm_name in (
        "unweighted_60s_default",
        "stateless_rr_stripped",
        "capacity_two_phase_gateway",
    ):
        arms[arm_name] = await _run_single_gateway_arm(arm_name, n_per_arm)
    return {
        "requests_per_arm": n_per_arm,
        "total_live_sse_streams": n_per_arm * 3,
        "arms": arms,
    }
