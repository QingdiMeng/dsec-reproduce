# DSec Reproduce

An independent implementation of elastic sandboxes for agent execution and RL,
inspired by the [DSec paper](https://arxiv.org/abs/2609.22978).

**Status: v0.1 development preview for a trusted user on one Linux host.**
This is not DeepSeek's original implementation. It is not yet a production
multi-tenant platform, and current measurements do not establish a general
performance or memory advantage over Docker.

[Quickstart](docs/guides/QUICKSTART.md) ·
[Use cases](docs/guides/USE_CASES.md) ·
[Documentation](docs/README.md) ·
[Roadmap](ROADMAP.md) ·
[Development release](https://github.com/QingdiMeng/dsec-reproduce/releases/tag/v0.1.0-dev.0)

## What it provides

- **Sandbox lifecycle:** Firecracker microVM execution, pause/snapshot/restore,
  durable requests and prepared-state forks with private writable state.
- **Storage:** EROFS layers, OverlayBD/ublk writable disks, explicit local or
  3FS-backed environment catalogs, and optional DAX configurations.
- **Scheduling:** resource budgets, admission checks, queued work, leases,
  wait reasons and resource monitoring on one host.
- **Agent integration:** framework-independent `reset`, `step`, `evaluate`
  and `stop` boundaries, Miles adapters and an optional native verl MBPP case.

OpenEnv is not a required service. A task plugin supplies the task instruction,
environment and verifier; the trainer owns model sampling, tokens, logprobs
and the RL algorithm.

Core implementations are organized under [src/dsec](src/dsec/). Existing
top-level imports and CLI names remain compatibility entry points. The
[refactor design](docs/architecture/MODULAR_REFACTOR_DESIGN.md) distinguishes
completed module migration from remaining runtime and application boundaries.

## Quick start

Install the Python control plane on Linux with Python 3.11 or newer:

```sh
git clone https://github.com/QingdiMeng/dsec-reproduce.git
cd dsec-reproduce
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/dsec-host --help
```

Installation alone does not provision a working microVM host.
Running sandboxes requires KVM access, a compatible Firecracker binary, guest
kernel/root filesystem, and the storage/network helpers selected by the catalog.

Follow the [deployment quickstart](docs/guides/QUICKSTART.md) to build a smoke
guest, configure an isolated instance, start services and run the non-TB task
acceptance. Configure networking and permissions once before launching jobs.

The core wheel has no third-party Python runtime dependencies. It does not
bundle tasks, model weights, guest images, kernels, 3FS, storage binaries or
experimental GPU training patches.

## Use cases

| Application | Available today | Start here |
| --- | --- | --- |
| Custom agent tasks | Task protocol and a counter smoke task | [Task contract](docs/architecture/AGENT_ENVIRONMENT_CONTRACT.md) |
| TB2.1 execution and scoring | Optional application package, task preparation and representative official-verifier acceptance | [TB2.1 application](apps/tb21/README.md) |
| API-driven agents | Environment/task interfaces; bring a model client and agent loop | [Use cases](docs/guides/USE_CASES.md) |
| Miles GRPO | Agent/generate/reward adapters and short real training validation; portable training recipe is being developed | [RL integration](docs/guides/RL_FRAMEWORK_ADAPTERS.md) |
| MBPP + verl GRPO | Qwen3.5-2B, one complete epoch, eight responses per task and full before/after held-out evaluation | [MBPP training case](apps/mbpp/README.md) |
| Repeated episodes | Prepared-state fork API and isolation/recovery acceptance tool | [Use cases](docs/guides/USE_CASES.md) |

Installing the core does not download TB2.1. Install its application only when
needed with `.venv/bin/python -m pip install ./apps/tb21`.

## Validation and limits

The modular refactor passed installed Linux regression (322 tests passed, one
expected release-boundary skip), old-to-new VM/container adoption, restart
recovery, shared admission, representative TB2.1 verification, four-step native
MBPP/verl GRPO and two-VM reads from a real 3FS-backed EROFS layer. See the
[R3 acceptance](docs/reports/DSEC_V01_INSTALL_ACCEPTANCE.md). This reused the
existing Linux host; a second clean-host installation remains open.

The installed v0.1 candidate passed selected Linux regressions, representative
TB2.1 scoring, short Qwen3.5-4B GRPO, real microVM restore and prepared-state
isolation checks. The optional MBPP/verl case completed 187 GRPO updates and
evaluated all 500 original test tasks with eight responses each before and
after training: mean sample success rose from 39.775% to 42.275%, and tasks
solved at least once in eight responses rose from 64.8% to 68.4%. See the
[case report](docs/reports/MBPP_VERL_FIRST_USE.md) for scoring, evaluation recovery
and sampling limits. These are bounded acceptance results: all 89 TB2.1 tasks
have not passed model episodes; multiple hosts, stateful multi-turn verl and
Uni-Agent have not been validated.

See the [latest GRPO acceptance](docs/reports/DSEC_V01_GRPO_ACCEPTANCE.md),
[installation acceptance](docs/reports/DSEC_V01_INSTALL_ACCEPTANCE.md) and
[complete-cost report](docs/reports/DSEC_VERITY_PILOT_REPORT.md). CI checks
packages and selected regressions; it does not provision a real GPU/KVM host.

## Documentation and development

The [documentation index](docs/README.md) separates setup guides, architecture
contracts and historical acceptance reports. The [roadmap](ROADMAP.md) records
paper alignment and future work with explicit acceptance gates.

Read [CONTRIBUTING.md](CONTRIBUTING.md) for package checks and contribution
requirements. Runtime modules remain at the repository root to preserve their
installed import paths; task/trainer plugins are in `dsec_adapters/`, optional
applications in `apps/`, and build/acceptance tools in `tools/`.

## License

Project code and documentation use [MIT](LICENSE). The Miles-derived agent loop
retains Apache-2.0. See [third-party notices](THIRD_PARTY_NOTICES.md) for source
attribution and external component licenses.
