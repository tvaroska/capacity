"""LiteLLM Proxy scale auditor, config builder, and callback renderer (`ADR-007`)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

from capacity.contract.common import PACKAGE_VERSION
from capacity.contract.results import LiteLLMScaleAudit
from capacity.engine.catalog import GEMINI_1S_GSU_THRESHOLD, ModelCatalog, resolve_model_family

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

_CALLBACK_SOURCE = Path(__file__).with_name("litellm_burndown_callback.py")


def render_burndown_callback(constants: Mapping[str, object]) -> str:
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


def build_litellm_yaml(
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
