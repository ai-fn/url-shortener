-- The other half of kafka_handle_error_mode='stream'. Without this MV a malformed
-- message is dropped with no record of what was lost.
CREATE MATERIALIZED VIEW IF NOT EXISTS clicks_dlq_mv TO clicks_dlq AS
SELECT
    _raw_message AS raw,
    _error       AS error,
    _topic       AS topic,
    _partition   AS partition,
    _offset      AS offset
FROM kafka_clicks_queue
WHERE length(_error) > 0;
