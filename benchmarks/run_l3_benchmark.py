"""Entrypoint for the `capacity` Open-Loop `Level 3` Empirical Benchmark (`R3`).

Modes:
- `--pilot`: 1-trial smoke pilot across all 5 Track-A sizing arms and 3 Track-B live SSE gateway
  arms (does not write `benchmarks/results/l3_receipt.json`).
- `--live`: Consolidated production run (`N = 10,000` real Azure LLM trace requests + `N = 500`
  live HTTP/1.1 SSE streaming requests per gateway arm = `1,500` live SSE streams), writing
  hashed `benchmarks/results/l3_receipt.json`.
- `--offline` (default): Fast `<2s` deterministic replay verifying `l3_receipt.json` hashes and
  Track-A `L2` oracle parity without opening network sockets.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.live_sse_harness import run_track_b_live_sse
from benchmarks.trace_backtest import run_track_a_backtest

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "benchmarks" / "corpora" / "azure_llm_trace_10k.jsonl.gz"
RECEIPT_PATH = REPO_ROOT / "benchmarks" / "results" / "l3_receipt.json"


def compute_engine_code_sha256() -> str:
    """Hash core capacity physics, FinOps, and gateway modules for provenance integrity."""
    h = hashlib.sha256()
    for rel in (
        "capacity/burst.py",
        "capacity/finops.py",
        "capacity/gateway.py",
        "capacity/self_hosted.py",
        "capacity/capacity.py",
        "capacity/forecasting.py",
    ):
        h.update(rel.encode("utf-8"))
        h.update((REPO_ROOT / rel).read_bytes())
    return h.hexdigest()


def compute_receipt_digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "receipt_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def run_benchmark(mode: str = "offline") -> dict[str, Any]:
    if mode == "pilot":
        track_a = run_track_a_backtest(CORPUS_PATH, pilot=True)
        track_b = asyncio.run(run_track_b_live_sse(pilot=True))
        return {"mode": "pilot", "track_a": track_a, "track_b": track_b}

    if mode == "live":
        track_a = run_track_a_backtest(CORPUS_PATH, pilot=False)
        track_b = asyncio.run(run_track_b_live_sse(pilot=False))
        receipt: dict[str, Any] = {
            "schema_version": "capacity.l3_receipt.v1",
            "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Interpreter version only: a platform string would publish the kernel build of
            # whichever machine regenerated the receipt.
            "environment": {"python": sys.version.split()[0]},
            "engine_code_sha256": compute_engine_code_sha256(),
            "corpus_file": "benchmarks/corpora/azure_llm_trace_10k.jsonl.gz",
            "corpus_gzip_sha256": track_a["corpus_gzip_sha256"],
            "track_a_empirical_trace_backtest": track_a,
            "track_b_live_http_sse_gateway": track_b,
        }
        receipt["receipt_sha256"] = compute_receipt_digest(receipt)
        RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT_PATH.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return receipt

    # Offline verification mode: replay Track A deterministically and verify committed receipt.
    if not RECEIPT_PATH.is_file():
        raise FileNotFoundError(f"Missing committed L3 receipt at {RECEIPT_PATH}")
    committed: dict[str, Any] = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    expected_digest = compute_receipt_digest(committed)
    if committed.get("receipt_sha256") != expected_digest:
        raise ValueError("Committed l3_receipt.json failed SHA-256 tamper check")
    replay_a = run_track_a_backtest(CORPUS_PATH, pilot=False)
    if replay_a["arms"] != committed["track_a_empirical_trace_backtest"]["arms"]:
        raise ValueError("Offline Track-A replay drifted from committed l3_receipt.json")
    return committed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run capacity Level 3 empirical benchmark.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pilot", action="store_true", help="Run 1-trial pilot across all arms")
    group.add_argument(
        "--live", action="store_true", help="Run full L3 benchmark and write receipt"
    )
    group.add_argument("--offline", action="store_true", help="Verify committed receipt offline")
    args = parser.parse_args()

    mode = "pilot" if args.pilot else ("live" if args.live else "offline")
    out = run_benchmark(mode=mode)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
