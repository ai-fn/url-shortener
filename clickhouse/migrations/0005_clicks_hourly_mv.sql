-- The zone in toStartOfHour is explicit: without it the container's TZ moves bucket
-- boundaries and the daily numbers quietly stop lining up.
CREATE MATERIALIZED VIEW IF NOT EXISTS clicks_hourly_mv TO clicks_hourly AS
SELECT
    link_id,
    toStartOfHour(ts, 'UTC') AS h,
    is_bot,
    country,
    uniqState(event_id) AS clicks_state
FROM clicks_raw
GROUP BY link_id, h, is_bot, country;
