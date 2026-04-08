CREATE TABLE IF NOT EXISTS {database}.{table}
(
    `_timestamp_load` DateTime64(3) DEFAULT now64(3) COMMENT '@generated: now64(3) | Load time - primary query filter' CODEC(Delta, ZSTD(1)),
    `_timestamp` DateTime64(3) COMMENT '@source: timestamp | now() | Event occurrence time (minmax indexed)' CODEC(Delta, ZSTD(1)),
    `_timestamp_received` Nullable(DateTime64(3)) COMMENT '@source: timestamp_received | When receiver/loader received the event' CODEC(Delta, ZSTD(1)),
    `_uuid` UUID DEFAULT generateUUIDv7() COMMENT '@generated: generateUUIDv7() | Unique event ID UUIDv7 (time-ordered)',
    `_org_id` LowCardinality(String) COMMENT '@source: org_id | Organisation ID for RLS (first in ORDER BY)' CODEC(ZSTD(1)),
    `_raw` Nullable(String) COMMENT '@renamed: logoriginal | Original log line (zero-copy rename from source)' CODEC(ZSTD(3)),
    `_json` Nullable(JSON) COMMENT '@captured: raw_payload as JSON | Complete Kafka message as JSON' CODEC(ZSTD(3)),
    `_source` LowCardinality(String) COMMENT '@source: first(_source) | topic_name | Destination table / data source identifier' CODEC(ZSTD(1)),
    `_tags` Nullable(JSON) COMMENT '@source: first(tags/_tags/meta/metadata.tags) | Metadata and collector/agent info' CODEC(ZSTD(3)),
    INDEX idx_timestamp _timestamp TYPE minmax GRANULARITY 1
)
ENGINE = MergeTree()
ORDER BY (_org_id, _timestamp_load, _uuid)
PARTITION BY (toYYYYMM(_timestamp_load), _org_id)
SETTINGS index_granularity = 8192