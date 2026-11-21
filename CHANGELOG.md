# Changelog

Rendered by CI and committed back at the end of a release -- do not edit by
hand. Release notes also appear on the GitHub Releases page, one per tag.

## [1.0.12](https://github.com/hyperi-io/dfe-docker/compare/v1.0.11...v1.0.12) (2026-11-21)

### Bug Fixes

* **post:** use dfe-docker for ingest ([fda5b21](https://github.com/hyperi-io/dfe-docker/commit/fda5b2106fc86b2ae8343224d3cf438499c4708f))

## [1.0.11](https://github.com/hyperi-io/dfe-docker/compare/v1.0.10...v1.0.11) (2026-11-21)

### Bug Fixes

* .env.example by component, real defaults ([2626958](https://github.com/hyperi-io/dfe-docker/commit/262695869b1338d2475b80900889cf165adf204c))
* apache log dirs, shared app env, config paths ([218dc1a](https://github.com/hyperi-io/dfe-docker/commit/218dc1ab6223ea137d21b7e4a9dc784a2dfbb928))
* bash probes and stop grace on the templates ([ef35866](https://github.com/hyperi-io/dfe-docker/commit/ef358669d45b5d3286c09d5c3743d0c82a2991c6))
* bring down all compose profiles in e2e stack_down ([1af9b37](https://github.com/hyperi-io/dfe-docker/commit/1af9b37752d8ab93f5ce1df0637be80b27f8237c))
* **broker:** add Redpanda Enterprise licence opt-in (beta) ([3ce1f60](https://github.com/hyperi-io/dfe-docker/commit/3ce1f60f2129da772212a94ec77ea58a910c1e04))
* **broker:** raise the dev free-space floor ([dbcecf3](https://github.com/hyperi-io/dfe-docker/commit/dbcecf3b07293f928d687dabfbe0e04f771d8846))
* **ci:** print runner disk space when a demo writes no rows ([612c346](https://github.com/hyperi-io/dfe-docker/commit/612c34602956edc3308a31145b5f803481c916db))
* **clickhouse:** keep boot merges below 3 GiB ([bc9df9a](https://github.com/hyperi-io/dfe-docker/commit/bc9df9a903a13f1618f1b270aa161d7cdfaebcc3))
* **clickhouse:** reserve free space on the shared disk ([92d1cb6](https://github.com/hyperi-io/dfe-docker/commit/92d1cb6be2ed43c7b485b9cf76adb9d99756f32b))
* compose comment facts into the docs ([2de28d8](https://github.com/hyperi-io/dfe-docker/commit/2de28d8f2a800074d462c7032ea5cef15138b40d))
* compose templates, one-shots and checks ([d44c775](https://github.com/hyperi-io/dfe-docker/commit/d44c77531e34a745ab616041b6b8fae6026d8b15))
* **compose:** mount a volume on the Redpanda config one-shot ([4f2c03f](https://github.com/hyperi-io/dfe-docker/commit/4f2c03fc48605f84f8e9b80954a7ba8fcffe7b24))
* **demo:** accept an empty vessel movers tile in demo-theatre ([46ba4b9](https://github.com/hyperi-io/dfe-docker/commit/46ba4b95a0af2b611fc7a6cdff498637ba5564e0))
* **demo:** group RFO timeline episodes by gap ([708bc57](https://github.com/hyperi-io/dfe-docker/commit/708bc5700ec989817a98dd05efc9b9ea4c67f534))
* **demo:** print logs, broker health and disk use when no rows land ([453a87d](https://github.com/hyperi-io/dfe-docker/commit/453a87d0bd2f4607ec6f24f417c7ee16660de6f6))
* **demo:** read the demo org from a seed file ([bc1d0fd](https://github.com/hyperi-io/dfe-docker/commit/bc1d0fd1b31c40fee5c0efa379b40888fa51d46f))
* **demo:** repeat telco faults every 30 minutes ([952f096](https://github.com/hyperi-io/dfe-docker/commit/952f0968db77abbfc09176df33ba1bfbe94c5461))
* **demo:** replay capture when live rows are low ([47c218b](https://github.com/hyperi-io/dfe-docker/commit/47c218b2483541019144a89d090f52c0a5dd2883))
* **docs:** correct the dfe-fetcher instance count ([253e03f](https://github.com/hyperi-io/dfe-docker/commit/253e03f173d110f26b01ff2f8dc960dbfe1d5339))
* document DFE_STACK_VERSION and the orgs make hunts creates ([4619318](https://github.com/hyperi-io/dfe-docker/commit/4619318e51c8c1d859650689ceaabd1db4e4f22f))
* drive the e2e console at the external origin ([1d9c831](https://github.com/hyperi-io/dfe-docker/commit/1d9c8311ab29ddd30123e0eb4113706b366c7ebe))
* drop the transforms' dead ports and keys ([6ba7737](https://github.com/hyperi-io/dfe-docker/commit/6ba7737bcd79cac94dfcfa804bb639b8142331cb))
* drop unread engine vars, admin name to engine.env ([bc6fab8](https://github.com/hyperi-io/dfe-docker/commit/bc6fab8b03112c7404f0daa4ffb8980197663e51))
* **e2e:** give the engine a schema fixtures dir in e2e posture ([6ccb804](https://github.com/hyperi-io/dfe-docker/commit/6ccb80436df22cc8419cf0396467d650129f115f))
* env files without comments, main's commits in branch style ([ca59c42](https://github.com/hyperi-io/dfe-docker/commit/ca59c42f0c9cab46a0ef2cedf4c0951727e9b6b6))
* expect single-kafka links in default test ([e2f494a](https://github.com/hyperi-io/dfe-docker/commit/e2f494a00b391c44ecc5016c847418d9f4f169b8))
* full source files and fetchers for make demo ([6e8077a](https://github.com/hyperi-io/dfe-docker/commit/6e8077a8ade66df8e6f7522078b1c598296465a0))
* generate the dev and unpublish overlays, move the rest into compose/ ([f288410](https://github.com/hyperi-io/dfe-docker/commit/f2884106dc093e04f89a18d4e0cc939d88d731bf))
* give bearer-tokens-init the engine's literal config path ([91773eb](https://github.com/hyperi-io/dfe-docker/commit/91773eb5e4f8f7a98de8b46d5e1d7230d50f048c))
* give e2e config checks the test's config ([10b71f5](https://github.com/hyperi-io/dfe-docker/commit/10b71f5e72fe3b292cb485476d827c2f154dd0c0))
* **gnmi:** add the opt-in gnmic service ([e940db4](https://github.com/hyperi-io/dfe-docker/commit/e940db45d69bffc3d1f66cae7618c80c02074c2a))
* HyperDX comments to docs, transform env names ([c8426c8](https://github.com/hyperi-io/dfe-docker/commit/c8426c85bddf4ad0782b78a46fadc71cc31e20b4))
* **hyperdx:** keep the DocumentDB TLS directories on renewal ([69d1ef8](https://github.com/hyperi-io/dfe-docker/commit/69d1ef897b6debc14ef357895173699419839e67))
* **hyperdx:** rename the DocumentDB TLS files into place ([c48a0c8](https://github.com/hyperi-io/dfe-docker/commit/c48a0c85374cfc96327de77ecd360108cda7f973))
* Kafbat follows the broker address ([02fd771](https://github.com/hyperi-io/dfe-docker/commit/02fd771f0d53491cd62f94c129d3a9b18f1a7d78))
* keep make demo's re-runs honest and its vendor sources dormant ([8f2f497](https://github.com/hyperi-io/dfe-docker/commit/8f2f49781859bb1ad36ebcb15818c7de1658a70c))
* keep the console key owner-only on a shared checkout ([6c7ba64](https://github.com/hyperi-io/dfe-docker/commit/6c7ba646b092a96982f1dfa853687a111a5ed8e9))
* keep the engine's logins out of the hunt runner ([5062b1c](https://github.com/hyperi-io/dfe-docker/commit/5062b1c0742ce26208a482349d0dde4807204f96))
* keep the receiver configs to transport only ([74f97bf](https://github.com/hyperi-io/dfe-docker/commit/74f97bfb51867b6fef355c0bdfe88f00c665a448))
* leave the Culvert pin out of make stack ([69a79b6](https://github.com/hyperi-io/dfe-docker/commit/69a79b61087909f20f9c681ff4a6f5d3809edfd9))
* let the daemon follow DFE image releases ([f0efb2e](https://github.com/hyperi-io/dfe-docker/commit/f0efb2e0cbd0b5caf52d8307c36cd4e45455071d))
* limit hunt runner to the config it reads ([4add9c2](https://github.com/hyperi-io/dfe-docker/commit/4add9c2c0b2d62838e425991b7a3fa6e569798f9))
* loader and receiver ports, compose comments to docs ([08d96bb](https://github.com/hyperi-io/dfe-docker/commit/08d96bbf4d02727d198bf91ced73a4ff728b82e7))
* merge main, demo Sources stay YAML ([5d26c61](https://github.com/hyperi-io/dfe-docker/commit/5d26c614767d50a4e5be361f0f179d6c86b3c374)), closes [#345](https://github.com/hyperi-io/dfe-docker/issues/345)
* merge main, keep the engine's env names ([6fe1f4a](https://github.com/hyperi-io/dfe-docker/commit/6fe1f4ac852ed7a324528acabd08473f9084619d))
* merge main, make demo on bare python3 ([2f95f3b](https://github.com/hyperi-io/dfe-docker/commit/2f95f3be904a2c8cca0c5a7b1bccb2df06d6fae9))
* mount dfe-engine-config read-only in hunt runner ([5b0c6aa](https://github.com/hyperi-io/dfe-docker/commit/5b0c6aab7a90c1bd408436c3696a9d9bef43c4c9))
* move compose comments to docs ([1c99b18](https://github.com/hyperi-io/dfe-docker/commit/1c99b184dca033b40527b444211582b59a107e6b))
* name the HyperDX DocumentDB login for what it is ([67c28d6](https://github.com/hyperi-io/dfe-docker/commit/67c28d65c6de83b37b044b91db91dfd1a624722a))
* one helper for the source runner's checkouts, tidy the proof ([a74d982](https://github.com/hyperi-io/dfe-docker/commit/a74d982a6c0838bfb63ea80679337caef74cb995))
* **orgs:** seed acme as the deployment's own org ([ef8d704](https://github.com/hyperi-io/dfe-docker/commit/ef8d704b895898bd6c2015ce92ec9f20c9b56687))
* **otel:** give container logs a level ([001bc39](https://github.com/hyperi-io/dfe-docker/commit/001bc39ec08acab056c0407f489fdaf126e71816))
* **otel:** log HyperDX under dfe-hyperdx ([d3fef9a](https://github.com/hyperi-io/dfe-docker/commit/d3fef9a58f86079c96db62909991c3d682d6c3af))
* **otel:** scrape HyperDX's metrics ([0b45964](https://github.com/hyperi-io/dfe-docker/commit/0b45964b67761a10a9c50dfea7fe3f71cb49f672))
* per-component env files and seed targets ([f6efcf3](https://github.com/hyperi-io/dfe-docker/commit/f6efcf34d1f58a132fb0efd75b973e255f5c61f8))
* **post:** send the bearer token with ingest requests ([124f0e2](https://github.com/hyperi-io/dfe-docker/commit/124f0e2f07595bb64f31ecc8ca72964c1f2b4c99))
* **publish:** set the engine's password rotation command ([aa2ec77](https://github.com/hyperi-io/dfe-docker/commit/aa2ec775026314a8ef0c7eaf6893e16d9b76dbde)), closes [#1230](https://github.com/hyperi-io/dfe-docker/issues/1230)
* read the e2e poll interval from one constant, its test included ([4771e64](https://github.com/hyperi-io/dfe-docker/commit/4771e649bedf0ac690955d5d3a23cebbd8efe70b))
* read the engine's DFE_ENGINE_ names ([a6ee674](https://github.com/hyperi-io/dfe-docker/commit/a6ee67426e492ee8e1763ca80c73f8bc87a037a5))
* regenerate the FortiGate demo sample from its generator, lint the generator ([14e2bba](https://github.com/hyperi-io/dfe-docker/commit/14e2bba76695a1b76f5daacb1a82a041e9877128))
* replace acme seed org with default ([3dc7c26](https://github.com/hyperi-io/dfe-docker/commit/3dc7c26d4b506413362ec21fc1584f9cbbf0c347))
* say the DocumentDB pin defaults in compose ([8f1260d](https://github.com/hyperi-io/dfe-docker/commit/8f1260d1c4930c3ca9c8d0b751500f5c035970cc))
* say the transforms push to the loader on gRPC ([3bd7b44](https://github.com/hyperi-io/dfe-docker/commit/3bd7b4411a7c16574e93c5b9f467351456b27e2b))
* **seed:** set poll intervals on the AWS seeds ([93d5bf3](https://github.com/hyperi-io/dfe-docker/commit/93d5bf3778f3fe009028397331134706b7728714))
* set single-kafka as the active profile ([ac65f2b](https://github.com/hyperi-io/dfe-docker/commit/ac65f2b00fe8fe198a94c8222d91e4b8b5a23dab))
* set the archiver's DFE_ARCHIVER_ env names ([1579f98](https://github.com/hyperi-io/dfe-docker/commit/1579f98105668b91ce6dee34f2de0e2524a7cfd5))
* share the compose helpers and tidy the cores rollup ([1f62e9b](https://github.com/hyperi-io/dfe-docker/commit/1f62e9bcdae944d91e811abc03edf4b7e39f8e47))
* simplify the self-update daemon, tick hourly ([3297356](https://github.com/hyperi-io/dfe-docker/commit/3297356ed6ed9ffda5d94f8f6ef7b0054dda943c))
* **source:** pass dfe-transform-vrl only for the filebeat case ([2c12cca](https://github.com/hyperi-io/dfe-docker/commit/2c12cca58d317a2544597c86fbef5719f289a90b)), closes [#1187](https://github.com/hyperi-io/dfe-docker/issues/1187)
* **tests:** wait for outage load request counts ([8d8fc16](https://github.com/hyperi-io/dfe-docker/commit/8d8fc16bf2bf9fd36cbada1220c26a493fa653b3))
* trim env templates to keys in use ([0fe5659](https://github.com/hyperi-io/dfe-docker/commit/0fe5659a3555640f6df9a6b862f2b6b194423979))
* ui, proxy and collector comments to docs ([b650e04](https://github.com/hyperi-io/dfe-docker/commit/b650e04285addb1995359d67d604ba3023696fcc))
* write no inline comments into .env pins ([c9c4617](https://github.com/hyperi-io/dfe-docker/commit/c9c46173fa025560dddc23b4e89bdb785ff8ef34))

## [1.0.10](https://github.com/hyperi-io/dfe-docker/compare/v1.0.9...v1.0.10) (2026-11-20)

### Bug Fixes

* **broker:** turn off Redpanda's trial Enterprise features ([a3ae247](https://github.com/hyperi-io/dfe-docker/commit/a3ae2476bbb19bb806cbee6219028197a279a70b))
* **demo:** replay a captured Feodo Tracker poll ([3d1956c](https://github.com/hyperi-io/dfe-docker/commit/3d1956c2d2f1ecbb4a55b38f33cbdadc38b8920d))
* **post:** show how to turn off a broker Enterprise feature ([31d6c4c](https://github.com/hyperi-io/dfe-docker/commit/31d6c4c8146298e0d4b83356392c0d23d700a00d))

## [1.0.9](https://github.com/hyperi-io/dfe-docker/compare/v1.0.8...v1.0.9) (2026-11-17)

### Bug Fixes

* **admin:** drop the Search admin link ([dff6695](https://github.com/hyperi-io/dfe-docker/commit/dff6695861ca65ef177705b1647beeaecdd840bc))
* **clickhouse:** keep merges from taking the memory inserts need ([#414](https://github.com/hyperi-io/dfe-docker/issues/414)) ([d4d517a](https://github.com/hyperi-io/dfe-docker/commit/d4d517a7e2d1e00b76dadc690ac1b1fc0928ceb9))
* **demo:** accept empty churn tiles in demo-theatre ([#412](https://github.com/hyperi-io/dfe-docker/issues/412)) ([813bd36](https://github.com/hyperi-io/dfe-docker/commit/813bd363eada73f0b029eb60fa80adfd0e1e5f36)), closes [#411](https://github.com/hyperi-io/dfe-docker/issues/411)
* **demo:** fix telco BGP paths and console search checks ([#413](https://github.com/hyperi-io/dfe-docker/issues/413)) ([7a5507b](https://github.com/hyperi-io/dfe-docker/commit/7a5507bc4a10d25859a9bdf39d3d8f217c6ea8e3))
* **demo:** replay a feed that answers with no rows ([#417](https://github.com/hyperi-io/dfe-docker/issues/417)) ([c24d515](https://github.com/hyperi-io/dfe-docker/commit/c24d515f8119ee437a1ed4c9888776e804ce8631))
* **demo:** take Spamhaus DROP out of demo-cyber ([#416](https://github.com/hyperi-io/dfe-docker/issues/416)) ([247d7db](https://github.com/hyperi-io/dfe-docker/commit/247d7db92004ec5dae7ffb9b7cf85b4dc7e669bc))
* **demo:** verify demo pack results in CI ([#411](https://github.com/hyperi-io/dfe-docker/issues/411)) ([56aa0dc](https://github.com/hyperi-io/dfe-docker/commit/56aa0dc3fd3f266ee27868a18dba50263da1eee1))
* **demo:** wait up to 480 s for the archived tile to read non-zero ([3b518b0](https://github.com/hyperi-io/dfe-docker/commit/3b518b08041760b435ad4d179bd0cc86491cf44c))
* **update:** remove previous stack images after a nightly update ([#406](https://github.com/hyperi-io/dfe-docker/issues/406)) ([87a9005](https://github.com/hyperi-io/dfe-docker/commit/87a9005a7131f977aa8c3760f4b29947d953440c))

## [1.0.8](https://github.com/hyperi-io/dfe-docker/compare/v1.0.7...v1.0.8) (2026-11-16)

### Bug Fixes

* **compose:** mint the admin login and print it with one command ([#396](https://github.com/hyperi-io/dfe-docker/issues/396)) ([e6563d8](https://github.com/hyperi-io/dfe-docker/commit/e6563d868a7e820a55f5b0792bf7706fcda46289))
* **compose:** pass the engine the images the stack runs ([#402](https://github.com/hyperi-io/dfe-docker/issues/402)) ([2c5cc48](https://github.com/hyperi-io/dfe-docker/commit/2c5cc48b5d3dd4e022f533b30c0c0d54503ae67b))
* **compose:** publish syslog and flow listeners through port keys ([#400](https://github.com/hyperi-io/dfe-docker/issues/400)) ([0339b29](https://github.com/hyperi-io/dfe-docker/commit/0339b294b7e57320739cefbb48f158cf757e4035))
* **compose:** TLS ingest via dfe-proxy and fetcher input mount ([#401](https://github.com/hyperi-io/dfe-docker/issues/401)) ([cd82ccf](https://github.com/hyperi-io/dfe-docker/commit/cd82ccfc9909e87d2fbb12d2cf39d6b5411fc400))
* **make:** block ci while a demo pack is streaming ([#394](https://github.com/hyperi-io/dfe-docker/issues/394)) ([43d1659](https://github.com/hyperi-io/dfe-docker/commit/43d165951b0d05a57f7f35dd14c58faa3e86fa30))
* **post:** check dfe-ui's metrics and correct stale comments ([#404](https://github.com/hyperi-io/dfe-docker/issues/404)) ([e4464ea](https://github.com/hyperi-io/dfe-docker/commit/e4464eaa263e9b238e72c605ed53dde9e1e9f4d8)), closes [#1099](https://github.com/hyperi-io/dfe-docker/issues/1099)
* **secrets:** mint 32 and 64 character alphanumeric secrets ([#395](https://github.com/hyperi-io/dfe-docker/issues/395)) ([b82f579](https://github.com/hyperi-io/dfe-docker/commit/b82f579dce19597e8be05a1ca014a904f4f313da))
* **stack:** refuse a prerelease stack pin on a release ([#391](https://github.com/hyperi-io/dfe-docker/issues/391)) ([20f139a](https://github.com/hyperi-io/dfe-docker/commit/20f139a315ebecc1c6fd80fbb0b0556b62d578c7))

## [1.0.7](https://github.com/hyperi-io/dfe-docker/compare/v1.0.6...v1.0.7) (2026-11-14)

### Bug Fixes

* **stack:** pin the stack dial at 2.2.2 ([#377](https://github.com/hyperi-io/dfe-docker/issues/377)) ([c7aac00](https://github.com/hyperi-io/dfe-docker/commit/c7aac00918de5c368093105ea8b4361ed063acfd))

## [1.0.6](https://github.com/hyperi-io/dfe-docker/compare/v1.0.5...v1.0.6) (2026-11-13)

### Bug Fixes

* **demo:** add the cyber demo pack's sources, events and console ([#384](https://github.com/hyperi-io/dfe-docker/issues/384)) ([0eae396](https://github.com/hyperi-io/dfe-docker/commit/0eae396bd8955bb16891fa7cb2ef3d39c344e368))
* **demo:** add the telco demo pack ([#389](https://github.com/hyperi-io/dfe-docker/issues/389)) ([7a45cd3](https://github.com/hyperi-io/dfe-docker/commit/7a45cd37440652cc2eed4bb499e6f1db6659c8fc))
* **demo:** add the theatre and battlespace demo pack ([#386](https://github.com/hyperi-io/dfe-docker/issues/386)) ([863c6bc](https://github.com/hyperi-io/dfe-docker/commit/863c6bcd09c32dc6fb7cbec89f07d4486741aa08))
* **demo:** publish one compose file per demo pack ([#383](https://github.com/hyperi-io/dfe-docker/issues/383)) ([e729085](https://github.com/hyperi-io/dfe-docker/commit/e7290857ade15242d1174d26d07fb6387ffc3bbe))
* **demo:** replay a pack's Beats sources and host their transforms ([#385](https://github.com/hyperi-io/dfe-docker/issues/385)) ([be05917](https://github.com/hyperi-io/dfe-docker/commit/be059173adff716d18d35bef7516cdad149d359c))
* **otel:** log HyperDX under dfe-hyperdx ([#371](https://github.com/hyperi-io/dfe-docker/issues/371)) ([b28bdc6](https://github.com/hyperi-io/dfe-docker/commit/b28bdc642f3273c00636fb3f260fd1d8c002c584))

## [1.0.5](https://github.com/hyperi-io/dfe-docker/compare/v1.0.4...v1.0.5) (2026-11-12)

### Bug Fixes

* **compose:** read alert destinations from the engine's secrets backend ([a291819](https://github.com/hyperi-io/dfe-docker/commit/a2918192afa1957bbab3b1ddd57ee319a4da991e))
* **compose:** run one fetcher for every fetcher source ([cf2e8d3](https://github.com/hyperi-io/dfe-docker/commit/cf2e8d35ab9f493c59c572ed5f23b0045a0b322e)), closes [hyperi-io/dfe-fetcher#289](https://github.com/hyperi-io/dfe-fetcher/issues/289)
* **demo:** complete the engine's first-run setup ([ee86c18](https://github.com/hyperi-io/dfe-docker/commit/ee86c18073fca5e8f01c14287d86c5e4596e23e6))
* **install:** publish one-line compose installs ([40169b4](https://github.com/hyperi-io/dfe-docker/commit/40169b43430fcdcca6ca7dbe63d5505724a4dd7a))
* **renovate:** keep hyperi-ci references on main ([8f78312](https://github.com/hyperi-io/dfe-docker/commit/8f783127da1afa5faa113e3f8e760527f1e10bac))
* **stack:** pin stack to 2.2.2 ([52f72ca](https://github.com/hyperi-io/dfe-docker/commit/52f72ca3573675a26480fd51e8af3d615b2f3c60))
* **stack:** set stack version to 2.2.2-rc.4 ([2b9f2e5](https://github.com/hyperi-io/dfe-docker/commit/2b9f2e57a6a90d270e99b0c805fb5bd1d5d1126d)), closes [dfe-infra#116](https://github.com/hyperi-io/dfe-infra/issues/116)

## [1.0.4](https://github.com/hyperi-io/dfe-docker/compare/v1.0.3...v1.0.4) (2026-11-10)

### Bug Fixes

* **compose:** name the HyperDX service dfe-hyperdx ([47f2a20](https://github.com/hyperi-io/dfe-docker/commit/47f2a205708180e46acf87c634f7b64240989eaa))
* **docs:** name the search console dfe-hyperdx ([139024c](https://github.com/hyperi-io/dfe-docker/commit/139024c78a4459ee03f628cff0f5401da8dd5a2f))
