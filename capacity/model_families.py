"""Unified model catalog: capacities, family physics, lifecycle tracks, and display labels."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from capacity.models import UnknownModelError

PublisherName = Literal["google", "anthropic", "self_hosted"]
ModelFamilyName = Literal["gemini_1p", "claude_3p", "self_hosted_vllm"]
LifecycleTrack = Literal["long_term_ga", "short_term_ga", "preview", "legacy_sunset"]

ENV_PREFIX = "GSU_CAPACITY_"
# 1P Gemini switches from the 10-second bucket to the 1-second bucket at 50 GSUs (ADR-002).
GEMINI_1S_GSU_THRESHOLD = 50
# High-speed `-fast` Gemini endpoints trade 2.0x compute burndown for 2.5x decode speed (ADR-002).
FAST_TIER_DECODE_SPEEDUP = 2.5
FAST_TIER_COMPUTE_MULTIPLIER = 2.0

# Version and release-channel suffixes that name the same model and so the same capacity:
# -001, -preview, -preview-05-06, -preview-09-2025, -exp-0827, -exp-03-25, -rc1, -ga, -v2.
_VERSION_SUFFIX_RE = re.compile(
    r"(?:-(?:00\d|preview(?:-\d{2}-\d{2,4})?|exp(?:-\d{4}|-\d{2}-\d{2})?"
    r"|rc\d*|ga|qcd|latest|v\d+))+$",
    re.IGNORECASE,
)
_PREVIEW_CHANNEL_RE = re.compile(
    r"-(?:preview(?:-\d{2}-\d{2,4})?|exp(?:-\d{4}|-\d{2}-\d{2})?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModelFamilySpec:
    """Physical Provisioned Throughput parameters for a model family."""

    publisher: PublisherName
    family: ModelFamilyName
    standard_input_weight: float
    standard_cached_read_weight: float
    standard_output_weight: float
    cache_write_5m_weight: float
    cache_write_1h_weight: float
    long_context_threshold: int | None
    long_context_input_weight: float
    long_context_cached_read_weight: float
    long_context_output_weight: float
    bucket_short_seconds: int
    bucket_long_seconds: int | None
    burst_credit_pct: float
    enforcement_mode: str
    monitoring_metric: str
    min_prefix_floor_tokens: int
    prefix_block_size_tokens: int
    checkpoint_granularity: str
    supports_folder_rhpt: bool
    supports_priority_paygo: bool
    supports_one_week_term: bool


@dataclass(frozen=True)
class ModelRecord:
    """Single source of truth for one managed model's capacity, physics, lifecycle, and label."""

    model_id: str
    display_label: str
    gsu_capacity_tpm: int
    family_spec: ModelFamilySpec
    lifecycle_track: LifecycleTrack = "long_term_ga"
    fast_decode_speedup: float = 1.0
    fast_decode_cost_multiplier: float = 1.0
    in_simulator_menu: bool = False


GEMINI_1P_FLASH_SPEC = ModelFamilySpec(
    publisher="google",
    family="gemini_1p",
    standard_input_weight=1.0,
    standard_cached_read_weight=0.1,
    standard_output_weight=4.0,
    cache_write_5m_weight=1.0,
    cache_write_1h_weight=1.0,
    long_context_threshold=200_000,
    long_context_input_weight=2.0,
    long_context_cached_read_weight=0.2,
    long_context_output_weight=7.5,
    bucket_short_seconds=10,
    bucket_long_seconds=None,
    burst_credit_pct=0.05,
    enforcement_mode="1p_subminute_bucket",
    monitoring_metric="aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput",
    min_prefix_floor_tokens=6_144,
    prefix_block_size_tokens=4_096,
    checkpoint_granularity="block_4096",
    supports_folder_rhpt=True,
    supports_priority_paygo=True,
    supports_one_week_term=True,
)

GEMINI_1P_PRO_SPEC = ModelFamilySpec(
    publisher="google",
    family="gemini_1p",
    standard_input_weight=1.0,
    standard_cached_read_weight=0.1,
    standard_output_weight=4.0,
    cache_write_5m_weight=1.0,
    cache_write_1h_weight=1.0,
    long_context_threshold=200_000,
    long_context_input_weight=2.0,
    long_context_cached_read_weight=0.2,
    long_context_output_weight=7.5,
    bucket_short_seconds=10,
    bucket_long_seconds=None,
    burst_credit_pct=0.05,
    enforcement_mode="1p_subminute_bucket",
    monitoring_metric="aiplatform.googleapis.com/publisher/online_serving/consumed_token_throughput",
    min_prefix_floor_tokens=2_048,
    prefix_block_size_tokens=1,
    checkpoint_granularity="token_boundary",
    supports_folder_rhpt=True,
    supports_priority_paygo=True,
    supports_one_week_term=True,
)

SELF_HOSTED_VLLM_SPEC = ModelFamilySpec(
    publisher="self_hosted",
    family="self_hosted_vllm",
    standard_input_weight=1.0,
    standard_cached_read_weight=0.1,
    standard_output_weight=4.0,
    cache_write_5m_weight=1.0,
    cache_write_1h_weight=1.0,
    long_context_threshold=None,
    long_context_input_weight=1.0,
    long_context_cached_read_weight=0.1,
    long_context_output_weight=4.0,
    bucket_short_seconds=10,
    bucket_long_seconds=None,
    burst_credit_pct=0.0,
    enforcement_mode="self_hosted_replica_pool",
    monitoring_metric="aiplatform.googleapis.com/prediction/online/prediction_count",
    min_prefix_floor_tokens=64,
    prefix_block_size_tokens=64,
    checkpoint_granularity="block_64",
    supports_folder_rhpt=False,
    supports_priority_paygo=False,
    supports_one_week_term=False,
)


def _claude_spec(min_prefix_floor_tokens: int) -> ModelFamilySpec:
    return ModelFamilySpec(
        publisher="anthropic",
        family="claude_3p",
        standard_input_weight=1.0,
        standard_cached_read_weight=0.1,
        standard_output_weight=5.0,
        cache_write_5m_weight=1.25,
        cache_write_1h_weight=2.0,
        long_context_threshold=None,
        long_context_input_weight=1.0,
        long_context_cached_read_weight=0.1,
        long_context_output_weight=5.0,
        bucket_short_seconds=30,
        bucket_long_seconds=60,
        burst_credit_pct=0.0,
        enforcement_mode="3p_dual_bucket_30s_60s",
        monitoring_metric="aiplatform.googleapis.com/publisher/online_serving/token_count",
        min_prefix_floor_tokens=min_prefix_floor_tokens,
        prefix_block_size_tokens=1,
        checkpoint_granularity="token_boundary",
        supports_folder_rhpt=False,
        supports_priority_paygo=False,
        supports_one_week_term=False,
    )


CLAUDE_3P_SONNET_SPEC = _claude_spec(min_prefix_floor_tokens=1_024)
CLAUDE_3P_OPUS_4_SPEC = _claude_spec(min_prefix_floor_tokens=4_096)
CLAUDE_3P_V55_SPEC = _claude_spec(min_prefix_floor_tokens=512)

_BUILTIN_RECORDS: tuple[ModelRecord, ...] = (
    ModelRecord(
        "gemini-2.0-flash",
        "gemini-2.0-flash (Legacy)",
        161_400,
        GEMINI_1P_FLASH_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash-lite (Legacy)",
        484_200,
        GEMINI_1P_FLASH_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "gemini-2.5-pro",
        "gemini-2.5-pro (Legacy)",
        39_000,
        GEMINI_1P_PRO_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "gemini-2.5-flash",
        "gemini-2.5-flash (Legacy)",
        161_400,
        GEMINI_1P_FLASH_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash-lite (Legacy)",
        484_200,
        GEMINI_1P_FLASH_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord("gemini-3-pro", "gemini-3-pro", 30_000, GEMINI_1P_PRO_SPEC),
    ModelRecord("gemini-3.1-pro", "gemini-3.1-pro", 30_000, GEMINI_1P_PRO_SPEC),
    ModelRecord("gemini-3-flash", "gemini-3-flash", 120_900, GEMINI_1P_FLASH_SPEC),
    ModelRecord("gemini-3.1-flash-lite", "gemini-3.1-flash-lite", 241_800, GEMINI_1P_FLASH_SPEC),
    ModelRecord("gemini-3.5-pro", "gemini-3.5-pro", 30_000, GEMINI_1P_PRO_SPEC),
    ModelRecord("gemini-3.5-flash", "gemini-3.5-flash", 120_900, GEMINI_1P_FLASH_SPEC),
    ModelRecord(
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite (4,096-block / 6,144 floor, Long-Term GA)",
        241_800,
        GEMINI_1P_FLASH_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord("gemini-3.6-flash", "gemini-3.6-flash", 120_900, GEMINI_1P_FLASH_SPEC),
    ModelRecord("gemini-3.7-flash", "gemini-3.7-flash", 120_900, GEMINI_1P_FLASH_SPEC),
    ModelRecord(
        "gemini-3.7-flash-fast",
        "gemini-3.7-flash-fast (2.5x decode speed)",
        60_450,
        GEMINI_1P_FLASH_SPEC,
        fast_decode_speedup=FAST_TIER_DECODE_SPEEDUP,
        fast_decode_cost_multiplier=FAST_TIER_COMPUTE_MULTIPLIER,
    ),
    ModelRecord(
        "gemini-3.8-flash",
        "gemini-3.8-flash (4,096-block / 6,144 floor, Long-Term GA)",
        120_900,
        GEMINI_1P_FLASH_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord(
        "gemini-3.8-flash-fast",
        "gemini-3.8-flash-fast (2.5x decode speed)",
        60_450,
        GEMINI_1P_FLASH_SPEC,
        fast_decode_speedup=FAST_TIER_DECODE_SPEEDUP,
        fast_decode_cost_multiplier=FAST_TIER_COMPUTE_MULTIPLIER,
        in_simulator_menu=True,
    ),
    ModelRecord(
        "gemini-4-argon",
        "gemini-4-argon (token-boundary / 2,048 floor, Long-Term GA)",
        30_000,
        GEMINI_1P_PRO_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord(
        "gemini-4-carbon",
        "gemini-4-carbon (500 TPS/GSU, 2,048 floor, Short-Term GA)",
        30_000,
        GEMINI_1P_PRO_SPEC,
        lifecycle_track="short_term_ga",
        in_simulator_menu=True,
    ),
    ModelRecord(
        "gemini-embedding-2",
        "gemini-embedding-2 (5,040 TPS/GSU)",
        302_400,
        GEMINI_1P_FLASH_SPEC,
    ),
    ModelRecord(
        "claude-3-5-sonnet",
        "claude-3-5-sonnet (Legacy)",
        21_000,
        CLAUDE_3P_SONNET_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "claude-3-7-sonnet",
        "claude-3-7-sonnet (Legacy)",
        21_000,
        CLAUDE_3P_SONNET_SPEC,
        lifecycle_track="legacy_sunset",
    ),
    ModelRecord(
        "claude-sonnet-4-6",
        "claude-sonnet-4-6 (350 TPS/GSU, 30s bucket, 1,024 floor)",
        21_000,
        CLAUDE_3P_SONNET_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord("claude-opus-4-5", "claude-opus-4-5", 12_600, CLAUDE_3P_OPUS_4_SPEC),
    ModelRecord(
        "claude-opus-4-6",
        "claude-opus-4-6 (210 TPS/GSU, 30s bucket, 4,096 floor)",
        12_600,
        CLAUDE_3P_OPUS_4_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord(
        "claude-sonnet-5-5",
        "claude-sonnet-5-5 (525 TPS/GSU, 30s bucket, 512 floor)",
        31_500,
        CLAUDE_3P_V55_SPEC,
        in_simulator_menu=True,
    ),
    ModelRecord("claude-opus-5-5", "claude-opus-5-5", 15_660, CLAUDE_3P_V55_SPEC),
    ModelRecord("claude-haiku-5-5", "claude-haiku-5-5", 625_020, CLAUDE_3P_V55_SPEC),
)

BUILTIN_MODELS: dict[str, ModelRecord] = {rec.model_id: rec for rec in _BUILTIN_RECORDS}
CLAUDE_GSU_CONFIG: dict[str, int] = {
    rec.model_id: rec.gsu_capacity_tpm
    for rec in _BUILTIN_RECORDS
    if rec.family_spec.publisher == "anthropic"
}
DEFAULT_GSU_CONFIG: dict[str, int] = {
    rec.model_id: rec.gsu_capacity_tpm for rec in _BUILTIN_RECORDS
}
SELF_HOSTED_MODEL_IDS: frozenset[str] = frozenset(
    {
        "llama-3.3-70b-fp8",
        "qwen-2.5-72b-fp8",
        "gemma-3-27b-bf16",
        "deepseek-r1-distill-32b",
    }
)
_SIMULATOR_MENU_ORDER: tuple[str, ...] = (
    "gemini-3.8-flash",
    "gemini-3.8-flash-fast",
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-3.5-flash-lite",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-sonnet-5-5",
)


def normalize_model_id(model_id: str) -> str:
    """Strip publisher prefixes, folders, `@region` and version suffixes from a model ID."""
    cleaned = model_id.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return _VERSION_SUFFIX_RE.sub("", cleaned)


def refill_window_for_gsus(spec: ModelFamilySpec, provisioned_gsus: int) -> int:
    """Return the active short token-bucket window in seconds for a model family and GSU count."""
    if spec.family == "claude_3p":
        return spec.bucket_short_seconds
    return 1 if provisioned_gsus >= GEMINI_1S_GSU_THRESHOLD else spec.bucket_short_seconds


def build_envoy_burndown_cel(spec: ModelFamilySpec) -> str:
    """Build the Envoy AI Gateway CEL post-SSE burndown expression from the family record."""
    std_block = (
        f"double(input_tokens - cached_input_tokens) * {spec.standard_input_weight:.1f}\n"
        f"      + double(cached_input_tokens) * {spec.standard_cached_read_weight:.1f}\n"
        f"      + double(output_tokens) * {spec.standard_output_weight:.1f}"
    )
    if spec.long_context_threshold is None:
        flat_block = (
            f"double(input_tokens - cached_input_tokens) * {spec.standard_input_weight:.1f}\n"
            f"    + double(cached_input_tokens) * {spec.standard_cached_read_weight:.1f}\n"
            f"    + double(output_tokens) * {spec.standard_output_weight:.1f}"
        )
        return f"int(\n    {flat_block}\n  )\n"
    long_block = (
        f"double(input_tokens - cached_input_tokens) * {spec.long_context_input_weight:.1f}\n"
        f"      + double(cached_input_tokens) * {spec.long_context_cached_read_weight:.1f}\n"
        f"      + double(output_tokens) * {spec.long_context_output_weight:.1f}"
    )
    return (
        f"input_tokens > {spec.long_context_threshold}\n"
        f"  ? int(\n      {long_block}\n    )\n"
        f"  : int(\n      {std_block}\n    )\n"
    )


@dataclass(frozen=True)
class ModelCatalog:
    """Immutable model catalog combining built-in records with env and scenario overrides."""

    records: Mapping[str, ModelRecord]

    def get(self, model_id: str) -> ModelRecord | None:
        raw = model_id.strip().lower().split("@", 1)[0]
        if "/" in raw:
            raw = raw.rsplit("/", 1)[-1]
        for candidate in (raw, normalize_model_id(raw)):
            if candidate in self.records:
                return self.records[candidate]
        return None

    def require(self, model_id: str) -> ModelRecord:
        rec = self.get(model_id)
        if rec is None:
            env_key = ENV_PREFIX + normalize_model_id(model_id).upper().replace(".", "_").replace(
                "-", "_"
            )
            raise UnknownModelError(
                f"No capacity table entry for model '{model_id}'. "
                f"Set {env_key}=<TPM per GSU> to supply one."
            )
        return rec

    def capacity_for(self, model_id: str) -> int | None:
        rec = self.get(model_id)
        return rec.gsu_capacity_tpm if rec is not None else None

    def require_capacity(self, model_id: str) -> int:
        return self.require(model_id).gsu_capacity_tpm

    def simulator_options(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        for mid in _SIMULATOR_MENU_ORDER:
            rec = self.records.get(mid)
            if rec is not None:
                items.append((rec.model_id, rec.display_label))
        return items


def _apply_capacity_override(
    table: dict[str, ModelRecord],
    candidate_ids: tuple[str, ...],
    cap_val: int,
) -> None:
    existing = next((table[cid] for cid in candidate_ids if cid in table), None)
    for cid in candidate_ids:
        if not cid:
            continue
        if existing is not None:
            table[cid] = replace(existing, gsu_capacity_tpm=cap_val)
        else:
            table[cid] = ModelRecord(
                model_id=cid,
                display_label=f"{cid} ({cap_val:,} TPM/GSU override)",
                gsu_capacity_tpm=cap_val,
                family_spec=GEMINI_1P_FLASH_SPEC,
            )


def build_model_catalog(
    overrides: Mapping[str, int] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> ModelCatalog:
    """Build a `ModelCatalog` from `BUILTIN_MODELS`, `GSU_CAPACITY_*` env vars, and `overrides`."""
    table: dict[str, ModelRecord] = dict(BUILTIN_MODELS)
    environ = os.environ if env is None else env
    for key, val in environ.items():
        if key.startswith(ENV_PREFIX):
            model_name = key[len(ENV_PREFIX) :].lower().replace("_", "-")
            dotted_name = re.sub(r"(\d)-(\d)", r"\1.\2", model_name)
            try:
                cap_val = int(val)
            except ValueError:
                continue
            _apply_capacity_override(
                table,
                (
                    model_name,
                    dotted_name,
                    normalize_model_id(model_name),
                    normalize_model_id(dotted_name),
                ),
                cap_val,
            )
    if overrides:
        for raw_model, cap_val in overrides.items():
            cleaned = raw_model.strip().lower().split("@", 1)[0]
            if "/" in cleaned:
                cleaned = cleaned.rsplit("/", 1)[-1]
            _apply_capacity_override(
                table,
                (cleaned, normalize_model_id(cleaned)),
                int(cap_val),
            )
    return ModelCatalog(records=table)


def resolve_model_record(
    model_id: str,
    catalog: ModelCatalog | None = None,
) -> ModelRecord:
    """Return the `ModelRecord` for `model_id`, raising `UnknownModelError` if unlisted."""
    cat = catalog if catalog is not None else build_model_catalog()
    return cat.require(model_id)


def resolve_model_family(
    model_id: str | None,
    catalog: ModelCatalog | None = None,
) -> ModelFamilySpec:
    """Return the `ModelFamilySpec` for `model_id`; raises `UnknownModelError` on unknown IDs."""
    if model_id is None:
        return GEMINI_1P_FLASH_SPEC
    norm = normalize_model_id(model_id)
    if norm in SELF_HOSTED_MODEL_IDS:
        return SELF_HOSTED_VLLM_SPEC
    cat = catalog if catalog is not None else build_model_catalog()
    return cat.require(model_id).family_spec


def is_partner_claude(
    model_id: str | None,
    catalog: ModelCatalog | None = None,
) -> bool:
    """Return True when `model_id` resolves to an Anthropic Claude record in the catalog."""
    if not model_id:
        return False
    cat = catalog if catalog is not None else build_model_catalog()
    rec = cat.get(model_id)
    return rec is not None and rec.family_spec.family == "claude_3p"


def infer_lifecycle_track(
    model_id: str,
    catalog: ModelCatalog | None = None,
) -> LifecycleTrack:
    """Return the GA lifecycle track from the catalog (or `preview` for `-preview`/`-exp`)."""
    raw = model_id.strip().lower().split("@", 1)[0]
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    if _PREVIEW_CHANNEL_RE.search(raw):
        return "preview"
    cat = catalog if catalog is not None else build_model_catalog()
    rec = cat.get(model_id)
    if rec is not None:
        return rec.lifecycle_track
    if normalize_model_id(model_id) in SELF_HOSTED_MODEL_IDS:
        return "long_term_ga"
    return cat.require(model_id).lifecycle_track
