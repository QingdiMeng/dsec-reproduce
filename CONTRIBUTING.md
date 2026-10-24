# Contributing

This project independently reproduces DSec mechanisms. Start with the
[roadmap](ROADMAP.md), [quickstart](docs/guides/QUICKSTART.md) and
[comparison protocol](docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md). Runtime execution requires Linux and explicitly provisioned VM/storage
components. The distributed API deployment remains a development preview;
see the roadmap for the exact isolation and failure-acceptance boundaries.

Open an issue describing the problem, supported configuration, expected
behavior, actual result and a minimal reproduction. For planned work, include
the roadmap work ID (for example, `RM-103`) and its acceptance gate. Remove
credentials and private task data from any attached logs.

Keep changes focused on one behavior. A pull request should explain the user
impact, implementation choice, validation and remaining limits. Preserve the
project's MIT license and the Apache-2.0 notices for Miles-derived code.

Update an existing implementation before adding another runner or helper.
Keep one canonical source; compatibility entry points should delegate to it.
Use configuration for deployment differences instead of copying launch scripts.
Guides and contracts describe current behavior, while acceptance reports record
bounded evidence. Do not append debugging diaries to operational documentation.
Trainer compatibility patches remain opt-in and separate from the core package.

## Source ownership and refactoring

| Path | Responsibility |
| --- | --- |
| `src/dsec/` | Sandbox contracts, SDK, control plane, runtime, storage and rollout services |
| `src/dsec_adapters/` | Thin framework/task adapters; no training algorithms |
| `src/dsec_image/` | Generic image preparation |
| `compat/python/` | Existing top-level imports and historical launch modules |
| `apps/` | Optional benchmark data, evaluators and training recipes |
| `native/`, `guest_agent.c`, `guest_native.c` | Guest execution mechanisms |
| `deploy/`, `tools/` | Deployment templates and reviewed build/acceptance commands |
| `tests/`, `verification/` | Portable regressions and bounded concurrency models |

Put new implementation in its owning package, never at the repository root or
in a legacy wrapper. Preserve module identity when retaining a compatibility
alias. Three historical modules (`tb2_backend`, `service_admin` and
`firecracker_smoke`) still contain legacy behavior; their extraction needs its
own dependency and deployment review. Do not grow that surface.

Worker names describe a process role. Episode coordination belongs to
`dsec.rollout.episodes.EpisodeService`; its lock and journal ordering remain the
single mutation authority. Action submission, dialogue projection and evaluator
invocation are separate components without independent lifecycle state. The old
worker constructor only adapts configuration and inherits that implementation.
Keep RPC framing and process startup in `rollout.server`, task rules in optional
applications, and model/token/training state outside the episode service.

Control-plane and Host CLI refactors follow the accepted
[CPH-001 through CPH-011 decisions](docs/architecture/MODULAR_REFACTOR_DESIGN.md#control-host-decisions).
Reference applicable decision IDs in the review, with compatibility evidence.
Changing a decision requires its rationale and migration impact to be recorded
there, together with updates to affected dependency gates and protocol/model
mapping. The decision record is the canonical source; avoid copying it into
another checklist or debugging diary.

Host configuration and composition are read-only preparation: use
`host.configuration` to normalize copied data and `host.composition` to build
service plans. Apply directory/environment/budget/process effects only through
`host.services`, and generate explicit deployment outputs through
`host.deployment`. CLI code parses and routes; other modules must not import it.
Shared validation belongs to contracts rather than requiring runtime imports.

Deployment is part of refactor acceptance. Keep read-only topology/artifact
planning separate from secret/file rendering, administrator provisioning and
service startup. The cluster generator's `--plan` must work without OpenSSL,
Docker or an installed runtime and must not create files. Reuse role configuration
for heterogeneous nodes; GPU/model management remains outside the sandbox system.
Single-Node arguments and batch `--nodes` normalize into one plan and use
`deploy/cluster/configuration.py` for all role configurations. Keep Node-specific
certificates/tokens and host export scope aligned with controller routes. A new
deployment directory generates new identities; it is not a join/upgrade tool.
Batch join, maintenance, upgrades and rollback require explicit failure and
clean-host acceptance before being described as supported deployment workflows.

Cluster decisions live in `control.cluster_api`, `rollout_api`, `iam`,
`cluster_inventory`, `placement`, `watcher`, `node_gateway` and `rollout_gateway`.
They consume injected interfaces, not Host/SDK or concrete network/store clients.
Keep Node RPC envelopes in `host.node_transport` and HTTP/TLS in
`host.cluster_http`; neither adapter owns admission or release decisions.
`host.cluster` composes these with the real etcd and private UDS clients.
Shared RPC errors belong to `contracts.rpc`, with SDK imports retained as aliases.
The historical Host gateway constructor inherits the canonical decision methods;
do not add policy to its compatibility dispatch wrapper. Model mutation controls
and canonical regressions must target the control modules used in production.

Install an editable checkout before running scripts; source layout is not an
API. Installed import names, CLI arguments and durable state formats stay stable
unless a separately reviewed migration explicitly changes them. A package move
alone does not establish runtime, isolation or training acceptance.

## Local package checks

Tests are grouped by execution requirements:

| Directory | Purpose | Automatic discovery |
| --- | --- | --- |
| `tests/unit/` | Core scheduling, storage, lifecycle and recovery regressions | Yes; Linux-specific checks skip elsewhere |
| `tests/contracts/` | Frozen v0.1 interfaces, legacy imports and module dependency boundaries | Yes |
| `tests/adapters/` | Task, Miles message/reward and verifier guards | Yes; the legacy 3FS publisher check needs the experiment workspace |
| `tests/packaging/` | Source export and opt-in bundle boundaries | Yes; the legacy bundle check needs the experiment workspace |
| `tests/benchmarks/` | Comparison harness accounting and host-isolation guards | Explicit suite in the experiment workspace |
| `tests/training/` | Opt-in trainer compatibility and GRPO probe checks | Explicit suite in the experiment workspace |
| `tests/integration/` | Manual Linux/KVM, snapshot faults and task acceptance | No; files are named `check_*.py` |

Run manual checks as modules, for example
`python -m tests.integration.tb21.check_tb2_trainer_rejoin --help`, after reviewing
their host paths and prerequisites. Some checks require an isolated service,
privileges or provisioned artifacts; they are not part of regular CI.
The published source includes the four portable suites above. CI and local
checks use unittest discovery, independently of the release exporter. New tests
in those suites are discovered and exported without a filename registration.
Extended experiment suites remain outside the published checkout.

Use Python 3.11 or newer in a virtual environment:

```sh
python -m pip install 'setuptools>=77' wheel build
python -m pip install --no-deps -e . -e apps/tb21
python -m build --wheel --no-isolation --outdir dist/core .
python tools/check_wheel.py dist/core/*.whl
python -m unittest discover -s tests -t . -v
python tools/check_docs.py
python -m build --wheel --no-isolation --outdir dist/tb21 apps/tb21
python -m pip install dist/core/*.whl dist/tb21/*.whl
python -m unittest discover -s apps/tb21/tests -v
```

The source archive allowlist in `tools/package_release_source.py` defines the
exported runtime, regression and application boundaries. Runtime modules follow
package metadata; portable tests follow the four named suite directories. Update
the explicit tool/document allowlist when adding a required release file; never
include generated artifacts or experiment state. GitHub CI builds both wheels and runs selected regressions without
provisioning KVM, Docker, GPU models or TB2.1 tasks. It is not a real sandbox
or clean-host acceptance test.

The portable TB2 verifier tests require the optional `apps/tb21` package;
they use fixtures and do not download tasks. Core-only installations can import
and construct the generic worker without this application. Test a new evaluator's
normal result, invalid verdict, interruption and completed-record recovery;
do not silently turn infrastructure failure into model reward zero.

Changes to lifecycle, storage, privileges or recovery also need the relevant
real Linux acceptance in an isolated instance. Record the exact revision,
configuration, raw evidence and cleanup result. Stop owned sandboxes through
the SDK before retiring services. A VM PSS comparison does not establish a
full backend resource advantage, and model failure is not an infrastructure
failure.

Lifecycle/native concurrency changes also require the bounded TLA+ checks:

```sh
python tools/check_concurrency_model.py --fetch --out /tmp/dsec-model-check-unique
python tools/check_shutdown_refinement.py --fetch --out /tmp/dsec-shutdown-check-unique
python tools/check_native_race_refinement.py --fetch --out /tmp/dsec-native-race-check-unique
python tools/check_cluster_refinement.py --root /tmp/dsec-cluster-check-unique
```

Use a new output directory for each run; logs and counterexamples are retained.
Java 11+ and the correspondence check's C compiler are development dependencies
only. `--fetch` explicitly downloads the
fixed, SHA-256-checked TLC version; offline runs use `--jar /path/to/tla2tools.jar`.
CI must pass both exhaustive safe-model checks and the expected invariant
violations in unsafe variants. Parser failures, missing tools and incomplete
searches are failures, not skips. See the
[model boundary and code mapping](docs/architecture/AGENT_ENVIRONMENT_CONTRACT.md#formal-concurrency-model).
Model checking validates the specified finite protocol, not the runtime's
refinement of it. Map code admission, commit, fencing and cleanup boundaries
to the model, and turn counterexamples into controlled implementation tests.
Fleet changes must preserve the `NodeLifecycle` mapping and its three required
guard-removal failures: admission racing drain, retirement with held work, and
stale transitions after retirement. Run the canonical controlled schedules in
`tests/unit/test_node_lifecycle.py`; random repetition is not their substitute.
Release changes additionally map `NodeUpgrade` to the pending-plan fence and
loaded-release proof in `NodeLifecycle._transition`. Its two guard-removal
controls must reject unproved completion and resume during a pending upgrade.
Review compatibility contract changes as migrations; a version label is not
evidence of data-format compatibility.
Security changes also preserve the CPH-011 mapping: `CredentialRotation` maps
IAM CAS plus old/new completion and cancellation proof; `TransportRotation`
maps publication generation fences and the request-acceptance trust check.
Run `python tools/check_security_refinement.py --root /tmp/dsec-security-unique`:
all five guard removals must cause their designated controlled regression to
fail. Keep `_credentials`, `_security_files` and `_tls` independent of Host,
control and runtime; the shared dependency gate permits only stdlib/contracts.
Changes to TLS behavior require real TLS tests and the affected installed-service
rotation acceptance; finite model success alone does not prove PKI correctness.
The shutdown correspondence check compiles the real C agent with probes,
injects signals at named boundaries and asks the same TLA+ model to accept/reject
the observed state sequence. It must reject the historical assignment mutant.
Changes to that boundary require updating/reviewing this mapping; do not make
Python duplicate the model's transition rules or weaken the safety property.
The native-race tool runs the canonical `tests/unit/test_native_races.py` against
real journals, lifecycle methods and local C-agent commands. Barriers reproduce
queue cancellation, late callbacks, incarnation replacement and container
stop/admission orderings. Four required code mutations must make the specified
regressions fail; their observed traces must conform to the corresponding
unsafe specification, violate its invariant, and be rejected by the safe one.
A changed marked code fragment, missing trace, skipped test, setup/cleanup error
or unexpected TLC failure rejects the mapping. Source snapshots and hashes tie
the verdict to the implementation. These checks use instrumented Docker/KVM
drivers. Real Linux acceptance uses `tools/verify_native_races.py` with a new
exclusive `--root`, pinned `--binary`, `--kernel`, `--template`, `--native-agent`,
optional `--vm-catalog`/`--vm-environment`, and `--container-catalog`/
`--container-environment`. Supply the tested source manifest SHA with
`--source-manifest-sha256`. This creates at most two VMs and one container at a
time, uses no GPU/network, and stops owned resources. The prepared disks must
already contain the tested agent; the tool does not modify immutable templates
or restart existing services. Recheck the same models against its six physical
traces:

```sh
python tools/check_native_race_refinement.py --fetch --observed /path/to/private-root/report.json --out /tmp/dsec-linux-trace-check-unique
```

A failed behavioral or cleanup report cannot be used as successful acceptance.
Record source/wheel/agent identities, complete raw evidence and host continuity;
fixture tests alone do not satisfy this gate.

For each change to a mapped concurrency boundary, review the transition and
invariant mapping, update the controlled regression/projection when necessary,
and run the affected correspondence tools (cluster, security, shutdown and native). A source-text mutation mismatch must be
reviewed, not bypassed. Changes to lifecycle behavior also require the relevant
real Linux acceptance. CI retains source snapshots, observations, checker logs
and counterexamples as `concurrency-evidence` artifacts for 14 days; TLC's
temporary state directories are excluded.

See release notes for the tested artifact identities. Future roadmap items
remain planned until their acceptance evidence is attached; do not replace
historical or frozen evidence with results from a different revision.

Node permission checks must run before installation targets, gateway state or
coordination connections are created. Keep `host/node_probe.py` standalone and
standard-library-only: the reviewed administrator installer embeds it and must
recheck the live platform plus the fixed runtime identity, never trust a saved
preflight verdict. Reuse the instance-scoped helper renderer; do not add a
general privileged command endpoint. Controlled regressions must cover missing
devices, device-access denial, capability bounds, remapped namespaces and blocked
initialization before writes. Container/provider names are not eligibility
rules. Fixture success does not count as real Linux boot/isolation acceptance.
