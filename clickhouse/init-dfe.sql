-- Project:   dfe-docker
-- File:      clickhouse/init-dfe.sql
-- Purpose:   Initialise the dfe database and event tables
-- Language:  SQL (ClickHouse)
--
-- License:   FSL-1.1-ALv2
-- Copyright: (c) 2026 HYPERI PTY LIMITED

CREATE DATABASE IF NOT EXISTS dfe;

-- Catch-all table for unrouted events
CREATE TABLE IF NOT EXISTS dfe.default (
    timestamp DateTime64(3) DEFAULT now64(3),
    timestamp_load DateTime64(3) DEFAULT now64(3),
    event_category LowCardinality(String),
    metadata String DEFAULT '{}'
) ENGINE = MergeTree()
ORDER BY (timestamp)
PARTITION BY toYYYYMM(timestamp);

-- Auth events
CREATE TABLE IF NOT EXISTS dfe.auth_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    timestamp_load DateTime64(3) DEFAULT now64(3),
    event_category LowCardinality(String) DEFAULT 'auth',
    event_type LowCardinality(String),
    user_id String,
    username String,
    ip_address String,
    user_agent String,
    success UInt8,
    failure_reason Nullable(String),
    metadata String DEFAULT '{}'
) ENGINE = MergeTree()
ORDER BY (timestamp, user_id)
PARTITION BY toYYYYMM(timestamp);

-- API events
CREATE TABLE IF NOT EXISTS dfe.api_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    timestamp_load DateTime64(3) DEFAULT now64(3),
    event_category LowCardinality(String) DEFAULT 'api',
    endpoint String,
    method LowCardinality(String),
    status_code UInt16,
    response_time_ms UInt32,
    request_id String,
    user_id Nullable(String),
    ip_address String,
    metadata String DEFAULT '{}'
) ENGINE = MergeTree()
ORDER BY (timestamp, endpoint)
PARTITION BY toYYYYMM(timestamp);

-- Admin events
CREATE TABLE IF NOT EXISTS dfe.admin_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    timestamp_load DateTime64(3) DEFAULT now64(3),
    event_category LowCardinality(String) DEFAULT 'admin',
    action LowCardinality(String),
    admin_user_id String,
    target_user_id Nullable(String),
    target_resource Nullable(String),
    details String DEFAULT '{}',
    ip_address String
) ENGINE = MergeTree()
ORDER BY (timestamp, admin_user_id)
PARTITION BY toYYYYMM(timestamp);

-- Error events
CREATE TABLE IF NOT EXISTS dfe.error_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    timestamp_load DateTime64(3) DEFAULT now64(3),
    event_category LowCardinality(String) DEFAULT 'errors',
    error_type LowCardinality(String),
    error_message String,
    stack_trace Nullable(String),
    service LowCardinality(String),
    request_id Nullable(String),
    user_id Nullable(String),
    metadata String DEFAULT '{}'
) ENGINE = MergeTree()
ORDER BY (timestamp, error_type)
PARTITION BY toYYYYMM(timestamp);

-- DLQ table for failed messages (30-day TTL)
CREATE TABLE IF NOT EXISTS dfe.dlq_events (
    timestamp DateTime64(3) DEFAULT now64(3),
    original_topic String,
    error_reason String,
    error_stage LowCardinality(String),
    raw_message String
) ENGINE = MergeTree()
ORDER BY (timestamp, original_topic)
PARTITION BY toYYYYMM(timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY;
