# Changelog

Rendered by CI and committed back at the end of a release -- do not edit by
hand. Release notes also appear on the GitHub Releases page, one per tag.

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
