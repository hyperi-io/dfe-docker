<!--
Project:   dfe-docker
File:      docs/live-console.md
Purpose:   Running dfe-ui as next dev over a checkout, inside a dev stack
Language:  Markdown

License:   BUSL-1.1
Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Console edits without a rebuild

```bash
make dev LIVE_UI=1   # dfe-ui runs `next dev` over $DFE_SRC_ROOT/dfe-ui
```

`LIVE_UI=1` layers `compose/live-ui.yml`, which runs dfe-ui as `next dev` over your checkout instead of building its image. It still sits behind dfe-proxy on `DFE_UI_PORT`, so login, the engine API and the dfe-hyperdx embed work as they do on any stack. A saved edit reaches the browser through hot reload, which rides the websocket upgrade the proxy allows. `docker compose logs -f dfe-ui` shows the dev server's output.

The first start installs dependencies into the checkout, a yarn cache under its gitignored `.yarn/` and later starts install from that cache in seconds. Each route compiles on its first hit. A background request warms the login page as the dev server starts, so a first visit after that compile stays inside dfe-proxy's 15s route timeout.


Next.js only accepts hot-reload connections from `localhost` or the host it serves on. The console does not finish loading without one. Live mode serves on the hostname `DFE_EXTERNAL_ORIGIN` names, so the console works there and on `localhost`. An origin that is an IP address cannot be served on by name, so browse that stack by hostname or on `localhost` instead.

`make apply SERVICES=dfe-ui DEV=1 LIVE_UI=1` recreates a running dev stack's console in live mode. Like every `make apply`, it removes the containers of deleted sources and brings the collector up to date. A stack started with `LOCAL` takes the same `LOCAL` with dfe-ui left out. The same command without `LIVE_UI` and with the stack's own `LOCAL` runs `dfe-ui:local` again, which exists only if an earlier `make dev` built it.

## Live mode needs Linux and a trusted network

The dev server answers its source-map and stack-frame endpoints without a login, so with `DFE_BIND_SCOPE=all` anyone who can reach the console port can read the console's source. Use live mode on a network you trust. `LIVE_UI` only applies to `make dev` and `make apply DEV=1`, never to `make ci` or `make up`.

The container runs as your user and writes `node_modules` and `.next` into the checkout, so stop any `next dev` you run there yourself first. Keys the stack does not set are filled from the checkout's `apps/dfe-core-ui` env files (`.env`, `.env.local` and `.env.development*`). The stack sets `NEXT_PUBLIC_API_URL` empty, as the production image does, so browser calls stay on the proxy's origin. `next dev` compiles routes in-process, so dfe-ui gets a 4G memory limit in place of the service tier's. `LOCAL` must not name dfe-ui alongside it.

Live mode needs a Linux host: on macOS the container's install would swap the native modules your host's `node_modules` relies on. On a Mac, run `next dev` on the host against the stack's engine instead, on port 5173 since dfe-proxy holds 3000, as dfe-ui's [local development guide](https://github.com/hyperi-io/dfe-ui/blob/main/docs/local-development.md) describes.

The rest of the dev loop is in [developing.md](developing.md).
