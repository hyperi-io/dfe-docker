<!--
Project:   dfe-docker
File:      examples/aws/README.md
Purpose:   Pull CloudTrail and CloudWatch Logs from your own AWS account with dfe-fetcher
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# AWS logs through the fetcher

Nothing pushes here. dfe-fetcher calls the AWS APIs on a schedule with a read-only key, and each source gets its own fetcher and transform container. CloudTrail comes from `LookupEvents`, which covers the last 90 days of management events in any account with no trail to set up. CloudWatch Logs comes from `FilterLogEvents` on a log group you name. Start the stack on `single-kafka` and log the CLI in first, as [the examples README](../README.md) shows.

You need an IAM user with an access key and nothing more than read. For these two sources that is `cloudtrail:LookupEvents` and `logs:FilterLogEvents`. [dfe-fetcher's AWS guide](https://github.com/hyperi-io/dfe-fetcher/blob/main/docs/cloud-setup/aws.md) has the full policy and the other services it reads.

Put the key in `env/fetcher.env`, which `make init` created and every fetcher container reads. Uncomment its two AWS key lines and fill them in:

```text
AWS_ACCESS_KEY_ID=<your key id>
AWS_SECRET_ACCESS_KEY=<your secret>
```

The sources name those variables with `env:`, so the key never lands in the engine's config history. Set `region` to the one your account works in.

```bash
dfe sources create --source aws-cloudtrail --display-name aws.cloudtrail \
  --schema '{"meta_schema": "meta/elastic/ecs", "derived_schema": "derived/aws/cloudtrail", "additional_fields": "additional/aws/cloudtrail"}' \
  --transform '{"engine": "elastic", "variant": "filebeat.aws_cloudtrail.default"}' \
  --fetcher '{"source_type": "aws", "topic": "own", "config": {
    "region": "us-west-2",
    "access_key_id": "env:AWS_ACCESS_KEY_ID", "secret_access_key": "env:AWS_SECRET_ACCESS_KEY",
    "services": [{"name": "cloudtrail"}]}}' \
  --from-catalogue-item aws-cloudtrail --from-catalogue-intake fetcher
dfe sources deploy aws-cloudtrail
make apply SERVICES="dfe-fetcher-aws-cloudtrail dfe-transform-elastic-aws-cloudtrail"
```

CloudWatch Logs is the same shape with one log group per source. Swap `/aws/lambda/my-function` for one of yours:

```bash
dfe sources create --source aws-cloudwatch-logs --display-name aws.cloudwatch_logs \
  --schema '{"meta_schema": "meta/elastic/ecs", "derived_schema": "derived/aws/cloudwatch_logs", "additional_fields": "additional/aws/cloudwatch_logs"}' \
  --transform '{"engine": "elastic", "variant": "filebeat.aws_cloudwatch_logs.default"}' \
  --fetcher '{"source_type": "aws", "topic": "own", "config": {
    "region": "us-west-2",
    "access_key_id": "env:AWS_ACCESS_KEY_ID", "secret_access_key": "env:AWS_SECRET_ACCESS_KEY",
    "services": [{"name": "cloudwatch_logs", "config": {"log_group_name": "/aws/lambda/my-function"}}]}}' \
  --from-catalogue-item aws-cloudwatch-logs --from-catalogue-intake fetcher
dfe sources deploy aws-cloudwatch-logs
make apply SERVICES="dfe-fetcher-aws-cloudwatch-logs dfe-transform-elastic-aws-cloudwatch-logs"
```

The fetcher polls about every five minutes, and its first poll looks back an hour. Wait out that first five minutes, then count:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT 'aws-cloudtrail' AS source, count() AS rows FROM dfe.\`aws-cloudtrail\`
  UNION ALL
  SELECT 'aws-cloudwatch-logs', count() FROM dfe.\`aws-cloudwatch-logs\` FORMAT PrettySpace"
```

```text
    source                rows

1.  aws-cloudtrail        2698
2.  aws-cloudwatch-logs      0
```

That is one row per CloudTrail event and one per log event in the hour before the first poll. A quiet account gives fewer. CloudWatch shows none here because the group it read had no events in that hour.

A refused key shows up in `docker logs dfe-fetcher-aws-cloudtrail` as an `AccessDeniedException` naming the action and the resource, and that poll's window is tried again on the next one.

To tear it down, delete both sources and apply, which removes their containers. Then take the key back out of `env/fetcher.env`.

```bash
dfe --quiet sources delete aws-cloudtrail
dfe --quiet sources delete aws-cloudwatch-logs
make apply
```
