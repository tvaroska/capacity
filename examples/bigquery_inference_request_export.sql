-- Export zero-payload Observability 2.0 (`aiplatform.googleapis.com/inference_request`) rows
-- from a customer BigQuery Log Analytics sink into JSONL for `capacity collect --logs-file`:
--
--   bq query --use_legacy_sql=false --format=prettyjson < examples/bigquery_inference_request_export.sql > bql_inference.json
--   uv run capacity collect --logs-file bql_inference.json --orders-file examples/orders_overlay.yaml --out scenario.json

SELECT
  FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E6SZ', timestamp) AS timestamp,
  COALESCE(
    JSON_VALUE(resource.labels, '$.model_user_id'),
    JSON_VALUE(resource.labels, '$.model_id'),
    JSON_VALUE(json_payload, '$.model')
  ) AS model_id,
  COALESCE(
    JSON_VALUE(resource.labels, '$.location'),
    'global'
  ) AS location,
  COALESCE(
    JSON_VALUE(json_payload, '$.request_labels.tenant'),
    JSON_VALUE(json_payload, '$.request_labels.business_unit'),
    JSON_VALUE(json_payload, '$.request_labels.app_id'),
    JSON_VALUE(labels, '$.x-business-unit'),
    'unattributed-pool'
  ) AS tenant,
  COALESCE(
    JSON_VALUE(json_payload, '$.traffic_type'),
    JSON_VALUE(json_payload, '$.usage_metadata.traffic_type'),
    'PROVISIONED_THROUGHPUT'
  ) AS traffic_type,
  CAST(COALESCE(JSON_VALUE(json_payload, '$.status_code'), '200') AS INT64) AS status_code,
  CAST(COALESCE(JSON_VALUE(json_payload, '$.prompt_token_count'), '0') AS INT64) AS prompt_token_count,
  CAST(COALESCE(JSON_VALUE(json_payload, '$.candidates_token_count'), '0') AS INT64) AS candidates_token_count,
  CAST(COALESCE(JSON_VALUE(json_payload, '$.thoughts_token_count'), '0') AS INT64) AS thoughts_token_count,
  CAST(COALESCE(JSON_VALUE(json_payload, '$.cached_content_token_count'), '0') AS INT64) AS cached_content_token_count
FROM
  `YOUR_LOG_PROJECT.YOUR_LOG_DATASET._AllLogs`
WHERE
  log_id = 'aiplatform.googleapis.com/inference_request'
  AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);
