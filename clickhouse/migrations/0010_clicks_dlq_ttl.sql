-- 0003's own TTL clause no-ops on any ClickHouse that ran it before this migration
-- existed. Also safe on a fresh clone, where it's a no-op the other way.
ALTER TABLE clicks_dlq MODIFY TTL ingested_at + INTERVAL 14 DAY;
