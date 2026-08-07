-- is_bot is a dimension, not a filter: 30-50% of hits on a public shortener are
-- preview fetchers and scanners, and folding them in makes every number wrong.
CREATE TABLE IF NOT EXISTS clicks_hourly
(
    link_id      UUID,
    h            DateTime('UTC'),
    is_bot       UInt8,
    country      LowCardinality(String),
    clicks_state AggregateFunction(uniq, UUID)
)
ENGINE = AggregatingMergeTree
PRIMARY KEY (link_id, h)
ORDER BY (link_id, h, is_bot, country);
