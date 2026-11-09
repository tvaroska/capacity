"""Multi-gateway configuration generator & scale auditor (LiteLLM, Envoy AI Gateway, K8s epp).

Generates production-hardened routing, weighted burndown rate-limiting, and KV-cache scoring
configurations across three enterprise AI gateway layers:
1. LiteLLM Proxy (`BerriAI/litellm`):
   - Tag-based routing across Dedicated PT, Priority PayGo (1.8x) spillover, Non-Prod Flex (0.5x),
     and Off-Peak PT Scavenging.
   - Anti-blackholing `cooldown_time` and `allowed_fails` tuned to 1s (>=50 GSUs) vs 10s (<50 GSUs)
     token-bucket refill windows.
   - Drop-in `CustomLogger` (`litellm_burndown_callback.py`) enforcing weighted burndown token
     quotas (`1.0x`/`0.1x`/`4.0x` at `<=200K`; `2.0x`/`0.2x`/`7.5x` at `>200K`), dynamic PT-bypass
     header injection on team burst, and 0ms-lag `usage_metadata.traffic_type` attribution.
   - High-QPS scale auditor (`>500-2,000 RPS`) for Prisma Rust subprocesses, Postgres row locks,
     and Python GIL / SSE event-loop saturation.
2. Envoy AI Gateway (`envoyproxy/ai-gateway`):
   - Declarative `AIGatewayRoute` + `BackendTrafficPolicy` with two-phase pre-flight check
     (`cost.request.number: 0`) and post-response `type: CEL` weighted burndown deduction.
3. GKE Inference Gateway / K8s Gateway API Inference Extension (`epp` / `llm-d`):
   - Declarative `InferencePool`, `InferenceObjective` (`Critical` / `Standard` / `Sheddable`), and
     `EndpointPickerConfig` (`prefix-cache-scorer`, `kv-cache-utilization-scorer`, `queue-scorer`).
"""

from __future__ import annotations

from capacity.capacity import DEFAULT_GSU_CONFIG, normalize_model_id
from capacity.models import (
    GatewayConfigBundle,
    GatewayHeaderPolicy,
    LiteLLMScaleAudit,
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

    # Match cooldown to the 1s (>=50 GSUs) vs 10s (<50 GSUs) token-bucket refill window
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
) -> GatewayConfigBundle:
    """Generate production-hardened LiteLLM, Envoy AI Gateway, and GKE epp configurations."""
    norm = normalize_model_id(model)
    gsus = max(1, int(provisioned_gsus))
    refill_window = 1 if gsus >= 50 else 10
    tpm_per_gsu = DEFAULT_GSU_CONFIG.get(norm, 120_900)
    committed_tpm = gsus * tpm_per_gsu

    litellm_audit = audit_litellm_scaling(
        provisioned_gsus=gsus,
        target_rps=target_rps,
        concurrent_sse_streams=concurrent_sse_streams,
        pods=pods,
        uvicorn_workers_per_pod=uvicorn_workers_per_pod,
    )

    litellm_yaml = f"""# Production-Hardened LiteLLM Proxy Config (capacity v0.3.0)
# Model: {norm} | Provisioned GSUs: {gsus} ({committed_tpm:,} TPM, {refill_window}s bucket)
model_list:
  # Tier 1 Primary: Provisioned Throughput (Dedicated Pool -> 429 triggers instant spillover)
  - model_name: {norm}-pt
    litellm_params:
      model: vertex_ai/{norm}
      vertex_project: "{project_id}"
      vertex_location: "{location}"
      extra_headers:
        X-Vertex-AI-LLM-Request-Type: "dedicated"
      tags: ["prod", "tier-1"]
    model_info:
      id: "vertex-{norm}-pt-pool"
      input_cost_per_token: 0.00000050
      output_cost_per_token: 0.00000200

  # Tier 1 Burst Spillover: Priority PayGo (Rank 2 CRITICAL, 1.8x Rate)
  - model_name: {norm}-priority-spillover
    litellm_params:
      model: vertex_ai/{norm}
      vertex_project: "{project_id}"
      vertex_location: "{location}"
      extra_headers:
        X-Vertex-AI-LLM-Request-Type: "shared"
        X-Vertex-AI-LLM-Shared-Request-Type: "priority"
      tags: ["prod", "tier-1", "burst"]
    model_info:
      id: "vertex-{norm}-priority-paygo"
      input_cost_per_token: 0.00000090
      output_cost_per_token: 0.00000360

  # Tier 2 Non-Prod & Daytime Eval: Flex PayGo (Rank 6, 0.5x Rate — Never Drains Prod PT)
  - model_name: {norm}-nonprod-flex
    litellm_params:
      model: vertex_ai/{norm}
      vertex_project: "{project_id}"
      vertex_location: "{location}"
      extra_headers:
        X-Vertex-AI-LLM-Request-Type: "shared"
        X-Vertex-AI-LLM-Shared-Request-Type: "flex"
      tags: ["nonprod", "eval"]

  # Tier 3 Off-Peak PT Scavenger: $0 PT-First with 0.5x Flex Overflow
  - model_name: {norm}-offpeak-scavenger
    litellm_params:
      model: vertex_ai/{norm}
      vertex_project: "{project_id}"
      vertex_location: "{location}"
      extra_headers:
        X-Vertex-AI-LLM-Shared-Request-Type: "flex"
      tags: ["scavenger", "batch"]

router_settings:
  routing_strategy: simple-shuffle
  enable_tag_filtering: true
  num_retries: 1
  # Anti-blackholing: tuned to {refill_window}s token-bucket refill window
  allowed_fails: {litellm_audit.anti_blackhole_allowed_fails}
  cooldown_time: {litellm_audit.anti_blackhole_cooldown_seconds}
  fallbacks:
    - {{"{norm}-pt": ["{norm}-priority-spillover"]}}

litellm_settings:
  use_redis_transaction_buffer: true
  buffer_time: {litellm_audit.redis_buffer_time_seconds}
  callbacks: ["litellm_burndown_callback.burndown_handler"]
"""

    callback_py = f'''"""LiteLLM CustomLogger for Weighted Burndown & traffic_type."""

from __future__ import annotations

from typing import Any

from litellm.integrations.custom_logger import CustomLogger

LONG_CONTEXT_THRESHOLD = 200_000
COMMITTED_POOL_BURNDOWN_TPM = {committed_tpm}


def compute_burndown_tokens(
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    """Compute hardware-weighted burndown tokens (<=200K vs >200K cliff)."""
    uncached = max(0, prompt_tokens - cached_tokens)
    decode = max(0, completion_tokens) + max(0, reasoning_tokens)
    if prompt_tokens > LONG_CONTEXT_THRESHOLD:
        return (uncached * 2.0) + (cached_tokens * 0.2) + (decode * 7.5)
    return (uncached * 1.0) + (cached_tokens * 0.1) + (decode * 4.0)


class BurndownAndTrafficTypeLogger(CustomLogger):
    """Enforces team burndown shares and records 0ms traffic_type into SpendLogs."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        metadata = data.get("metadata") or {{}}
        team_burndown_tpm = float(metadata.get("team_current_burndown_tpm", 0.0))
        team_funded_ceiling_tpm = float(
            metadata.get("team_funded_burndown_tpm", COMMITTED_POOL_BURNDOWN_TPM)
        )
        # If team exceeds its funded PT share, bypass PT so peers are never starved
        if team_burndown_tpm > team_funded_ceiling_tpm:
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
        prompt_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        comp_tok = int(getattr(usage, "completion_tokens", 0) or 0)
        burndown = compute_burndown_tokens(prompt_tok, comp_tok)
        raw_traffic_type = str(
            kwargs.get("additional_args", {{}}).get("traffic_type", "PROVISIONED_THROUGHPUT")
        )
        litellm_params = kwargs.setdefault("litellm_params", {{}})
        meta = litellm_params.setdefault("metadata", {{}})
        meta["weighted_burndown_tokens"] = round(burndown, 2)
        meta["vertex_traffic_type"] = raw_traffic_type


burndown_handler = BurndownAndTrafficTypeLogger()
'''

    envoy_yaml = f"""# Envoy AI Gateway Declarative Weighted Burndown Config (capacity v0.3.0)
apiVersion: aigateway.envoyproxy.io/v1alpha1
kind: AIGatewayRoute
metadata:
  name: {norm}-weighted-burndown-route
  namespace: ai-gateway
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: central-ai-gateway
  rules:
    - matches:
        - headers:
            - type: Exact
              name: x-ai-eg-model
              value: {norm}
      backendRefs:
        - name: vertex-{norm}-backend
          weight: 100
      llmRequestCosts:
        - metadataKey: llm_input_token
          type: InputToken
        - metadataKey: llm_cached_input_token
          type: CachedInputToken
        - metadataKey: llm_output_token
          type: OutputToken
        - metadataKey: vertex_burndown_tokens
          type: CEL
          cel: >
            input_tokens > 200000
              ? int(
                  double(input_tokens - cached_input_tokens) * 2.0
                  + double(cached_input_tokens) * 0.2
                  + double(output_tokens) * 7.5
                )
              : int(
                  double(input_tokens - cached_input_tokens) * 1.0
                  + double(cached_input_tokens) * 0.1
                  + double(output_tokens) * 4.0
                )
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: {norm}-burndown-ratelimit
  namespace: ai-gateway
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: central-ai-gateway
  rateLimit:
    type: Global
    global:
      rules:
        - clientSelectors:
            - headers:
                - name: x-business-unit
                  type: Distinct
          cost:
            request:
              from: Number
              number: 0
            response:
              from: Metadata
              metadata:
                namespace: io.envoy.ai_gateway
                key: vertex_burndown_tokens
          limit:
            requests: {committed_tpm}
            unit: Minute
"""

    epp_yaml = f"""# GKE Inference Gateway / K8s Gateway API Inference Extension (epp / llm-d)
apiVersion: inference.networking.x-k8s.io/v1alpha2
kind: InferencePool
metadata:
  name: {self_hosted_model}-pool
  namespace: ai-serving
spec:
  targetPortNumber: 8000
  selector:
    app: vllm-{self_hosted_model}
  extensionRef:
    name: {self_hosted_model}-epp
---
apiVersion: inference.networking.x-k8s.io/v1alpha2
kind: InferenceObjective
metadata:
  name: {self_hosted_model}-interactive-critical
  namespace: ai-serving
spec:
  priority: Critical
  poolRef:
    name: {self_hosted_model}-pool
---
apiVersion: inference.networking.x-k8s.io/v1alpha2
kind: EndpointPickerConfig
metadata:
  name: {self_hosted_model}-epp-config
  namespace: ai-serving
spec:
  scorers:
    - type: prefix-cache-scorer
      weight: 4
      blockSizeTokens: 64
    - type: kv-cache-utilization-scorer
      weight: 2
      targetUtilizationPct: 80
    - type: queue-scorer
      weight: 2
      maxWaitingRequests: 10
    - type: lora-affinity-scorer
      weight: 1
"""

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
