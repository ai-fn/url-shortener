-- kafka_flush_interval_ms is the setting that bites: at low volume the block never
-- fills, so the interval alone drives part creation and the 1000ms default wedges
-- the consumer on TOO_MANY_PARTS. 7.5s of analytics latency is invisible.
CREATE TABLE IF NOT EXISTS kafka_clicks_queue
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
ENGINE = Kafka
SETTINGS
    kafka_broker_list                = 'kafka:9092',
    kafka_topic_list                 = 'clicks',
    kafka_group_name                 = 'ch_clicks',
    kafka_format                     = 'JSONEachRow',
    kafka_num_consumers              = 1,
    kafka_thread_per_consumer        = 0,
    kafka_max_block_size             = 100000,
    kafka_poll_max_batch_size        = 8192,
    kafka_flush_interval_ms          = 7500,
    kafka_handle_error_mode          = 'stream',
    kafka_commit_every_batch         = 0,
    input_format_skip_unknown_fields = 1;
