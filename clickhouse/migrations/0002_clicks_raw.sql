-- PRIMARY KEY is deliberately shorter than ORDER BY: dedup needs event_id in the sort
-- tuple, but indexing a UUID prunes nothing and would block a later MODIFY ORDER BY.
CREATE TABLE IF NOT EXISTS clicks_raw
(
    event_id       UUID,
    link_id        UUID,
    ts             DateTime64(3, 'UTC'),
    ip_hash        UInt64,
    country        LowCardinality(String),
    device_type    LowCardinality(String),
    browser        LowCardinality(String),
    os             LowCardinality(String),
    referer_domain LowCardinality(String),
    is_bot         UInt8
)
ENGINE = ReplacingMergeTree
PRIMARY KEY (link_id, ts)
ORDER BY (link_id, ts, event_id)
PARTITION BY toYYYYMM(ts)
-- ttl_only_drop_parts drops a part only once every row in it expires, so retention
-- rounds up to the partition: 90 days here becomes up to ~120.
TTL toDateTime(ts) + INTERVAL 90 DAY
SETTINGS
    ttl_only_drop_parts                 = 1,
    non_replicated_deduplication_window = 1000;
