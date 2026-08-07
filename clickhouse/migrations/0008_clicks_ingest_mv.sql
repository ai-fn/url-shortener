-- The WHERE is required, not an optimisation: with kafka_handle_error_mode='stream'
-- the unparseable rows come down this path too unless filtered out here.
CREATE MATERIALIZED VIEW IF NOT EXISTS clicks_ingest_mv TO clicks_raw AS
SELECT
    event_id,
    link_id,
    ts,
    ip_hash,
    country,
    device_type,
    browser,
    os,
    referer_domain,
    is_bot
FROM kafka_clicks_queue
WHERE length(_error) = 0;
