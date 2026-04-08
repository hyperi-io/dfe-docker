-- HyperI DFE aws_cloudtrail 2026-04-08 10:55:17 UTC
CREATE TABLE IF NOT EXISTS {database}.{table}
(
    `_timestamp_load` DateTime64(3,'UTC') DEFAULT now64(3) COMMENT '@generated: now64(3) — Insertion timestamp (ms precision)' CODEC(Delta, LZ4),
    `_timestamp` DateTime64(3,'UTC') COMMENT '@source: timestamp | now() — Event timestamp from source data' CODEC(Delta, ZSTD(1)),
    `_uuid` Nullable(UUID) DEFAULT generateUUIDv7() COMMENT '@generated: generateUUIDv7() — Time-ordered unique event identifier',
    `_org_id` LowCardinality(String) COMMENT '@source: org_id — Tenant/organisation identifier' CODEC(ZSTD(1)),
    `_json` Nullable(JSON) COMMENT '@captured: raw_payload as JSON — Original event payload as structured JSON' CODEC(ZSTD(3)),
    `event_id` Nullable(String) COMMENT '@source: EventId — Unique CloudTrail event identifier' CODEC(ZSTD(1)),
    `event_name` LowCardinality(Nullable(String)) COMMENT '@source: EventName — API action name (e.g. ConsoleLogin, AssumeRole)' CODEC(ZSTD(1)),
    `event_time` Nullable(DateTime64(3,'UTC')) COMMENT '@source: EventTime — When the API call occurred' CODEC(Delta, ZSTD(1)),
    `event_source` LowCardinality(Nullable(String)) COMMENT '@source: EventSource — AWS service (e.g. ec2.amazonaws.com)' CODEC(ZSTD(1)),
    `username` Nullable(String) COMMENT '@source: Username — IAM user or role that performed the action' CODEC(ZSTD(1)),
    `source_ip` Nullable(IPv6) COMMENT '@source: first(SourceIPAddress/sourceIPAddress) — IP address of the caller' CODEC(LZ4),
    `access_key_id` Nullable(String) COMMENT '@source: AccessKeyId — Access key used for the API call' CODEC(ZSTD(1)),
    `user_agent` Nullable(String) COMMENT '@source: UserAgent — Client user agent string' CODEC(ZSTD(3)),
    `error_code` LowCardinality(Nullable(String)) COMMENT '@source: ErrorCode — Error code if the API call failed' CODEC(ZSTD(1)),
    `resource_type` LowCardinality(Nullable(String)) COMMENT '@source: Resources[0].ResourceType — Type of the primary affected resource' CODEC(ZSTD(1)),
    INDEX idx__timestamp `_timestamp` TYPE minmax GRANULARITY 4,
    INDEX idx__org_id `_org_id` TYPE set(0) GRANULARITY 4,
    INDEX idx_event_id `event_id` TYPE bloom_filter GRANULARITY 4,
    INDEX idx_event_name `event_name` TYPE set(0) GRANULARITY 4,
    INDEX idx_event_time `event_time` TYPE minmax GRANULARITY 4,
    INDEX idx_event_source `event_source` TYPE set(0) GRANULARITY 4,
    INDEX idx_username `username` TYPE set(0) GRANULARITY 4,
    INDEX idx_source_ip `source_ip` TYPE minmax GRANULARITY 4,
    INDEX idx_access_key_id `access_key_id` TYPE set(0) GRANULARITY 4,
    INDEX idx_error_code `error_code` TYPE set(0) GRANULARITY 4,
    INDEX idx_resource_type `resource_type` TYPE set(0) GRANULARITY 4,
    PROJECTION timestamp_optimized (SELECT * ORDER BY `_timestamp`)
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(_timestamp_load)
PRIMARY KEY (`_timestamp_load`, `_timestamp`, `_org_id`)
ORDER BY (`_timestamp_load`, `_timestamp`, `_org_id`)
TTL _timestamp + INTERVAL 90 DAY DELETE WHERE _timestamp >= 0,
    _timestamp_load + INTERVAL 90 DAY DELETE WHERE _timestamp_load >= 0
COMMENT '@schema_version: 2 | @profile: minimal | @profile_version: 1.0.0';
