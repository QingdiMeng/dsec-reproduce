# Documentation

**English** · [简体中文](README.zh-CN.md)

Start with API access in the quickstart, then choose an application. Operators
use the HA cluster and host setup guides; direct sockets are development/internal
interfaces. RL recipes use the rollout API; historical scores retain their earlier
configuration. Reports
record the tested revision and configuration; they are not installation guides.

## Guides

| Document | Purpose |
| --- | --- |
| [Quickstart](guides/QUICKSTART.md) | Install, provision a smoke guest and validate an isolated instance |
| [Use cases](guides/USE_CASES.md) | Application entry points, dependencies and current gaps |
| [Host configuration](guides/DSEC_HOST_CONFIGURATION.md) | Configure paths, runtime identity, storage and budgets |
| [HA cluster preview](guides/CLUSTER_DEPLOYMENT.md) | IAM, multiple API replicas, placement, watchers and authenticated Nodes |
| [Cluster management console](guides/CLUSTER_CONSOLE.md) | Inspect authorized inventories and recover requests through the same-origin UI |
| [Service deployment](guides/SERVICE_DEPLOYMENT.en.md) | Deploy and administer the daemon and rollout worker |
| [Artifact publication](guides/ARTIFACT_PUBLICATION.en.md) | Prepare and publish immutable environment artifacts |
| [RL integration](guides/RL_FRAMEWORK_ADAPTERS.en.md) | Trainer responsibilities, Miles integration and historical GPU experiments |
| [TB2.1 application](../apps/tb21/README.md) | Optional task staging, image preparation and registration |
| [MBPP + verl training case](../apps/mbpp/README.md) | Optional Python environment, Qwen3.5-2B GRPO, full-epoch training and before/after evaluation |
| [SWE-Gym execution case](../apps/swegym/README.md) | Score pinned repository tasks with the official verifier through the API. A single-task SAO pilot exercises the training lifecycle. |
| [OSWorld desktop case](../apps/osworld/README.md) | Replay desktop actions on an Ubuntu FullVM and score the results with the upstream metric |
| [OpenHands runtime adapter](../integrations/openhands/README.md) | Optional upstream agent integration, reusable coding conformance and explicit capability/ownership boundaries |

## Architecture and contracts

| Document | Purpose |
| --- | --- |
| [Modular refactor design](architecture/MODULAR_REFACTOR_DESIGN.en.md) | Approved boundaries, paper alignment, migration progress and acceptance gates |
| [Agent environment and generality](architecture/AGENT_ENVIRONMENT_CONTRACT.md) | Workload-to-system design method, capability admission, optional task lifecycle, durable actions and recovery |
| [HA control design](architecture/CLUSTER_CONTROL_DESIGN.md) | Ownership, failure boundaries and acceptance evidence |
| [Sandbox daemon](architecture/SANDBOX_DAEMON.en.md) | Durable request handling and sandbox control plane |
| [Native execution and production isolation](architecture/EXECUTION_ISOLATION.md) | Review aether and chronus responsibilities, compatibility and QEMU confinement checks |
| [Comparison protocol](architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.en.md) | Matched workloads, cache conditions and complete backend accounting |

## Acceptance and historical reports

These records may contain experiment-host paths and references to private
raw evidence. Files explicitly marked as historical sources are not shipped.
Current operational instructions are in the guides above.

| Document | Scope |
| --- | --- |
| [Installation acceptance](reports/DSEC_V01_INSTALL_ACCEPTANCE.en.md) | Installed runtime and fresh virtual-environment checks |
| [Latest GRPO acceptance](reports/DSEC_V01_GRPO_ACCEPTANCE.en.md) | Short real model training, snapshots, forks and cleanup |
| [Integrity and cost report](reports/DSEC_VERITY_PILOT_REPORT.en.md) | Protected verifier artifacts and complete backend comparison |
| [MBPP + verl case report](reports/MBPP_VERL_FIRST_USE.md) | First-use issues, execution concurrency, 187 GRPO updates and 500-task before/after results |
| [SWE-Gym case report](reports/SWEGYM_FIRST_USE.md) | First-use configuration, runs and scoring evidence |
| [Current roadmap](../ROADMAP.md) | Implementation boundaries and remaining work |

## Project

- [Current capabilities and task register](PROJECT_STATUS.en.md): implementation, scoped evidence, current work and candidate increments; generated from one register.
- [Roadmap and paper alignment](../ROADMAP.md)
- [Contribution guide](../CONTRIBUTING.md)
- [License](../LICENSE) and [third-party notices](../THIRD_PARTY_NOTICES.md)
