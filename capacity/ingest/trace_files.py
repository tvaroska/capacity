"""Offline JSON minute-TPM trace and `--folder-map` file parsers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from capacity.contract.common import SeriesKey
from capacity.engine.stats import minute_list_to_sparse


def parse_folder_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    stripped = raw.strip()
    if stripped.startswith("{"):
        loaded = json.loads(stripped)
        return {str(k): str(v) for k, v in loaded.items()}
    mapping: dict[str, str] = {}
    for item in stripped.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            mapping[k.strip()] = v.strip()
    return mapping


def load_tpm_list(path: Path) -> list[float]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("--trace-file must contain a JSON array of numbers")
    return [float(x) for x in raw]


def load_sparse_from_trace_file(
    trace_file: Path,
    days: int,
    by_region: bool,
    default_model: str = "gemini-3.8-flash",
) -> tuple[dict[SeriesKey, dict[datetime, int]], int]:
    raw_data = json.loads(trace_file.read_text(encoding="utf-8"))
    total_minutes = days * 24 * 60
    if isinstance(raw_data, list):
        skey = SeriesKey(model=default_model, region="us-central1" if by_region else None)
        return {skey: minute_list_to_sparse(raw_data)}, max(total_minutes, len(raw_data))
    if isinstance(raw_data, dict):
        sparse: dict[SeriesKey, dict[datetime, int]] = {}
        max_len = 0
        for raw_k, vals in raw_data.items():
            if isinstance(vals, list):
                k_str = str(raw_k)
                max_len = max(max_len, len(vals))
                parsed = SeriesKey.parse(k_str, by_region=by_region and "@" in k_str)
                bucket = sparse.setdefault(parsed, {})
                for dt, val in minute_list_to_sparse(vals).items():
                    bucket[dt] = bucket.get(dt, 0) + val
        return sparse, max(total_minutes, max_len)
    raise ValueError(f"Unsupported trace file structure in {trace_file}")
