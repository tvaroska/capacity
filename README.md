# capacity — Vertex AI Capacity Planner

Recommend GSU (GPU Serving Unit) allocation for Vertex AI models based on historical token throughput from GCP Cloud Monitoring.

Available as a **CLI** (Typer) and a **REST API** (FastAPI).

## Prerequisites

- Python 3.14+
- [uv](https://github.com/astral-sh/uv)
- GCP credentials with `monitoring.timeSeries.list` permission (e.g. `roles/monitoring.viewer`)

```bash
gcloud auth application-default login
uv sync
```

## CLI

```bash
# Recommend GSUs with default percentiles (P50, P75, P90, P95)
capacity recommend --project my-project

# Custom time range and percentiles
capacity recommend --project my-project --days 30 --percentile 50 --percentile 90

# Filter by model or region
capacity recommend --project my-project --model gemini-2.0-flash --region us-central1

# JSON output (same schema as the REST API)
capacity recommend --project my-project --format json
```

Example table output:

```
Model                  P50 TPM  P50 GSUs  P90 TPM  P90 GSUs
—————                  ———————  ————————  ———————  ————————
gemini-2.0-flash       12000    2         45000    8
gemini-embedding-001   120      1         300      1
```

### CLI reference

| Flag | Description | Default |
|------|-------------|---------|
| `--project` | GCP project ID (required) | — |
| `--days` | Days of history | 7 |
| `--percentile` | Percentile to compute (repeatable) | 50, 75, 90, 95 |
| `--model` | Filter by model ID (repeatable) | all models |
| `--region` | Filter by region (repeatable) | all regions |
| `--format` | Output format: `table` or `json` | table |

## REST API

Start the server:

```bash
capacity serve
capacity serve --port 9000 --reload
```

OpenAPI docs available at `http://localhost:8000/docs`.

### GET /api/v1/recommendations

Recommend GSU allocation based on historical token throughput.

**Query parameters:**

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `project` | string | GCP project ID (required) | — |
| `days` | int | Days of history | 7 |
| `percentile` | int[] | Percentiles to compute (repeatable) | 50, 75, 90, 95 |
| `model` | string[] | Filter by model (repeatable) | all |
| `region` | string[] | Filter by region (repeatable) | all |

**Example:**

```bash
curl "http://localhost:8000/api/v1/recommendations?project=my-project&days=7&percentile=50&percentile=90"
```

**Response:**

```json
{
  "project": "my-project",
  "period_days": 7,
  "percentiles": [50, 90],
  "models": [
    {
      "model": "gemini-2.0-flash",
      "percentiles": {
        "P50": { "tpm": 12000, "recommended_gsus": 2 },
        "P90": { "tpm": 45000, "recommended_gsus": 8 }
      }
    }
  ]
}
```

## GSU capacity configuration

GSU capacity per model is configured via environment variables. The pattern is `GSU_CAPACITY_{MODEL}` where the model name has dots and dashes replaced with underscores, uppercased.

```bash
export GSU_CAPACITY_GEMINI_2_0_FLASH=6000
export GSU_CAPACITY_GEMINI_2_0_FLASH_LITE=9000
export GSU_CAPACITY_GEMINI_2_5_PRO=2000
```

Recommended GSUs = `ceil(tokens_per_minute / capacity_per_gsu)`. Any nonzero traffic gets at least 1 GSU.

If no env var is set for a model, `recommended_gsus` will be `null` in the output.

> **Note:** The `.env` file is not auto-loaded by the application. In Docker, pass `--env-file .env` to `docker run`. In Cloud Run, set environment variables via the service configuration or `gcloud run deploy --env-vars-file`.

## How it works

1. Fetches `consumed_token_throughput` from GCP Monitoring API (dedicated + shared, all modalities)
2. Builds a per-minute time series per model over the requested window
3. Computes percentiles across all minutes (idle minutes count as zero)
4. Divides each percentile's TPM by the model's GSU capacity to get recommended GSU count

## Project structure

```
capacity/
├── api.py              # FastAPI routes
├── capacity.py         # GSU recommendation logic
├── cli.py              # Typer CLI
├── core.py             # shared GCP Monitoring fetch logic
└── models.py           # Pydantic response models
```
