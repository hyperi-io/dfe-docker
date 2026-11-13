# Changelog

Rendered by CI and committed back at the end of a release -- do not edit by
hand. Release notes also appear on the GitHub Releases page, one per tag.

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
