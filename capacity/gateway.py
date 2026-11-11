"""Multi-gateway configuration generator & scale auditor (LiteLLM, Envoy AI Gateway, K8s epp)."""

from __future__ import annotations

import hashlib
import re

import yaml

from capacity.capacity import load_gsu_config, normalize_model_id, require_capacity
from capacity.models import (
    GCP_LOCATION_PATTERN,
    GCP_PROJECT_ID_PATTERN,
    K8S_NAME_PATTERN,
    MODEL_ID_PATTERN,
    GatewayConfigBundle,
    GatewayHeaderPolicy,
    LiteLLMScaleAudit,
)

ENVOY_BURNDOWN_CEL_EXPRESSION = (
    "input_tokens > 200000\n"
    "  ? int(\n"
    "      double(input_tokens - cached_input_tokens) * 2.0\n"
    "      + double(cached_input_tokens) * 0.2\n"
    "      + double(output_tokens) * 7.5\n"
    "    )\n"
    "  : int(\n"
    "      double(input_tokens - cached_input_tokens) * 1.0\n"
    "      + double(cached_input_tokens) * 0.1\n"
    "      + double(output_tokens) * 4.0\n"
    "    )\n"
)

GATEWAY_HEADER_POLICIES: list[GatewayHeaderPolicy] = [
    GatewayHeaderPolicy(
        tier_id=1,
        scenario="Tier-0 / Tier-1 Prod (Within Team Funded PT Share)",
        request_type_header="<unset>",
        shared_request_type_header="priority",
        uta_rank="Rank 1 CRITICAL_PLUS -> Rank 2 CRITICAL",
        billing_and_capacity_behavior=(
            "Consumes Provisioned Throughput first (99.5% SLA); spills over automatically to "
            "Priority PayGo (1.8x rate) when the PT pool is 100% saturated."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=2,
        scenario="Tier-1 Prod (Team Exceeded Funded PT Share, Bursting)",
        request_type_header="shared",
        shared_request_type_header="priority",
        uta_rank="Rank 2 CRITICAL",
        billing_and_capacity_behavior=(
            "Bypasses PT completely (preventing a bursting team from starving peers' reserved "
            "GSUs) and routes directly to Priority PayGo (1.8x rate) billed to caller labels."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=3,
        scenario="Tier-2 Prod (Standard Interactive Traffic)",
        request_type_header="<unset> or shared",
        shared_request_type_header="standard",
        uta_rank="Rank 3/4 SHEDDABLE_PLUS",
        billing_and_capacity_behavior=(
            "Uses spare PT if unset (or bypasses PT if shared), spilling over to Standard "
            "PayGo (1.0x rate)."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=4,
        scenario="Strict Hard-Capped Prod ($0.00 PayGo Overage Allowed)",
        request_type_header="dedicated",
        shared_request_type_header="<ignored>",
        uta_rank="Rank 1 CRITICAL_PLUS only",
        billing_and_capacity_behavior=(
            "Consumes PT only; immediately returns HTTP 429 on exhaustion, guaranteeing $0.00 "
            "in variable PayGo overage charges."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=5,
        scenario="Daytime Non-Prod (Dev / UAT / Load Tests) & Daytime Eval",
        request_type_header="shared",
        shared_request_type_header="flex",
        uta_rank="Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Bypasses PT and Priority ceilings completely; routes to Flex PayGo (0.5x rate, "
            "50% discount) so non-prod never drains production GSUs."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=6,
        scenario="Off-Peak PT Scavenger Queue ($0.00 Marginal Cost Only)",
        request_type_header="dedicated",
        shared_request_type_header="<ignored>",
        uta_rank="Rank 1 CRITICAL_PLUS (spare headroom)",
        billing_and_capacity_behavior=(
            "Harvests idle overnight/weekend PT GSUs at $0.00 marginal cost via paced online "
            "calls; returns HTTP 429 for queue retry if interactive traffic spikes."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=7,
        scenario="Off-Peak Hybrid PT Scavenger + 0.5x Flex Spillover",
        request_type_header="<unset>",
        shared_request_type_header="flex",
        uta_rank="Rank 1 CRITICAL_PLUS -> Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Consumes spare PT GSUs first at $0.00 marginal cost with zero Flex delay; spills "
            "excess volume above the PT ceiling to 0.5x Flex PayGo."
        ),
    ),
    GatewayHeaderPolicy(
        tier_id=8,
        scenario="Pure Off-Peak / Batch PayGo (Projects Without PT)",
        request_type_header="shared",
        shared_request_type_header="offpeak",
        uta_rank="Rank 6 SHEDDABLE_L",
        billing_and_capacity_behavior=(
            "Bypasses PT and executes at 0.5x (50% discount) during off-peak windows "
            "(Mon-Thu 15:00-21:00 PT & Fri 15:00-Sun 21:00 PT)."
        ),
    ),
]


class _LiteralBlockStr(str):
    """YAML string marker serialized using `|` block style for readable CEL expressions."""


def _literal_str_representer(dumper: yaml.SafeDumper, data: _LiteralBlockStr) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


class _GatewayYamlDumper(yaml.SafeDumper):
    pass


_GatewayYamlDumper.add_representer(_LiteralBlockStr, _literal_str_representer)


def _k8s_dns_label(raw: str, *, prefix: str = "", suffix: str = "") -> str:
    """Build a valid Kubernetes RFC-1035 DNS label (`<= 63` chars, `[a-z0-9-]`, no dots)."""
    stem = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower())
    stem = re.sub(r"-{2,}", "-", stem).strip("-") or "model"
    if not prefix and not stem[0].isalpha():
        stem = f"m-{stem}"
    budget = 63 - len(prefix) - len(suffix)
    if len(stem) > budget:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        keep = max(1, budget - 7)
        stem = f"{stem[:keep].rstrip('-')}-{digest}"
    label = f"{prefix}{stem}{suffix}".strip("-")
    if not label[0].isalpha():
        label = f"k-{label}"[:63].rstrip("-")
    return label


def audit_litellm_scaling(
    provisioned_gsus: int = 20,
    target_rps: int = 650,
    concurrent_sse_streams: int = 400,
    pods: int = 12,
    uvicorn_workers_per_pod: int = 4,
) -> LiteLLMScaleAudit:
    """Audit LiteLLM Proxy high-QPS bottlenecks (Prisma engines, Postgres locks, SSE GIL)."""
    gsus = max(1, int(provisioned_gsus))
    rps = max(1, int(target_rps))
    sse = max(0, int(concurrent_sse_streams))
    n_pods = max(1, int(pods))
    workers = max(1, int(uvicorn_workers_per_pod))

    total_prisma = n_pods * workers
    pgbouncer_pool = max(20, min(100, total_prisma // 2 + 10))
    pg_max_conns = max(100, total_prisma + 40)

    cooldown_sec = 2 if gsus >= 50 else 5
    allowed_fails = max(30, gsus * 3)

    warnings: list[str] = [
        f"Prisma spawns 1 Rust query engine binary per Uvicorn worker ({n_pods} pods * "
        f"{workers} workers = {total_prisma} engines); front PostgreSQL with PgBouncer "
        f"(pool_size={pgbouncer_pool}, max_connections>={pg_max_conns}).",
        "Enable use_redis_transaction_buffer: true (buffer_time: 10s) to batch SpendLogs writes "
        "and eliminate PostgreSQL row-lock contention on LiteLLM_VerificationToken and "
        "LiteLLM_TeamTable.",
        "LiteLLM native tpm_limit counts unweighted raw tokens over 60s; attach "
        "litellm_burndown_callback.py to enforce 1s/10s weighted burndown quotas and capture "
        "usage_metadata.traffic_type.",
    ]

    if rps > 1_500 or sse > 1_000:
        status = "migrate_dataplane_to_envoy"
        warnings.append(
            f"Target load ({rps:,} RPS / {sse:,} concurrent SSE streams) exceeds Python GIL and "
            "asyncio event-loop ceiling; offload data-plane routing and CEL rate limiting to "
            "Envoy AI Gateway while keeping LiteLLM for control-plane key management."
        )
    elif rps >= 500 or sse >= 300 or total_prisma >= 32:
        status = "tune_required"
        warnings.append(
            f"At {rps:,} RPS / {sse:,} SSE streams, set LITELLM_RUST=1 for Rust tokenizers and "
            f"keep router cooldown_time={cooldown_sec}s so momentary PT micro-burst 429s do not "
            "blackhole the PT pool."
        )
    else:
        status = "healthy"

    return LiteLLMScaleAudit(
        target_rps=rps,
        concurrent_sse_streams=sse,
        pods=n_pods,
        uvicorn_workers_per_pod=workers,
        total_prisma_rust_engines=total_prisma,
        recommended_pgbouncer_pool_size=pgbouncer_pool,
        recommended_postgres_max_connections=pg_max_conns,
        use_redis_transaction_buffer=True,
        redis_buffer_time_seconds=10,
        rust_tokenizer_enabled=True,
        anti_blackhole_cooldown_seconds=cooldown_sec,
        anti_blackhole_allowed_fails=allowed_fails,
        scale_status=status,
        bottleneck_warnings=warnings,
    )


def generate_gateway_bundle(
    model: str = "gemini-3.8-flash",
    project_id: str = "prod-genai-pt-host",
    location: str = "global",
    provisioned_gsus: int = 20,
    target_rps: int = 650,
    concurrent_sse_streams: int = 400,
    pods: int = 12,
    uvicorn_workers_per_pod: int = 4,
    self_hosted_model: str = "llama-3.3-70b-fp8",
    business_unit_shares: int = 4,
) -> GatewayConfigBundle:
    """Generate production-hardened LiteLLM, Envoy AI Gateway, and GKE epp configurations."""
    for field_name, value, pattern in (
        ("model", model, MODEL_ID_PATTERN),
        ("project_id", project_id, GCP_PROJECT_ID_PATTERN),
        ("location", location, GCP_LOCATION_PATTERN),
        ("self_hosted_model", self_hosted_model, K8S_NAME_PATTERN),
    ):
        if not re.fullmatch(pattern, value):
            raise ValueError(f"Invalid {field_name} {value!r}: must match {pattern}")
    norm = normalize_model_id(model)
    gsus = max(1, int(provisioned_gsus))
    bu_count = max(1, int(business_unit_shares))
    refill_window = 1 if gsus >= 50 else 10
    tpm_per_gsu = require_capacity(norm, load_gsu_config())
    committed_tpm = gsus * tpm_per_gsu
    per_bu_funded_tpm = max(1_024, committed_tpm // bu_count)

    litellm_audit = audit_litellm_scaling(
        provisioned_gsus=gsus,
        target_rps=target_rps,
        concurrent_sse_streams=concurrent_sse_streams,
        pods=pods,
        uvicorn_workers_per_pod=uvicorn_workers_per_pod,
    )

    litellm_doc = {
        "model_list": [
            {
                "model_name": f"{norm}-pt",
                "litellm_params": {
                    "model": f"vertex_ai/{norm}",
                    "vertex_project": project_id,
                    "vertex_location": location,
                    "extra_headers": {"X-Vertex-AI-LLM-Request-Type": "dedicated"},
                    "tags": ["prod", "tier-1"],
                },
                "model_info": {
                    "id": f"vertex-{norm}-pt-pool",
                    "input_cost_per_token": 0.00000050,
                    "output_cost_per_token": 0.00000200,
                },
            },
            {
                "model_name": f"{norm}-priority-spillover",
                "litellm_params": {
                    "model": f"vertex_ai/{norm}",
                    "vertex_project": project_id,
                    "vertex_location": location,
                    "extra_headers": {
                        "X-Vertex-AI-LLM-Request-Type": "shared",
                        "X-Vertex-AI-LLM-Shared-Request-Type": "priority",
                    },
                    "tags": ["prod", "tier-1", "burst"],
                },
                "model_info": {
                    "id": f"vertex-{norm}-priority-paygo",
                    "input_cost_per_token": 0.00000090,
                    "output_cost_per_token": 0.00000360,
                },
            },
            {
                "model_name": f"{norm}-nonprod-flex",
                "litellm_params": {
                    "model": f"vertex_ai/{norm}",
                    "vertex_project": project_id,
                    "vertex_location": location,
                    "extra_headers": {
                        "X-Vertex-AI-LLM-Request-Type": "shared",
                        "X-Vertex-AI-LLM-Shared-Request-Type": "flex",
                    },
                    "tags": ["nonprod", "eval"],
                },
            },
            {
                "model_name": f"{norm}-offpeak-scavenger",
                "litellm_params": {
                    "model": f"vertex_ai/{norm}",
                    "vertex_project": project_id,
                    "vertex_location": location,
                    "extra_headers": {
                        "X-Vertex-AI-LLM-Shared-Request-Type": "flex",
                    },
                    "tags": ["scavenger", "batch"],
                },
            },
        ],
        "router_settings": {
            "routing_strategy": "simple-shuffle",
            "enable_tag_filtering": True,
            "num_retries": 1,
            "allowed_fails": litellm_audit.anti_blackhole_allowed_fails,
            "cooldown_time": litellm_audit.anti_blackhole_cooldown_seconds,
            "fallbacks": [{f"{norm}-pt": [f"{norm}-priority-spillover"]}],
        },
        "litellm_settings": {
            "use_redis_transaction_buffer": True,
            "buffer_time": litellm_audit.redis_buffer_time_seconds,
            "callbacks": ["litellm_burndown_callback.burndown_handler"],
        },
    }
    litellm_header = (
        "# Production-Hardened LiteLLM Proxy Config (capacity v0.3.0)\n"
        f"# Model: {norm} | Provisioned GSUs: {gsus} ({committed_tpm:,} TPM, "
        f"{refill_window}s bucket)\n"
    )
    litellm_yaml = litellm_header + yaml.safe_dump(
        litellm_doc, sort_keys=False, default_flow_style=False
    )

    callback_py = f'''"""LiteLLM CustomLogger enforcing stateful Redis weighted burndown shares."""

from __future__ import annotations

import time
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover

    class CustomLogger:  # type: ignore[no-redef]
        pass


LONG_CONTEXT_THRESHOLD = 200_000
COMMITTED_POOL_BURNDOWN_TPM = {committed_tpm}
DEFAULT_TEAM_FUNDED_BURNDOWN_TPM = {per_bu_funded_tpm}
PREFLIGHT_FLOOR_TOKENS = 1_024
WINDOW_TTL_SECONDS = 120


def compute_burndown_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    """Compute hardware-weighted burndown tokens (<=200K vs >200K cliff)."""
    total_in = max(0, int(prompt_tokens))
    cached = min(max(0, int(cached_tokens)), total_in)
    uncached = total_in - cached
    decode = max(0, int(completion_tokens)) + max(0, int(reasoning_tokens))
    if total_in > LONG_CONTEXT_THRESHOLD:
        return (uncached * 2.0) + (cached * 0.2) + (decode * 7.5)
    return (uncached * 1.0) + (cached * 0.1) + (decode * 4.0)


def _extract_usage_counts(usage: Any) -> tuple[int, int, int, int]:
    """Extract prompt, completion, reasoning, and cached token counts across SDK/dict shapes."""
    if isinstance(usage, dict):
        p_tok = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        c_tok = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
        p_det = usage.get("prompt_tokens_details") or {{}}
        c_det = usage.get("completion_tokens_details") or {{}}
        cached = int(
            usage.get("cached_tokens", usage.get("cached_content_token_count", 0))
            or (p_det.get("cached_tokens", 0) if isinstance(p_det, dict) else 0)
            or 0
        )
        reason = int(
            usage.get("reasoning_tokens", usage.get("thoughts_tokens", 0))
            or (c_det.get("reasoning_tokens", 0) if isinstance(c_det, dict) else 0)
            or 0
        )
        return p_tok, c_tok, reason, cached

    p_tok = int(getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)) or 0)
    c_tok = int(getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)) or 0)
    p_det = getattr(usage, "prompt_tokens_details", None)
    c_det = getattr(usage, "completion_tokens_details", None)
    cached = int(
        getattr(usage, "cached_tokens", getattr(usage, "cached_content_token_count", 0))
        or (getattr(p_det, "cached_tokens", 0) if p_det is not None else 0)
        or 0
    )
    reason = int(
        getattr(usage, "reasoning_tokens", getattr(usage, "thoughts_tokens", 0))
        or (getattr(c_det, "reasoning_tokens", 0) if c_det is not None else 0)
        or 0
    )
    return p_tok, c_tok, reason, cached


class BurndownAndTrafficTypeLogger(CustomLogger):
    """Tracks server-side rolling Redis team burndown and flips bursting teams onto Priority."""

    def __init__(self, time_fn: Any = time.time) -> None:
        super().__init__()
        self._time_fn = time_fn
        self._cache_ref: Any = None
        self._local_window: dict[str, float] = {{}}

    def _window_key(self, team_id: str) -> str:
        minute_bucket = int(self._time_fn() // 60)
        return f"capacity:burndown:{{team_id}}:{{minute_bucket}}"

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
        # Reserve a pre-SSE floor immediately so concurrent slow >200K streams cannot bypass quota.
        await self._increment_counter(cache, key, float(PREFLIGHT_FLOOR_TOKENS))

        proxy_meta = data.setdefault("litellm_params", {{}}).setdefault("metadata", {{}})
        proxy_meta["_burndown_window_key"] = key
        proxy_meta["_burndown_preflight_reserved"] = float(PREFLIGHT_FLOOR_TOKENS)

        if rolling_burndown + PREFLIGHT_FLOOR_TOKENS > team_funded_ceiling:
            headers = dict(data.get("extra_headers") or {{}})
            headers["X-Vertex-AI-LLM-Request-Type"] = "shared"
            headers["X-Vertex-AI-LLM-Shared-Request-Type"] = "priority"
            data["extra_headers"] = headers
        return data

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        usage = getattr(response_obj, "usage", None) or {{}}
        prompt_tok, comp_tok, reason_tok, cached_tok = _extract_usage_counts(usage)
        burndown = compute_burndown_tokens(
            prompt_tok,
            comp_tok,
            reasoning_tokens=reason_tok,
            cached_tokens=cached_tok,
        )
        litellm_params = kwargs.setdefault("litellm_params", {{}})
        meta = litellm_params.setdefault("metadata", {{}})
        team_id = str(meta.get("user_api_key_team_id") or "default")
        key = str(meta.get("_burndown_window_key") or self._window_key(team_id))
        reserved = float(meta.get("_burndown_preflight_reserved", 0.0))
        cache = litellm_params.get("cache") or self._cache_ref
        await self._increment_counter(cache, key, burndown - reserved)

        raw_traffic_type = str(
            kwargs.get("additional_args", {{}}).get("traffic_type", "PROVISIONED_THROUGHPUT")
        )
        meta["weighted_burndown_tokens"] = round(burndown, 2)
        meta["vertex_traffic_type"] = raw_traffic_type


burndown_handler = BurndownAndTrafficTypeLogger()
'''

    envoy_route_name = _k8s_dns_label(norm, suffix="-weighted-burndown-route")
    envoy_backend_svc = _k8s_dns_label(norm, prefix="vertex-", suffix="-backend")
    envoy_rl_name = _k8s_dns_label(norm, suffix="-burndown-ratelimit")
    envoy_docs = [
        {
            "apiVersion": "aigateway.envoyproxy.io/v1alpha1",
            "kind": "AIGatewayRoute",
            "metadata": {
                "name": envoy_route_name,
                "namespace": "ai-gateway",
            },
            "spec": {
                "targetRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "central-ai-gateway",
                    }
                ],
                "rules": [
                    {
                        "matches": [
                            {
                                "headers": [
                                    {
                                        "type": "Exact",
                                        "name": "x-ai-eg-model",
                                        "value": norm,
                                    }
                                ]
                            }
                        ],
                        "backendRefs": [
                            {
                                "name": envoy_backend_svc,
                                "weight": 100,
                            }
                        ],
                        "llmRequestCosts": [
                            {"metadataKey": "llm_input_token", "type": "InputToken"},
                            {"metadataKey": "llm_cached_input_token", "type": "CachedInputToken"},
                            {"metadataKey": "llm_output_token", "type": "OutputToken"},
                            {
                                "metadataKey": "vertex_burndown_tokens",
                                "type": "CEL",
                                "cel": _LiteralBlockStr(ENVOY_BURNDOWN_CEL_EXPRESSION),
                            },
                        ],
                    }
                ],
            },
        },
        {
            "apiVersion": "gateway.envoyproxy.io/v1alpha1",
            "kind": "BackendTrafficPolicy",
            "metadata": {
                "name": envoy_rl_name,
                "namespace": "ai-gateway",
            },
            "spec": {
                "targetRefs": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "Gateway",
                        "name": "central-ai-gateway",
                    }
                ],
                "rateLimit": {
                    "type": "Global",
                    "global": {
                        "rules": [
                            {
                                "clientSelectors": [
                                    {
                                        "headers": [
                                            {
                                                "name": "x-business-unit",
                                                "type": "Distinct",
                                            }
                                        ]
                                    }
                                ],
                                "cost": {
                                    "request": {
                                        "from": "Number",
                                        "number": 256,
                                    },
                                    "response": {
                                        "from": "Metadata",
                                        "metadata": {
                                            "namespace": "io.envoy.ai_gateway",
                                            "key": "vertex_burndown_tokens",
                                        },
                                    },
                                },
                                "limit": {
                                    "requests": per_bu_funded_tpm,
                                    "unit": "Minute",
                                },
                            }
                        ]
                    },
                },
            },
        },
    ]
    envoy_header = "# Envoy AI Gateway Declarative Weighted Burndown Config (capacity v0.3.0)\n"
    envoy_yaml = envoy_header + yaml.dump_all(
        envoy_docs, Dumper=_GatewayYamlDumper, sort_keys=False, default_flow_style=False
    )

    pool_name = _k8s_dns_label(self_hosted_model, suffix="-pool")
    app_label = _k8s_dns_label(self_hosted_model, prefix="vllm-")
    epp_cfg_name = _k8s_dns_label(self_hosted_model, suffix="-epp-config")
    obj_name = _k8s_dns_label(self_hosted_model, suffix="-interactive-critical")

    epp_docs = [
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferencePool",
            "metadata": {
                "name": pool_name,
                "namespace": "ai-serving",
            },
            "spec": {
                "targetPortNumber": 8000,
                "selector": {"app": app_label},
                "extensionRef": {"name": epp_cfg_name},
            },
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferenceObjective",
            "metadata": {
                "name": obj_name,
                "namespace": "ai-serving",
            },
            "spec": {
                "priority": "Critical",
                "poolRef": {"name": pool_name},
            },
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "EndpointPickerConfig",
            "metadata": {
                "name": epp_cfg_name,
                "namespace": "ai-serving",
            },
            "spec": {
                "scorers": [
                    {
                        "type": "prefix-cache-scorer",
                        "weight": 4,
                        "blockSizeTokens": 64,
                    },
                    {
                        "type": "kv-cache-utilization-scorer",
                        "weight": 2,
                        "targetUtilizationPct": 80,
                    },
                    {
                        "type": "queue-scorer",
                        "weight": 2,
                        "maxWaitingRequests": 10,
                    },
                    {
                        "type": "lora-affinity-scorer",
                        "weight": 1,
                    },
                ]
            },
        },
    ]
    epp_header = "# GKE Inference Gateway / K8s Gateway API Inference Extension (epp / llm-d)\n"
    epp_yaml = epp_header + yaml.dump_all(
        epp_docs, Dumper=_GatewayYamlDumper, sort_keys=False, default_flow_style=False
    )

    return GatewayConfigBundle(
        model=model,
        normalized_model=norm,
        provisioned_gsus=gsus,
        refill_window_seconds=refill_window,
        committed_burndown_tpm=committed_tpm,
        header_policies=GATEWAY_HEADER_POLICIES,
        litellm_audit=litellm_audit,
        litellm_config_yaml=litellm_yaml,
        litellm_burndown_callback_py=callback_py,
        envoy_ai_gateway_yaml=envoy_yaml,
        gke_epp_inference_pool_yaml=epp_yaml,
    )
