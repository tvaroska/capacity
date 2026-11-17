"""Multi-gateway configuration generator & scale auditor (LiteLLM, Envoy AI Gateway, K8s epp)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from capacity.model_families import (
    GEMINI_1S_GSU_THRESHOLD,
    ModelCatalog,
    build_envoy_burndown_cel,
    build_model_catalog,
    normalize_model_id,
    refill_window_for_gsus,
    resolve_model_family,
)
from capacity.models import (
    PACKAGE_VERSION,
    GatewayConfigBundle,
    GatewayConfigRequest,
    GatewayHeaderPolicy,
    LiteLLMScaleAudit,
)

# Pre-SSE request reservation in Envoy Global Rate Limit before streaming completes (ADR-007).
ENVOY_PREFLIGHT_COST_TOKENS = 256
# Minimum per-BU funded TPM floor matching the LiteLLM callback's pre-SSE reservation (ADR-007).
LITELLM_PREFLIGHT_FLOOR_TOKENS = 1_024
# PgBouncer transaction-pool sizing bounds for Prisma query engines (ADR-007).
PGBOUNCER_MIN_POOL_SIZE = 20
PGBOUNCER_MAX_POOL_SIZE = 100
PGBOUNCER_BASE_HEADROOM = 10
# PostgreSQL `max_connections` floor and operational connection headroom (ADR-007).
POSTGRES_MIN_MAX_CONNECTIONS = 100
POSTGRES_ADMIN_HEADROOM = 40
# LiteLLM Redis transaction buffer flush interval in seconds (ADR-007).
REDIS_BUFFER_FLUSH_SECONDS = 10
# Anti-blackhole router cooldowns kept shorter than PT token-bucket recovery (ADR-007).
CLAUDE_COOLDOWN_SECONDS = 5
GEMINI_1S_BUCKET_COOLDOWN_SECONDS = 2
GEMINI_10S_BUCKET_COOLDOWN_SECONDS = 5
# Transient per-minute 429 tolerance before LiteLLM cools down a PT deployment (ADR-007).
MIN_ALLOWED_FAILS = 30
ALLOWED_FAILS_PER_GSU = 3
# Load thresholds where Python GIL / Uvicorn workers require tuning or Envoy migration (ADR-007).
ENVOY_MIGRATION_RPS = 1_500
ENVOY_MIGRATION_SSE = 1_000
TUNE_REQUIRED_RPS = 500
TUNE_REQUIRED_SSE = 300
TUNE_REQUIRED_PRISMA_ENGINES = 32

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


_CALLBACK_SOURCE = Path(__file__).with_name("litellm_burndown_callback.py")


def _render_burndown_callback(constants: Mapping[str, object]) -> str:
    """Return the callback module's source with its constants set for one deployment."""
    source = _CALLBACK_SOURCE.read_text(encoding="utf-8")
    for name, value in constants.items():
        rendered = repr(value).replace("\\", r"\\")
        source, hits = re.subn(rf"(?m)^({name}(?:: [^=\n]+)? = ).*$", rf"\g<1>{rendered}", source)
        if hits != 1:
            raise RuntimeError(f"callback module must assign {name} exactly once, found {hits}")
    return source


def audit_litellm_scaling(
    provisioned_gsus: int = 20,
    target_rps: int = 650,
    concurrent_sse_streams: int = 400,
    pods: int = 12,
    uvicorn_workers_per_pod: int = 4,
    model: str | None = None,
    *,
    catalog: ModelCatalog | None = None,
) -> LiteLLMScaleAudit:
    """Audit LiteLLM Proxy high-QPS bottlenecks (Prisma engines, Postgres locks, SSE GIL)."""
    spec = resolve_model_family(model, catalog=catalog)
    is_claude = spec.family == "claude_3p"
    gsus = max(1, int(provisioned_gsus))
    rps = max(1, int(target_rps))
    sse = max(0, int(concurrent_sse_streams))
    n_pods = max(1, int(pods))
    workers = max(1, int(uvicorn_workers_per_pod))

    total_prisma = n_pods * workers
    pgbouncer_pool = max(
        PGBOUNCER_MIN_POOL_SIZE,
        min(PGBOUNCER_MAX_POOL_SIZE, total_prisma // 2 + PGBOUNCER_BASE_HEADROOM),
    )
    pg_max_conns = max(POSTGRES_MIN_MAX_CONNECTIONS, total_prisma + POSTGRES_ADMIN_HEADROOM)

    cooldown_sec = (
        CLAUDE_COOLDOWN_SECONDS
        if is_claude
        else (
            GEMINI_1S_BUCKET_COOLDOWN_SECONDS
            if gsus >= GEMINI_1S_GSU_THRESHOLD
            else GEMINI_10S_BUCKET_COOLDOWN_SECONDS
        )
    )
    allowed_fails = max(MIN_ALLOWED_FAILS, gsus * ALLOWED_FAILS_PER_GSU)

    bucket_label = "30s/60s" if is_claude else "1s/10s"
    warnings: list[str] = [
        f"Prisma spawns 1 Rust query engine binary per Uvicorn worker ({n_pods} pods * "
        f"{workers} workers = {total_prisma} engines); front PostgreSQL with PgBouncer "
        f"(pool_size={pgbouncer_pool}, max_connections>={pg_max_conns}).",
        f"Enable use_redis_transaction_buffer: true (buffer_time: {REDIS_BUFFER_FLUSH_SECONDS}s) "
        "to batch SpendLogs writes and eliminate PostgreSQL row-lock contention on "
        "LiteLLM_VerificationToken and LiteLLM_TeamTable.",
        f"LiteLLM native tpm_limit counts unweighted raw tokens over 60s; attach "
        f"litellm_burndown_callback.py to enforce {bucket_label} weighted burndown quotas and "
        "capture usage_metadata.traffic_type.",
    ]
    if is_claude:
        warnings.append(
            "3P Claude Provisioned Throughput uses 30s/60s dual buckets (0% burst credit), "
            "lacks Folder RHPT and 1-week terms, and spills over to Standard PayGo rather than "
            "1P Priority PayGo; enforce per-BU burndown limits in the gateway across hub projects."
        )

    if rps > ENVOY_MIGRATION_RPS or sse > ENVOY_MIGRATION_SSE:
        status = "migrate_dataplane_to_envoy"
        warnings.append(
            f"Target load ({rps:,} RPS / {sse:,} concurrent SSE streams) exceeds Python GIL and "
            "asyncio event-loop ceiling; offload data-plane routing and CEL rate limiting to "
            "Envoy AI Gateway while keeping LiteLLM for control-plane key management."
        )
    elif (
        rps >= TUNE_REQUIRED_RPS
        or sse >= TUNE_REQUIRED_SSE
        or total_prisma >= TUNE_REQUIRED_PRISMA_ENGINES
    ):
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
        redis_buffer_time_seconds=REDIS_BUFFER_FLUSH_SECONDS,
        rust_tokenizer_enabled=True,
        anti_blackhole_cooldown_seconds=cooldown_sec,
        anti_blackhole_allowed_fails=allowed_fails,
        scale_status=status,
        bottleneck_warnings=warnings,
    )


def _build_litellm_yaml(
    *,
    norm: str,
    project_id: str,
    location: str,
    gsus: int,
    committed_tpm: int,
    refill_window: int,
    spill_shared_header: str,
    litellm_audit: LiteLLMScaleAudit,
) -> str:
    spill_model_name = f"{norm}-{spill_shared_header}-spillover"
    spill_pool_id = f"vertex-{norm}-{spill_shared_header}-paygo"
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
                "model_name": spill_model_name,
                "litellm_params": {
                    "model": f"vertex_ai/{norm}",
                    "vertex_project": project_id,
                    "vertex_location": location,
                    "extra_headers": {
                        "X-Vertex-AI-LLM-Request-Type": "shared",
                        "X-Vertex-AI-LLM-Shared-Request-Type": spill_shared_header,
                    },
                    "tags": ["prod", "tier-1", "burst"],
                },
                "model_info": {
                    "id": spill_pool_id,
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
            "fallbacks": [{f"{norm}-pt": [spill_model_name]}],
        },
        "litellm_settings": {
            "use_redis_transaction_buffer": True,
            "buffer_time": litellm_audit.redis_buffer_time_seconds,
            "callbacks": ["litellm_burndown_callback.burndown_handler"],
        },
    }
    litellm_header = (
        f"# Production-Hardened LiteLLM Proxy Config (capacity v{PACKAGE_VERSION})\n"
        f"# Model: {norm} | Provisioned GSUs: {gsus} ({committed_tpm:,} TPM, "
        f"{refill_window}s bucket)\n"
    )
    return litellm_header + yaml.safe_dump(litellm_doc, sort_keys=False, default_flow_style=False)


def _build_envoy_yaml(*, norm: str, envoy_cel_expr: str, per_bu_funded_tpm: int) -> str:
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
                                "cel": _LiteralBlockStr(envoy_cel_expr),
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
                                        "number": ENVOY_PREFLIGHT_COST_TOKENS,
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
    envoy_header = (
        f"# Envoy AI Gateway Declarative Weighted Burndown Config (capacity v{PACKAGE_VERSION})\n"
    )
    return envoy_header + yaml.dump_all(
        envoy_docs, Dumper=_GatewayYamlDumper, sort_keys=False, default_flow_style=False
    )


def _build_gke_epp_yaml(self_hosted_model: str) -> str:
    pool_name = _k8s_dns_label(self_hosted_model, suffix="-pool")
    app_label = _k8s_dns_label(self_hosted_model, prefix="vllm-")
    epp_cfg_name = _k8s_dns_label(self_hosted_model, suffix="-epp-config")
    obj_name = _k8s_dns_label(self_hosted_model, suffix="-interactive-critical")

    epp_docs = [
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferencePool",
            "metadata": {"name": pool_name, "namespace": "ai-serving"},
            "spec": {
                "targetPortNumber": 8000,
                "selector": {"app": app_label},
                "extensionRef": {"name": epp_cfg_name},
            },
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "InferenceObjective",
            "metadata": {"name": obj_name, "namespace": "ai-serving"},
            "spec": {"priority": "Critical", "poolRef": {"name": pool_name}},
        },
        {
            "apiVersion": "inference.networking.x-k8s.io/v1alpha2",
            "kind": "EndpointPickerConfig",
            "metadata": {"name": epp_cfg_name, "namespace": "ai-serving"},
            "spec": {
                "scorers": [
                    {"type": "prefix-cache-scorer", "weight": 4, "blockSizeTokens": 64},
                    {
                        "type": "kv-cache-utilization-scorer",
                        "weight": 2,
                        "targetUtilizationPct": 80,
                    },
                    {"type": "queue-scorer", "weight": 2, "maxWaitingRequests": 10},
                    {"type": "lora-affinity-scorer", "weight": 1},
                ]
            },
        },
    ]
    epp_header = "# GKE Inference Gateway / K8s Gateway API Inference Extension (epp / llm-d)\n"
    return epp_header + yaml.dump_all(
        epp_docs, Dumper=_GatewayYamlDumper, sort_keys=False, default_flow_style=False
    )


def generate_gateway_bundle(
    req: GatewayConfigRequest,
    *,
    catalog: ModelCatalog | None = None,
) -> GatewayConfigBundle:
    """Generate production-hardened LiteLLM, Envoy AI Gateway, and GKE epp configurations."""
    cat = catalog if catalog is not None else build_model_catalog()
    record = cat.require(req.model)
    norm = normalize_model_id(req.model)
    spec = record.family_spec
    gsus = req.provisioned_gsus
    refill_window = refill_window_for_gsus(spec, gsus)
    committed_tpm = gsus * record.gsu_capacity_tpm
    per_bu_funded_tpm = max(
        LITELLM_PREFLIGHT_FLOOR_TOKENS, committed_tpm // req.business_unit_shares
    )

    litellm_audit = audit_litellm_scaling(
        provisioned_gsus=gsus,
        target_rps=req.target_rps,
        concurrent_sse_streams=req.concurrent_sse_streams,
        pods=req.pods,
        uvicorn_workers_per_pod=req.uvicorn_workers_per_pod,
        model=req.model,
        catalog=cat,
    )
    spill_shared_header = "priority" if spec.supports_priority_paygo else "standard"
    litellm_yaml = _build_litellm_yaml(
        norm=norm,
        project_id=req.project_id,
        location=req.location,
        gsus=gsus,
        committed_tpm=committed_tpm,
        refill_window=refill_window,
        spill_shared_header=spill_shared_header,
        litellm_audit=litellm_audit,
    )
    callback_py = _render_burndown_callback(
        {
            "MODEL_FAMILY": spec.family,
            "LONG_CONTEXT_THRESHOLD": spec.long_context_threshold,
            "STD_INPUT_WEIGHT": spec.standard_input_weight,
            "STD_CACHED_WEIGHT": spec.standard_cached_read_weight,
            "STD_OUTPUT_WEIGHT": spec.standard_output_weight,
            "CACHE_WRITE_5M_WEIGHT": spec.cache_write_5m_weight,
            "CACHE_WRITE_1H_WEIGHT": spec.cache_write_1h_weight,
            "LONG_INPUT_WEIGHT": spec.long_context_input_weight,
            "LONG_CACHED_WEIGHT": spec.long_context_cached_read_weight,
            "LONG_OUTPUT_WEIGHT": spec.long_context_output_weight,
            "SPILLOVER_SHARED_HEADER": spill_shared_header,
            "COMMITTED_POOL_BURNDOWN_TPM": committed_tpm,
            "DEFAULT_TEAM_FUNDED_BURNDOWN_TPM": per_bu_funded_tpm,
        }
    )
    envoy_yaml = _build_envoy_yaml(
        norm=norm,
        envoy_cel_expr=build_envoy_burndown_cel(spec),
        per_bu_funded_tpm=per_bu_funded_tpm,
    )
    epp_yaml = _build_gke_epp_yaml(req.self_hosted_model)

    return GatewayConfigBundle(
        model=req.model,
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
