<!--
Project:   dfe-docker
File:      docs/auth-profile.md
Purpose:   The opt-in auth profile: an OIDC gate in front of the infra UIs
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Gating the infra UIs with OIDC

Status: opt-in and off by default (`DFE_AUTH_ENABLED=false`). `make check-compose` checks its wiring and no e2e test signs in through it.

`DFE_AUTH_ENABLED=true` arms the `auth` profile, which puts an oauth2-proxy in front of each infra UI. Reaching Kafbat or dfe-hyperdx on `:8090` and `:8000`, then needs an OIDC sign-in **and** membership of one of `DFE_OIDC_ALLOWED_GROUPS` (`dfe-infra`, `dfe-admin` by default). The group check is the gate - authentication alone is not, which is the same rule the Kubernetes tier applies at the Envoy edge.

The embed on `:8091` stays outside that gate, as the Kubernetes tier's dfe-hyperdx route does. An OIDC redirect cannot complete inside the console's frame and dfe-hyperdx authenticates each caller itself from the console's `dfe_token`. So any console user reaches dfe-hyperdx through the embed, group or not.

Three proxies, one per origin, because oauth2-proxy serves a single listener and dfe-hyperdx is one UI across two origins:

| Host port | Proxy | Upstream |
|---|---|---|
| 8081 | oauth2-proxy-kafbat | kafka-ui |
| 8090 | oauth2-proxy-hyperdx | dfe-hyperdx app. Gates page access only, as dfe-hyperdx reads the engine token itself |
| 8000 | oauth2-proxy-hyperdx-api | dfe-hyperdx API, an operator surface. `OAUTH2_PROXY_PASS_HOST_HEADER` forwards the request's Host header to dfe-hyperdx |

They take those ports over and the UIs stop publishing their own, so nothing moves for anyone using them. All three share one cookie secret and cookies are not port-scoped, so a single sign-in covers the set. Register all three redirect URIs (`<origin>:<port>/oauth2/callback`) with the IdP and make sure it emits a groups claim or every sign-in is refused.

The infra kill switch still wins: `DFE_INFRA_UIS_EXTERNAL=false` unpublishes the proxies as well as the UIs.

## Three limits

It gates page access only. Neither UI learns from the proxy who signed in, because dfe-hyperdx identifies callers by the engine token and neither gets roles or an audit trail from it.

An expired session on the API origin fails opaquely. The browser blocks a cross-origin XHR that gets a redirect to the IdP, so the app shows failed requests rather than a login prompt. Reload the app page to sign back in.

Nothing else moves behind it. Ingest, ClickHouse, Kafka, the metrics ports and the product UIs are untouched.

It must stay possible to run every other profile without an issuer. Arming it with a setting missing stops `make` and names the key rather than starting a proxy that redirects nowhere.

Every variable the profile reads is in [configuration.md](configuration.md#infra-ui-authentication-opt-in). The access model it sits in is in [operating.md](operating.md).
