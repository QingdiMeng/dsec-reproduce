# Documentation

Start with the deployment quickstart, then choose an application. Reports
record the tested revision and configuration; they are not installation guides.

## Guides

| Document | Purpose |
| --- | --- |
| [Quickstart](guides/QUICKSTART.md) | Install, provision a smoke guest and validate an isolated instance |
| [Use cases](guides/USE_CASES.md) | Application entry points, dependencies and current gaps |
| [Host configuration](guides/DSEC_HOST_CONFIGURATION.md) | Configure paths, runtime identity, storage and budgets |
| [Service deployment](guides/SERVICE_DEPLOYMENT.md) | Deploy and administer the daemon and rollout worker |
| [Artifact publication](guides/ARTIFACT_PUBLICATION.md) | Prepare and publish immutable environment artifacts |
| [RL integration](guides/RL_FRAMEWORK_ADAPTERS.md) | Trainer responsibilities, Miles integration and historical GPU experiments |
| [TB2.1 application](../apps/tb21/README.md) | Optional task staging, image preparation and registration |
| [MBPP + verl training case](../apps/mbpp/README.md) | Optional Python environment, Qwen3.5-2B GRPO, full-epoch training and before/after evaluation |

## Architecture and contracts

| Document | Purpose |
| --- | --- |
| [Agent environment](architecture/AGENT_ENVIRONMENT_CONTRACT.md) | Task lifecycle, scoring boundary, durable actions and recovery |
| [Sandbox daemon](architecture/SANDBOX_DAEMON.md) | Durable request handling and sandbox control plane |
| [Comparison protocol](architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md) | Matched workloads, cache conditions and complete backend accounting |

## Acceptance and historical reports

These records may contain experiment-host paths and references to private
raw evidence. Files explicitly marked as historical sources are not shipped.
Current operational instructions are in the guides above.

| Document | Scope |
| --- | --- |
| [Installation acceptance](reports/DSEC_V01_INSTALL_ACCEPTANCE.md) | Installed runtime and fresh virtual-environment checks |
| [Latest GRPO acceptance](reports/DSEC_V01_GRPO_ACCEPTANCE.md) | Short real model training, snapshots, forks and cleanup |
| [Integrity and cost report](reports/DSEC_VERITY_PILOT_REPORT.md) | Protected verifier artifacts and complete backend comparison |
| [MBPP + verl case report](reports/MBPP_VERL_FIRST_USE.md) | First-use issues, execution concurrency, 187 GRPO updates and 500-task before/after results |
| [Current roadmap](../ROADMAP.md) | Implementation boundaries and remaining work |

## Project

- [Roadmap and paper alignment](../ROADMAP.md)
- [Contribution guide](../CONTRIBUTING.md)
- [License](../LICENSE) and [third-party notices](../THIRD_PARTY_NOTICES.md)
