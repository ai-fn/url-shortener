CREATE MATERIALIZED VIEW IF NOT EXISTS clicks_daily_dims_mv TO clicks_daily_dims AS
SELECT
    link_id,
    toDate(ts, 'UTC') AS d,
    is_bot,
    country,
    device_type,
    browser,
    os,
    referer_domain,
    uniqState(event_id) AS clicks_state
FROM clicks_raw
GROUP BY link_id, d, is_bot, country, device_type, browser, os, referer_domain;
