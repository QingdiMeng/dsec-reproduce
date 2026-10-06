# DSec reproduction: elastic sandbox control plane

This repository is an experimental, independent reproduction of DSec's elastic
sandbox ideas. The Python control plane provides a libdsec-style client,
long-lived rollout worker, resource-aware scheduler, artifact catalog and
publisher, and Firecracker microVM lifecycle with durable request handling.
It also contains Docker and Terminal-Bench adapters for comparison. It is not
the original DSec implementation.

The v0.1 development wheel contains the runtime modules listed in
`pyproject.toml` and the `dsec_adapters` task/trainer plugins. It does **not** contain benchmark tasks,
downloaded upstream repositories, VM images, kernels, 3FS, OverlayBD/ublk
binaries, experiment results, or a preconfigured host. TB2 adapters are present
for compatibility but the core environment ID and rollout interfaces are not
specific to TB2.

The installed TB2 verifier uses the packaged plugin. Legacy experiment imports
are compatibility aliases; running the installed services does not require
the experiment directory or `PYTHONPATH`.

The sandbox and rollout worker can serve different RL trainers. Miles has an
experimental adapter with short real training and GRPO reuse validation; verl and Uni-Agent
need their own trajectory and reward adapters and have not yet been validated.
See [RL framework adapters](RL_FRAMEWORK_ADAPTERS.md).

`agent_environment.py` defines the framework-independent episode boundary:
`reset`, one durable `step`, `evaluate`, and `stop`. A task adapter supplies the
instruction, work directory, sandbox profile, and verifier interpretation;
the trainer keeps ownership of prompts, token IDs, logprobs, and sampling.
The TB2 and non-TB counter adapters live in `dsec_adapters`. OpenEnv is not a
required service or core dependency. E2B-compatible sandbox APIs can be added below this episode
boundary without changing a task adapter's scoring contract. See the
[agent environment contract](AGENT_ENVIRONMENT_CONTRACT.md) for the lifecycle,
recovery rules, and separation from the sandbox SDK.

## Use cases

See [use cases and runnable entry points](USE_CASES.md) for TB2.1 task
execution, API-driven agents, Miles RL, prepared-state episode reuse and custom
non-TB tasks. Each case identifies shipped commands, external dependencies,
validation evidence and missing application launchers. Start with the non-TB
smoke, then the optional [TB2.1 application](apps/tb21/README.md).

## Roadmap and paper alignment

The [project roadmap](ROADMAP.md) records current paper alignment, remaining
gaps, stable work IDs and acceptance gates. The planned order is a reproducible
v0.1 release, unified storage and measured reuse, complete elastic rollout
execution, then production isolation and distributed scale. These are future
milestones; existing single-host experiments do not establish production or
paper-scale capability. The [v0.1 release checklist](DSEC_V01_RELEASE_PLAN.md)
tracks the current delivery separately.

See [contribution instructions](CONTRIBUTING.md) for issue reports, package
checks and the distinction between CI regressions and real Linux acceptance.

## Install the control plane

For a new host, follow [the deployment quickstart](QUICKSTART.md), including a
fresh non-TB smoke guest build. The [TB2.1 application](apps/tb21/README.md) has
a separately installed `dsec-tb21-case` package for explicit task staging,
file checks, image preparation and environment registration. Core installation
does not install that application or download its tasks and images.

On Linux with Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/dsec-sandboxd --help
.venv/bin/dsec-rollout-worker --help
.venv/bin/dsec-host --help
```

Running a microVM also requires a Firecracker binary, a compatible guest
kernel and root filesystem, KVM access, and the storage/network helpers chosen
by the environment catalog. Configure their paths explicitly:

```sh
.venv/bin/dsec-host --config host.json init --instance local \
  --state-root "$HOME/dsec-state" --network-interface eth0 \
  --binary /opt/dsec/firecracker --kernel /opt/dsec/vmlinux \
  --template /opt/dsec/guest.ext4
.venv/bin/dsec-host --config host.json doctor
.venv/bin/dsec-host --config host.json run sandbox --validate-only
.venv/bin/dsec-host --config host.json render --out rendered-units
systemd-analyze --user verify rendered-units/*.service
mkdir -p "$HOME/.config/systemd/user"
# Inspect the rendered units before installing them; use an unused instance name.
cp rendered-units/dsec-local-*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start dsec-local-sandbox.service dsec-local-worker.service
.venv/bin/dsec-host --config host.json wait
.venv/bin/dsec-host --config host.json doctor --live
.venv/bin/dsec-host --config host.json smoke --out acceptance.json
.venv/bin/dsec-host --config host.json status
```

Use the actual host network interface. `init` writes a private configuration and
refuses to replace an existing file. All configured relative paths resolve from
the configuration directory. Its initial budgets are explicit small-example
settings; adjust them to the host and environment catalog. Installed services
use Python isolated mode to ignore source-tree imports. The default `service_group`
is `kvm`, so the runtime user must already be a member; the launcher refreshes
that existing group membership without changing device permissions.

An isolated acceptance instance can additionally run
`smoke --restart-services --out restart-acceptance.json`. This restarts only the
two user services derived from its instance name and checks live VMM identity,
committed-action deduplication, paused recovery, scoring and lease cleanup.
Stopping the sandbox service preserves VMMs for recovery: stop owned sandboxes
through the SDK before retiring the instance.

The core wheel has no third-party Python runtime dependencies. The Miles plugin
uses the `miles` extra and an existing Miles installation; GPU training patches
remain experimental and are excluded. See [host configuration](DSEC_HOST_CONFIGURATION.md),
[service deployment](SERVICE_DEPLOYMENT.md),
[sandbox daemon](SANDBOX_DAEMON.md), and [system status](DSEC_ELASTIC_SYSTEM_STATUS.md)
before configuring a host. Artifact preparation and experimental scripts live
in `experiments/` and are not installed by the wheel.

## Measurement boundary

Docker comparison must pair the same task revision, verifier, model, image
identity, cache state and concurrency. Shared DSec services, guest memory and
host page cache belong in its full backend resource total. See the
[comparison protocol](DOCKER_DSEC_BENCHMARK_PROTOCOL.md). Existing small-scale
measurements have **not** established a general DSec memory or performance
advantage over Docker.

The project's own code and documentation use the [MIT License](LICENSE).
The packaged Miles-derived agent loop retains Apache-2.0; both licenses and
attribution accompany the wheel. See [third-party notices](THIRD_PARTY_NOTICES.md).
This is a development packaging checkpoint, not a public release. Final source
publication is a separate step. The r5 installed candidate passed protected-disk
integrity, paired backend accounting, short GRPO and prepared-state isolation;
see the [latest acceptance](DSEC_V01_GRPO_ACCEPTANCE.md) and
[cost report](DSEC_VERITY_PILOT_REPORT.md). This is a trusted single-host control
plane. External binaries, images and models remain separately provisioned and
subject to their own licenses; a complete deployment image is not included.
