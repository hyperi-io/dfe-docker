-- Project:   dfe-docker
-- File:      clickhouse/init-dfe.sql
-- Purpose:   Initialise the dfe database and event tables
-- Language:  SQL (ClickHouse)
--
-- License:   BUSL-1.1
-- Copyright: (c) 2026 HYPERI PTY LIMITED

CREATE DATABASE IF NOT EXISTS dfe;

-- Catch-all table for unrouted events
CREATE TABLE IF NOT EXISTS dfe.default
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
SETTINGS index_granularity = 8192;

-- -- Auth events
-- CREATE TABLE IF NOT EXISTS dfe.auth_events (
--     timestamp DateTime64(3) DEFAULT now64(3),
--     timestamp_load DateTime64(3) DEFAULT now64(3),
--     event_category LowCardinality(String) DEFAULT 'auth',
--     event_type LowCardinality(String),
--     user_id String,
--     username String,
--     ip_address String,
--     user_agent String,
--     success UInt8,
--     failure_reason Nullable(String),
--     metadata String DEFAULT '{}'
-- ) ENGINE = MergeTree()
-- ORDER BY (timestamp, user_id)
-- PARTITION BY toYYYYMM(timestamp);

-- -- API events
-- CREATE TABLE IF NOT EXISTS dfe.api_events (
--     timestamp DateTime64(3) DEFAULT now64(3),
--     timestamp_load DateTime64(3) DEFAULT now64(3),
--     event_category LowCardinality(String) DEFAULT 'api',
--     endpoint String,
--     method LowCardinality(String),
--     status_code UInt16,
--     response_time_ms UInt32,
--     request_id String,
--     user_id Nullable(String),
--     ip_address String,
--     metadata String DEFAULT '{}'
-- ) ENGINE = MergeTree()
-- ORDER BY (timestamp, endpoint)
-- PARTITION BY toYYYYMM(timestamp);

-- -- Admin events
-- CREATE TABLE IF NOT EXISTS dfe.admin_events (
--     timestamp DateTime64(3) DEFAULT now64(3),
--     timestamp_load DateTime64(3) DEFAULT now64(3),
--     event_category LowCardinality(String) DEFAULT 'admin',
--     action LowCardinality(String),
--     admin_user_id String,
--     target_user_id Nullable(String),
--     target_resource Nullable(String),
--     details String DEFAULT '{}',
--     ip_address String
-- ) ENGINE = MergeTree()
-- ORDER BY (timestamp, admin_user_id)
-- PARTITION BY toYYYYMM(timestamp);

-- -- Error events
-- CREATE TABLE IF NOT EXISTS dfe.error_events (
--     timestamp DateTime64(3) DEFAULT now64(3),
--     timestamp_load DateTime64(3) DEFAULT now64(3),
--     event_category LowCardinality(String) DEFAULT 'errors',
--     error_type LowCardinality(String),
--     error_message String,
--     stack_trace Nullable(String),
--     service LowCardinality(String),
--     request_id Nullable(String),
--     user_id Nullable(String),
--     metadata String DEFAULT '{}'
-- ) ENGINE = MergeTree()
-- ORDER BY (timestamp, error_type)
-- PARTITION BY toYYYYMM(timestamp);

-- -- DLQ table for failed messages (30-day TTL)
-- CREATE TABLE IF NOT EXISTS dfe.dlq_events (
--     timestamp DateTime64(3) DEFAULT now64(3),
--     original_topic String,
--     error_reason String,
--     error_stage LowCardinality(String),
--     raw_message String
-- ) ENGINE = MergeTree()
-- ORDER BY (timestamp, original_topic)
-- PARTITION BY toYYYYMM(timestamp)
-- TTL toDateTime(timestamp) + INTERVAL 30 DAY;
