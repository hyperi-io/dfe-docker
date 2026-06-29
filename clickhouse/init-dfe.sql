-- Project:   dfe-docker
-- File:      clickhouse/init-dfe.sql
-- Purpose:   Initialise the dfe database and default tables
-- Language:  SQL (ClickHouse)
--
-- License:   BUSL-1.1
-- Copyright: (c) 2026 HYPERI PTY LIMITED

CREATE DATABASE IF NOT EXISTS dfe;

CREATE TABLE IF NOT EXISTS dfe.default
    (
        `_timestamp_load` DateTime64(3,'UTC') DEFAULT now64(3) COMMENT '@generated: now64(3) - Insertion timestamp (ms precision)' CODEC(Delta, LZ4),
        `_timestamp` DateTime64(3,'UTC') COMMENT '@source: timestamp | now() - Event timestamp from source data' CODEC(Delta, LZ4),
        `_uuid` Nullable(UUID) DEFAULT generateUUIDv7() COMMENT '@generated: generateUUIDv7() - Time-ordered unique event identifier',
        `_org_id` LowCardinality(String) COMMENT '@source: org_id - Tenant/organisation identifier' CODEC(ZSTD(1)),
        `_json` Nullable(JSON) COMMENT '@captured: raw_payload as JSON - Original event payload as structured JSON' CODEC(ZSTD(3)),
        INDEX idx__timestamp `_timestamp` TYPE minmax GRANULARITY 4,
        INDEX idx__org_id `_org_id` TYPE set(0) GRANULARITY 4,
        PROJECTION _timestamp_optimized (SELECT * ORDER BY `_timestamp`)
    )
    ENGINE = MergeTree()
    PARTITION BY toYYYYMMDD(_timestamp_load)
    PRIMARY KEY (`_timestamp_load`, `_timestamp`, `_org_id`)
    ORDER BY (`_timestamp_load`, `_timestamp`, `_org_id`)
    TTL _timestamp + INTERVAL 90 DAY DELETE WHERE _timestamp >= 0,
    _timestamp_load + INTERVAL 90 DAY DELETE WHERE _timestamp_load >= 0
    SETTINGS
        index_granularity = 2048,
        ttl_only_drop_parts = 1
    COMMENT '@profile: minimal | @profile_version: 1.0.0';