<!--
Project:   dfe-docker
File:      examples/entra/README.md
Purpose:   Pull Microsoft Entra ID directory audit logs from your own tenant with dfe-fetcher
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Entra ID audit logs through the fetcher

dfe-fetcher reads your tenant's directory audit log from Microsoft Graph, `auditLogs/directoryAudits`, on a schedule. That is the record of every change made in the directory: users, groups, apps, roles and policies. Each record lands whole in `dfe.entra-directory-audits`, with no transform to run, so any profile works. Start the stack and log the CLI in first, as [the examples README](../README.md) shows.

You need an app registration in the tenant with a client secret and one Microsoft Graph application permission, `AuditLog.Read.All`, granted admin consent. It reads and nothing more. [dfe-fetcher's Azure guide](https://github.com/hyperi-io/dfe-fetcher/blob/main/docs/cloud-setup/azure.md) has the `az` commands for each step.

Put the three values in `env/fetcher.env`, which `make init` created and every fetcher container reads. Uncomment its `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` and `AZURE_CLIENT_SECRET` lines and fill them in, then add the last line here:

```text
AZURE_TENANT_ID=<your tenant id>
AZURE_CLIENT_ID=<your app's client id>
AZURE_CLIENT_SECRET=<your client secret>
DFE_FETCHER_CURSOR__DEFAULT_WINDOW_HOURS=168
```

A first poll looks back one hour by default. A quiet tenant can go days without a directory change, so that last line widens the first look to seven days. Later polls carry on from where the last one stopped.

The source names those variables with `env:`, so none of the three lands in the engine's config history:

```bash
dfe sources create --source entra-directory-audits --display-name entra.directory_audits \
  --header '{"type": "common-header/timeseries", "version": "1.0.1"}' \
  --schema '{"meta_schema": "meta/azure/entra_directory_audits", "meta_schema_version": "1.0.0"}' \
  --fetcher '{"source_type": "azure", "topic": "own", "config": {
    "tenant_id": "env:AZURE_TENANT_ID", "client_id": "env:AZURE_CLIENT_ID", "client_secret": "env:AZURE_CLIENT_SECRET",
    "services": [{"name": "entra_directory_audits"}]}}'
dfe sources deploy entra-directory-audits
make apply SERVICES=dfe-fetcher-entra-directory-audits
```

The fetcher polls about every five minutes. Wait out that first five minutes, then see what changed in your directory:

```bash
docker exec dfe-clickhouse clickhouse-client --query "
  SELECT _json.activityDisplayName.:String AS activity, _json.result.:String AS result, count() AS rows
  FROM dfe.\`entra-directory-audits\` GROUP BY activity, result ORDER BY rows DESC, activity FORMAT PrettySpace"
```

```text
    activity                     result    rows

1.  Add service principal        success      8
2.  GroupLifecyclePolicies_Get   success      2
3.  Update service principal     success      1
```

That is one row per audit record in the week before the first poll. Each one carries Graph's own fields under `_json`, such as `initiatedBy`, `targetResources` and `activityDateTime`, and `event_id` holds the record's Graph id.

Sign-ins and provisioning are separate feeds, each a source of its own. Swap `entra_directory_audits` for `entra_signins` or `entra_provisioning` in the service name and the schema name, and pick a new source name.

A refused secret or a missing consent shows up in `docker logs dfe-fetcher-entra-directory-audits` with Microsoft's own error message, and that poll's window is tried again on the next one.

## Tear it down

Delete the source and apply, which removes its container. The table and its rows stay until `make clean`. Then take the four lines back out of `env/fetcher.env`.

```bash
dfe --quiet sources delete entra-directory-audits
make apply
```
