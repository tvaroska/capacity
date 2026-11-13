"""Model-family Provisioned Throughput specifications (1P Gemini vs. 3P Anthropic Claude)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PublisherName = Literal["google", "anthropic"]
ModelFamilyName = Literal["gemini_1p", "claude_3p"]

# Version and release-channel suffixes that name the same model and so the same capacity:
# -001, -preview, -preview-05-06, -preview-09-2025, -exp-0827, -exp-03-25, -rc1, -ga, -v2.
_VERSION_SUFFIX_RE = re.compile(
    r"(?:-(?:00\d|preview(?:-\d{2}-\d{2,4})?|exp(?:-\d{4}|-\d{2}-\d{2})?"
    r"|rc\d*|ga|qcd|latest|v\d+))+$",
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


# Verified public Google Cloud Vertex AI Provisioned Throughput TPM/GSU (TPS_per_GSU * 60).
# Claude keys omit -v2 suffix because normalize_model_id strips trailing -v\d+.
CLAUDE_GSU_CONFIG: dict[str, int] = {
    "claude-3-5-sonnet": 21_000,  # 350 TPS/GSU (claude-3-5-sonnet-v2)
    "claude-3-7-sonnet": 21_000,  # 350 TPS/GSU
    "claude-sonnet-4-6": 21_000,  # 350 TPS/GSU
    "claude-opus-4-5": 12_600,  # 210 TPS/GSU
    "claude-opus-4-6": 12_600,  # 210 TPS/GSU
    "claude-sonnet-5-5": 31_500,  # 525 TPS/GSU
    "claude-opus-5-5": 15_660,  # 261 TPS/GSU
    "claude-haiku-5-5": 625_020,  # 10,417 TPS/GSU
}

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


def normalize_model_id(model_id: str) -> str:
    """Strip publisher prefixes, folders, `@region` and version suffixes from a model ID."""
    cleaned = model_id.strip().lower()
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0]
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    return _VERSION_SUFFIX_RE.sub("", cleaned)


def is_partner_claude(model_id: str | None) -> bool:
    """Return True when the model ID belongs to the 3P Anthropic Claude family."""
    if not model_id:
        return False
    return normalize_model_id(model_id).startswith("claude-")


def resolve_model_family(model_id: str | None) -> ModelFamilySpec:
    """Return the Provisioned Throughput physics specification for a model ID."""
    if not model_id:
        return GEMINI_1P_FLASH_SPEC
    norm = normalize_model_id(model_id)
    if norm.startswith("claude-"):
        if norm.endswith("-5-5"):
            return CLAUDE_3P_V55_SPEC
        if "opus-4-5" in norm or "opus-4-6" in norm:
            return CLAUDE_3P_OPUS_4_SPEC
        return CLAUDE_3P_SONNET_SPEC
    if "argon" in norm or "carbon" in norm or "pro" in norm:
        return GEMINI_1P_PRO_SPEC
    return GEMINI_1P_FLASH_SPEC
