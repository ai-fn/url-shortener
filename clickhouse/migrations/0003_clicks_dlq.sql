-- DEFAULT now() is correct here: DLQ arrival metadata, not event time, nothing
-- dedups on it. TTL bounds `raw`, an attacker-controlled payload.
CREATE TABLE IF NOT EXISTS clicks_dlq
(
    raw         String,
    error       String,
    topic       LowCardinality(String),
    partition   UInt64,
    offset      UInt64,
    ingested_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY ingested_at
TTL ingested_at + INTERVAL 14 DAY;
