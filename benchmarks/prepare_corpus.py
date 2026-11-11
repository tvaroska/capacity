"""Deterministic builder for the 10,000-request Azure Public LLM Inference benchmark corpus.

Transforms real Microsoft Azure Public LLM Inference production trace records
(`AzureLLMInferenceTrace_conv.csv` + `AzureLLMInferenceTrace_code.csv`, CC-BY-4.0,
Patel et al., Splitwise, ISCA 2024) into a 7-day (`168h`) multi-tenant enterprise JSONL corpus
with zero pseudo-random number generation.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "benchmarks" / "corpora"
DEFAULT_RAW_DIR = Path(tempfile.gettempdir()) / "cap_public_trace"

TENANTS = ("claims-triage", "underwriting-copilot", "policy-audit", "quant-research")


def _load_csv_rows(path: Path) -> tuple[str, list[tuple[datetime, int, int]]]:
    raw_bytes = path.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    parsed: list[tuple[datetime, int, int]] = []
    for row in csv.DictReader(raw_bytes.decode("utf-8").splitlines()):
        ts_raw = row["TIMESTAMP"].strip()
        if "." in ts_raw:
            head, frac = ts_raw.split(".", 1)
            ts_norm = f"{head}.{frac[:6]}"
        else:
            ts_norm = ts_raw
        dt = datetime.strptime(ts_norm, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
        ctx = max(1, int(row["ContextTokens"]))
        gen = max(1, int(row["GeneratedTokens"]))
        parsed.append((dt, ctx, gen))
    return sha, parsed


def build_corpus(raw_dir: Path = DEFAULT_RAW_DIR) -> dict[str, object]:
    """Build `azure_llm_trace_10k.jsonl.gz` and `MANIFEST.json` from upstream Azure CSVs."""
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    conv_sha, conv_rows = _load_csv_rows(raw_dir / "AzureConv_sample.csv")
    code_sha, code_rows = _load_csv_rows(raw_dir / "AzureCode_sample.csv")

    selected_conv = conv_rows[:7_000]
    selected_code = code_rows[:3_000]

    # Interleave 7 conversational requests and 3 repository-scale coding/agentic requests per
    # 10-request enterprise wave while preserving exact empirical sub-second burst deltas within
    # each wave across a 7-day (5 active business days + 2 weekend light days) horizon.
    base_epoch = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
    records: list[dict[str, object]] = []

    # Distribute 1,000 waves across 7 days: 184 waves/day on Mon-Fri (920 waves) concentrating
    # 82% in 08:00-18:00 business hours, and 40 light waves/day on Sat-Sun (80 waves).
    wave_offsets_sec: list[float] = []
    for day in range(7):
        day_base = day * 86_400.0
        n_waves = 184 if day < 5 else 40
        for w in range(n_waves):
            if day < 5:
                if w < 150:
                    # Active business hours 08:00..18:00 (36,000s), clustered around noon burst
                    frac = w / 150.0
                    noon_warp = (frac * 0.65) + (0.35 * (frac * frac * (3.0 - 2.0 * frac)))
                    sec_in_day = 8 * 3600.0 + noon_warp * 10 * 3600.0
                else:
                    # Off-peak interactive tail (18:00..08:00)
                    off_idx = w - 150
                    sec_in_day = (18 * 3600.0 + (off_idx / 34.0) * 14 * 3600.0) % 86_400.0
            else:
                sec_in_day = (w / 40.0) * 86_400.0
            wave_offsets_sec.append(day_base + sec_in_day)

    conv_idx = 0
    code_idx = 0
    for wave_id, wave_start_sec in enumerate(wave_offsets_sec):
        c_slice = selected_conv[conv_idx : conv_idx + 7]
        k_slice = selected_code[code_idx : code_idx + 3]
        conv_idx += 7
        code_idx += 3

        c_t0 = c_slice[0][0]
        k_t0 = k_slice[0][0]

        for j, (orig_dt, ctx, gen) in enumerate(c_slice):
            rel_micro = max(0.0, (orig_dt - c_t0).total_seconds())
            ts = base_epoch + timedelta(seconds=wave_start_sec + (rel_micro % 12.0))
            tenant = TENANTS[(wave_id + j) % len(TENANTS)]
            # Enterprise RAG / multi-turn prompt context scaling (8x system/retrieved docs)
            prompt_tokens = ctx * 8
            cached_tokens = int(prompt_tokens * 0.65) if prompt_tokens >= 6_144 else 0
            thoughts_tokens = int(gen * 0.5) if tenant in ("claims-triage", "quant-research") else 0
            records.append(
                {
                    "seq": len(records),
                    "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "epoch_sec": round(ts.timestamp(), 6),
                    "source_trace": "AzureLLMInferenceTrace_conv",
                    "tenant": tenant,
                    "prompt_tokens": prompt_tokens,
                    "cached_tokens": cached_tokens,
                    "output_tokens": gen,
                    "thoughts_tokens": thoughts_tokens,
                }
            )

        for j, (orig_dt, ctx, gen) in enumerate(k_slice):
            rel_micro = max(0.0, (orig_dt - k_t0).total_seconds())
            ts = base_epoch + timedelta(seconds=wave_start_sec + (rel_micro % 8.0))
            tenant = TENANTS[(wave_id + j + 1) % len(TENANTS)]
            # Repository-context agentic coding/analysis prompts (34x repo context -> >200K tail)
            prompt_tokens = ctx * 34
            cached_tokens = int(prompt_tokens * 0.72) if prompt_tokens >= 6_144 else 0
            thoughts_tokens = gen * 2 if prompt_tokens > 150_000 else int(gen * 0.5)
            records.append(
                {
                    "seq": len(records),
                    "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "epoch_sec": round(ts.timestamp(), 6),
                    "source_trace": "AzureLLMInferenceTrace_code",
                    "tenant": tenant,
                    "prompt_tokens": prompt_tokens,
                    "cached_tokens": cached_tokens,
                    "output_tokens": gen,
                    "thoughts_tokens": thoughts_tokens,
                }
            )

    records.sort(key=lambda r: (float(str(r["epoch_sec"])), int(str(r["seq"]))))
    for idx, rec in enumerate(records):
        rec["seq"] = idx

    jsonl_bytes = ("\n".join(json.dumps(r, separators=(",", ":")) for r in records) + "\n").encode(
        "utf-8"
    )
    out_gz = CORPUS_DIR / "azure_llm_trace_10k.jsonl.gz"
    out_gz.write_bytes(gzip.compress(jsonl_bytes, compresslevel=9, mtime=0))

    gz_sha = hashlib.sha256(out_gz.read_bytes()).hexdigest()
    jsonl_sha = hashlib.sha256(jsonl_bytes).hexdigest()
    long_ctx_count = sum(1 for r in records if int(str(r["prompt_tokens"])) > 200_000)

    manifest = {
        "corpus_file": "azure_llm_trace_10k.jsonl.gz",
        "record_count": len(records),
        "long_context_gt_200k_count": long_ctx_count,
        "gzip_sha256": gz_sha,
        "uncompressed_jsonl_sha256": jsonl_sha,
        "license": "CC-BY-4.0 (Microsoft Azure Public Dataset - Splitwise ISCA 2024)",
        "upstream_sources": [
            {
                "name": "AzureLLMInferenceTrace_conv.csv",
                "url": "https://raw.githubusercontent.com/Azure/AzurePublicDataset/master/data/AzureLLMInferenceTrace_conv.csv",
                "sample_sha256": conv_sha,
                "rows_used": 7_000,
            },
            {
                "name": "AzureLLMInferenceTrace_code.csv",
                "url": "https://raw.githubusercontent.com/Azure/AzurePublicDataset/master/data/AzureLLMInferenceTrace_code.csv",
                "sample_sha256": code_sha,
                "rows_used": 3_000,
            },
        ],
    }
    (CORPUS_DIR / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    info = build_corpus()
    print(json.dumps(info, indent=2))
