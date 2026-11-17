"""Verification gate for the committed Open-Loop `Level 3` Empirical Benchmark Receipt (`R3`)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

import benchmarks.run_l3_benchmark as l3_mod
from benchmarks.run_l3_benchmark import (
    CORPUS_PATH,
    RECEIPT_PATH,
    compute_receipt_digest,
    run_benchmark,
)
from capacity import model_families

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


def test_comment_or_whitespace_edit_passes_default_gate_and_fails_release_hash_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(l3_mod, "compute_engine_code_sha256", lambda: "0" * 64)
    receipt = run_benchmark(mode="offline")
    assert receipt["schema_version"] == "capacity.l3_receipt.v1"

    with pytest.raises(ValueError, match="Engine source bytes changed"):
        run_benchmark(mode="offline", verify_source_hash=True)


def test_damaged_receipt_fails_tamper_check(tmp_path: Path) -> None:
    damaged = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    damaged["track_a_empirical_trace_backtest"]["requests_replayed"] = 9_999
    bad_path = tmp_path / "l3_receipt.json"
    bad_path.write_text(json.dumps(damaged), encoding="utf-8")

    with pytest.raises(ValueError, match="failed SHA-256 tamper check"):
        run_benchmark(mode="offline", receipt_path=bad_path)


def test_changed_burndown_weight_fails_behavioural_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutated_flash = dataclasses.replace(
        model_families.GEMINI_1P_FLASH_SPEC,
        standard_output_weight=4.5,
    )
    monkeypatch.setattr(model_families, "GEMINI_1P_FLASH_SPEC", mutated_flash)

    with pytest.raises(ValueError, match="Offline Track-A replay drifted"):
        run_benchmark(mode="offline")


def test_live_receipt_records_git_commit_and_valid_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_track_a: dict[str, Any] = {
        "corpus_gzip_sha256": "abc123",
        "requests_replayed": 10,
        "total_weighted_burndown_tokens": 100.0,
        "arms": {"capacity_compiled": {"total_7d_cost_usd": 1.0}},
        "summary_deltas": {"optimality_gap_vs_l2_oracle_pct": 0.0},
    }

    async def _fake_track_b(*, pilot: bool = False) -> dict[str, Any]:
        _ = pilot
        return {"requests_per_arm": 16, "total_live_sse_streams": 48, "arms": {}}

    monkeypatch.setattr(l3_mod, "run_track_a_backtest", lambda _p, pilot=False: fake_track_a)
    monkeypatch.setattr(l3_mod, "run_track_b_live_sse", _fake_track_b)

    out_path = tmp_path / "l3_receipt.json"
    receipt = run_benchmark(mode="live", receipt_path=out_path)
    assert re.fullmatch(r"[0-9a-f]{40}|unknown", receipt["git_commit"])
    assert receipt["receipt_sha256"] == compute_receipt_digest(receipt)
