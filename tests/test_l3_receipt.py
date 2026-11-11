"""Verification gate for the committed Open-Loop `Level 3` Empirical Benchmark Receipt (`R3`)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.run_l3_benchmark import (
    CORPUS_PATH,
    RECEIPT_PATH,
    compute_engine_code_sha256,
    compute_receipt_digest,
    run_benchmark,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_l3_corpus_manifest_and_receipt_integrity() -> None:
    manifest = json.loads((REPO_ROOT / "benchmarks/corpora/MANIFEST.json").read_text("utf-8"))
    assert manifest["record_count"] == 10_000
    assert manifest["long_context_gt_200k_count"] >= 100
    actual_gz_sha = hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest()
    assert actual_gz_sha == manifest["gzip_sha256"]

    assert RECEIPT_PATH.is_file(), "Run `python -m benchmarks.run_l3_benchmark --live` first"
    receipt = run_benchmark(mode="offline")
    assert receipt["receipt_sha256"] == compute_receipt_digest(receipt)
    assert receipt["corpus_gzip_sha256"] == actual_gz_sha
    assert receipt["engine_code_sha256"] == compute_engine_code_sha256()

    track_a = receipt["track_a_empirical_trace_backtest"]
    assert track_a["requests_replayed"] == 10_000
    deltas = track_a["summary_deltas"]
    assert deltas["optimality_gap_vs_l2_oracle_pct"] <= 8.0
    assert deltas["cost_savings_vs_static_p99_pct"] >= 20.0
    assert deltas["capacity_tier0_429_rate_pct"] == pytest.approx(0.0)
    assert deltas["naive_tier0_429_rate_pct"] > 5.0

    track_b = receipt["track_b_live_http_sse_gateway"]
    assert track_b["requests_per_arm"] == 500
    assert track_b["total_live_sse_streams"] == 1_500
    cap_arm = track_b["arms"]["capacity_two_phase_gateway"]
    naive_arm = track_b["arms"]["unweighted_60s_default"]
    rr_arm = track_b["arms"]["stateless_rr_stripped"]

    assert cap_arm["burndown_accounting_drift_pct"] <= 0.1
    assert naive_arm["burndown_accounting_drift_pct"] > 20.0
    assert cap_arm["peer_pt_starvation_events"] == 0
    assert cap_arm["router_blackhole_drop_rate_pct"] == pytest.approx(0.0)
    assert naive_arm["router_blackhole_drop_rate_pct"] > 5.0
    assert cap_arm["effective_prefix_cache_hit_pct"] > rr_arm["effective_prefix_cache_hit_pct"] * 5
