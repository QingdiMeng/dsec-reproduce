# Roadmap: reproducing DSec as a usable elastic sandbox system

Updated: 2026-10-06. Baseline: the `0.1.0.dev0` r5 installation candidate.

The goal is a general elastic sandbox system for agent workloads and RL, with
reproducible evidence for the mechanisms described in the
[DSec paper](https://arxiv.org/abs/2609.22978). TB2.1 is an optional application
case, not the system boundary. This is an independent reproduction, not the
original DSec implementation.

v0.1 is a trusted single-host prototype with working storage, scheduling,
microVM recovery and short RL integration. It is not a complete reproduction
of the paper's production platform or its performance results. Future version
names below are planning targets, not released capabilities or calendar
commitments. Follow the milestone order; move a date rather than weaken an
acceptance gate.

The [modular refactor design](docs/architecture/MODULAR_REFACTOR_DESIGN.md)
was approved on 2026-10-08 against revision `7f70524`. R0 compatibility gates
and R1 general-module migration are implemented. R2 has separated worker scoring
into registered evaluators and the optional TB2.1 application, and moved
container lifecycle ownership from the compatibility client to runtime Edge.
The SDK is transport-only. Node reservation mechanics, episode slots and API
rate quotas are now separate modules with one derived compatibility report.
Edge now owns durable node admission for direct SDK calls and scheduled work;
workers own only episode/API quotas. Lifecycle decomposition, session/storage
boundaries and R3 real-host acceptance remain pending. This does not change the mechanism
validation statuses below; moving a component does not establish a missing
paper capability.

## Current alignment with the paper

Here, **validated** means a named real execution has passed, **partial** means
the mechanism exists with missing integration or a narrower validation scope,
and **missing** means there is no complete supported path. Validation on one
path does not establish the same result for every storage or runtime backend.

| Paper capability | v0.1 status | Remaining boundary |
| --- | --- | --- |
| libdsec-style client and backend selection | Partial | Container and microVM paths exist; some run arguments are unsupported. FnCall and full VM backends are absent. |
| Shared EROFS layers and private OverlayFS writes | Validated on single-host paths | Generic offline layer compaction is not yet a supported publication policy. |
| On-demand storage, local metadata and local/3FS sources | Validated on selected paths | Distributed throughput, failure handling and heterogeneous workload benefits are not established. |
| Rust OverlayBD and ublk storage path | Validated integration | External upstream components; not bundled binaries or a reproduction of their implementation. |
| pmem/DAX sharing of read-only file pages | Validated on generic two-VM paths | Not enabled in the latest paired-cost or GRPO acceptance; DAX objects currently require local backing and a compatible VMM. |
| DAMON and free-page reporting | Partial | FPR is supported; DAMON remains profile-specific rather than a general workload policy. |
| LS/BE CPU QoS | Partial | Container experiments exist; automatic placement and VMM coverage are incomplete. |
| microVM pause, terminate and snapshot resume | Validated | Small-scale real recovery, not exhaustive failure or scale validation. |
| Container pause and memory offload | Missing from the public SDK | Container pause/resume, reclaim and prefetch require a supported implementation. |
| Agent execution independent of GPU preemption | Partial | Worker persists sandbox/actions; the Miles-side agent loop and training trajectory still depend on the trainer session. |
| `pack_diff` disk checkpoint to a new environment | Partial foundations | Disk COW and prepared-state forks do not provide the complete build, sanitize, publish and restore workflow. |
| Scheduling and resource accounting | Validated locally | Resource budgets, queue reasons and leases exist; multi-node placement, watcher and hierarchical quotas do not. |
| Production security and platform scale | Partial local isolation | netns/helpers exist; hierarchical IAM, AppArmor/eBPF policies, backend containment and distributed operations are not reproduced. |

Mechanism references: [paper §3](https://arxiv.org/html/2609.22978#S3),
[§5](https://arxiv.org/html/2609.22978#S5), and
[§6](https://arxiv.org/html/2609.22978#S6).

The current candidate passed 49 core regressions, short GRPO with rewards
`[1,0]` / `[0,0]`, four actual VMM terminate/resume events and prepared-state
isolation. This establishes integration and a valid learning update, not
long-term learning gains or all-task correctness. See the
[latest GRPO acceptance](docs/reports/DSEC_V01_GRPO_ACCEPTANCE.md).

The latest two-task paired pilot still has slower complete warm episodes than
Docker. It excludes DAX, remote 3FS and the ready pool. No general speed or
memory advantage is claimed. See the
[cost report](docs/reports/DSEC_VERITY_PILOT_REPORT.md) and
[comparison protocol](docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md).

## M0 — publish a reproducible v0.1

Priority: **P0**. Keep the existing trusted single-host boundary. Do not expand
this milestone into another GPU optimization or performance exploration.

| Work ID | Deliverable | Completion gate |
| --- | --- | --- |
| RM-001 | Public source release and contribution entry points | Publish the reviewed source and license notices, link quickstart/roadmap/support instructions, and identify the exact tested artifacts. Source preparation is complete; public publication remains pending. |
| RM-002 | CI for the release boundary | Linux source build, package-content checks, selected core/application regressions and documentation-link checks pass. Actual KVM checks run on a separately configured runner; ordinary CI must not imply that it exercised a VM. |
| RM-003 | Clean-host installation acceptance | A second Linux host provisions the documented external dependencies and a fresh non-TB guest, then passes create/execute/evaluate/stop, restart recovery and cleanup. A new venv on the existing experiment host is not this gate. |

**Exit:** a contributor can install and run the documented single-host system
without our private experiment directory, credentials or pre-existing task
images. Core installation does not fetch TB2.1, models or a GPU trainer.
The optional application and external runtime components remain explicit.

## M1 — unified storage and measurable reuse, target v0.2

Priority: **P1**, immediately after M0. This milestone turns existing E1/E2/E3
mechanisms into one supported system path and completes the missing disk
environment workflow. Extend working components before adding a new backend.

| Work ID | Deliverable | Completion gate |
| --- | --- | --- |
| RM-101 | Versioned storage capability contract | Publish and create generic environments through the installed entry points with EROFS layers, local/3FS sources and file-ext4/OverlayBD roots. DAX eligibility and unsupported combinations are explicit; no silent fallback. Cover more than TB2.1. |
| RM-102 | Generic compaction and on-demand integrity | Move offline layer compaction into the artifact publisher; preserve layer priority, whiteouts and sharing. Supported cold-create paths avoid full payload scans while retaining verified integrity. The current local verifier fs-verity result must not be extrapolated to all EROFS or 3FS objects. |
| RM-103 | Supported `pack_diff` workflow | Build with an isolated builder identity, export an incremental disk checkpoint, remove build residuals/reference answers, validate and publish an immutable environment, then create independent sandboxes from it. Reject incompatible parents, failed sanitation and incomplete publication. |
| RM-104 | Mechanism and full-cost benchmark suite | Run matched Docker/DSec trajectories through the installed system, including fresh, warm and repeated-episode conditions. Report preparation, queue, reset, work, verifier, recovery and cleanup separately, with full backend costs and raw evidence. |

Application entry points and their current packaging gaps are listed in
[use cases](docs/guides/USE_CASES.md). RM-104 includes exporting the API-agent and general
benchmark drivers as supported examples, rather than requiring private
experiment launchers.

RM-101 defines the configurations used by the other work items. Run a bounded
representative set first: a small environment, a large sparsely read
environment, shared read-only data across VMs, and private-write episodes.
Include cold input, warm reuse, local and remote storage where supported.
Compare DAX against block transport and measure amortization of prepared-state
reuse. DAX/remote combinations that need staging must account for that staging
cost rather than imply direct remote DAX.

Only expand to the optional 89-task TB2.1 case and concurrency sweeps after
representative correctness and accounting pass. Stop a performance exploration
when a bounded paired result identifies the cost or disproves its hypothesis;
record the outcome and make a concrete implementation decision. Do not keep
tuning until a favorable result appears.

**Exit:** sharing, sparse reads and episode reuse have reproducible end-to-end
tests and measured costs through one published configuration contract. A
benefit may be claimed only for the workload and conditions that demonstrate
it; a neutral or negative result remains a valid report. Prepared-state memory
forking remains a separately named extension, not the paper's `pack_diff` API.

## M2 — complete elastic rollout execution, target v0.3

Priority: **P2**. Complete the separation between GPU training and durable
agent execution described by the paper's newer architecture.

| Work ID | Deliverable | Completion gate |
| --- | --- | --- |
| RM-201 | Worker-owned agent loop and reconnectable model sessions | Persist dialogue, action identities and policy-session identity outside the trainer. After trainer/GPU interruption, reconnect without replaying committed shell side effects. Retain or recover exact token IDs, logprobs, masks and weight version before admitting a trajectory to training; discard incomplete trajectories. |
| RM-202 | Preemption-driven lifecycle and container offload | A configured preemption event pauses associated sandboxes and the next admitted action resumes them. Container SDK pause/resume includes measured reclaim/prefetch behavior; memory, swap and resume costs are reported. VM termination alone is not evidence that snapshot page-cache memory was freed. |
| RM-203 | Framework adapters | The optional MBPP/native-verl execution-reward case completed 187 GRPO updates and full before/after held-out evaluation; see the [case report](docs/reports/MBPP_VERL_FIRST_USE.md). Worker-owned multi-turn control and interrupted trainer-session recovery remain open for verl, and Uni-Agent remains planned. Each full adapter passes action deduplication, verifier-error rejection, interrupted-session trajectory handling and one real parameter update. |

**Exit:** trainer loss no longer loses the agent control loop. Correct sandbox
recovery and correct training-trajectory recovery are separately demonstrated.
OpenEnv is not introduced as a required core service; adapters stay optional.

## M3 — production isolation and adaptive single-host operation

Priority: **P3**, required before claiming untrusted or multi-tenant production
support. This is a broader deployment boundary than v0.1.

| Work ID | Deliverable | Completion gate |
| --- | --- | --- |
| RM-301 | Separated runtime and privileged identities | Worker has no Docker/root authority; narrowly scoped helpers/brokers enforce instance ownership and allowed operations. Wrong namespace context and malformed requests are rejected before mutation. Crash/recovery tests leave existing host services unchanged. |
| RM-302 | Tenant and verifier protection | Enforce identity, per-project permissions/quotas, filesystem and network policy for the supported backends. Evaluate the paper's AppArmor/eBPF approach and document any different enforcement. Treat verdicts and agent-writable evidence as separate trust domains. |
| RM-303 | Scheduling policies and workload classification | Integrate LS/BE classification, placement and VMM QoS; define DAX/DAMON/FPR policies with explicit eligibility. Validate queue fairness, tail latency, throughput and resource tradeoffs under interference. |
| RM-304 | Operational recovery and upgrades | Exercise bounded service/storage/network failures, reconciliation after restart, artifact reference recovery, TTL/lease expiry, upgrade rollback and resource cleanup. Publish the tested failure matrix and unresolved cases. |

**Exit:** the stated production trust boundary is enforced, not merely
documented. AppArmor/eBPF and quota claims require actual enforcement tests;
netns isolation alone does not satisfy the gate.

## M4 — distributed control plane and scale

Priority: **P4**. Begin after the local lifecycle, identity and resource
contracts are stable. No paper-scale capacity is inferred from single-host VM
counts.

| Work ID | Deliverable | Completion gate |
| --- | --- | --- |
| RM-401 | Multi-node API, placement and watcher | Discover host capabilities/health, place work with node admission, route by sandbox owner, and rebuild authoritative state after controller restart. Test host loss and stale ownership without duplicate writable instances. |
| RM-402 | Distributed 3FS and cache behavior | Measure remote sparse reads, bandwidth, cache accounting, unavailable replicas and node loss across actual hosts. Local single-machine 3FS deployment is not evidence of distributed availability. |
| RM-403 | Scale and optional backend expansion | Publish measured creation/restore throughput, concurrent capacity, resource density and failure scope on disclosed hardware. Add FnCall, full VM or cloud bursting only when an application requires them; keep backend coverage distinct from scale results. |

**Exit:** multi-host correctness and resource/performance reports are
reproducible. Paper-scale workloads remain an independent validation target,
not a prerequisite for describing a smaller working deployment accurately.

## Tracking and acceptance rules

- Use the work IDs above as stable issue references. They are planned work
  items, not already-created GitHub issues. Each implementation issue records
  its owner, dependencies, scope, acceptance command and evidence location.
- Track each item as `planned`, `in progress`, `blocked`, or `validated`.
  Change it to `validated` only with evidence tied to an exact source/package
  revision and deployment configuration. Milestone placement is not a status.
- A historical E experiment is supporting evidence. It does not replace an
  installed-system regression after integration, and an enabled feature is
  not automatically an enabled default.
- Preserve failed/zero-reward runs and distinguish model/task failure from
  infrastructure/verifier failure. Report evidence omissions and sampling
  boundaries explicitly.
- Keep default changes reversible and gated by supported configurations.
  Promotion of DAX, remote storage, compaction or a ready pool requires its
  correctness, lifecycle and full-cost evidence.
- GPU memory patches, MTP experiments and model selection are trainer work,
  not DSec release milestones. Extra dashboards or broad performance sweeps
  must address a named acceptance blocker to interrupt the current milestone.
- Keep frozen release evidence immutable. Documentation changes enter the next
  source export; runtime changes need the corresponding installed acceptance.

The immediate execution order is **RM-001/002/003 → RM-101 →
RM-102/103/104 → RM-201/202/203**. The production and distributed milestones
remain explicit future scope. The current v0.1 delivery checklist is maintained
in [the release plan](ROADMAP.md), and historical implementation
records are in [system status](ROADMAP.md).
