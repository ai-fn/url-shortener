-- Two rollups only. A table per dimension is another MV, another part stream and
-- another migration for data one GROUP BY away.
CREATE TABLE IF NOT EXISTS clicks_daily_dims
(
    link_id        UUID,
    d              Date,
    is_bot         UInt8,
    country        LowCardinality(String),
    device_type    LowCardinality(String),
    browser        LowCardinality(String),
    os             LowCardinality(String),
    referer_domain LowCardinality(String),
    clicks_state   AggregateFunction(uniq, UUID)
)
ENGINE = AggregatingMergeTree
PRIMARY KEY (link_id, d)
ORDER BY (link_id, d, is_bot, country, device_type, browser, os, referer_domain);
