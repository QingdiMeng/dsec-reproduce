# Roadmap: reproducing DSec as a usable elastic sandbox system

Updated: 2026-10-24. Baseline: `0.1.0.dev0`, with modular R3 functional acceptance complete.

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
workers own only episode/API quotas. Existing pause/restore/cleanup transitions
now live in a composed controller with Sandbox retaining the single state owner.
Cold creation and ready-pool scheduling now use separate composed components;
the durable registry owns directory locking and recovery, with a composed Edge
service entry point. Bounded shell dispatch and guest/Docker command channels
now share an explicit execution contract while retaining existing journals and
wire protocols.

The 2026-10-21 refactor also treats deployment as an acceptance boundary
([CPH-008](docs/architecture/MODULAR_REFACTOR_DESIGN.md#control-host-decisions)).
The generator now separates read-only planning from private artifact generation
and accepts batch Node inventories through shared role configuration. It creates
per-Node credentials and host-scoped exports; this does not constitute automatic
cluster installation. Before further feature
expansion, Node preflight and scoped network-helper initialization now have local
implementation and controlled permission-failure tests. Linux read-only preflight
has been checked using an isolated core wheel against existing file/OverlayBD
Host configurations. On 2026-10-24, a fresh isolated instance on the existing Linux
host passed 9 administrator installation checks, 13 ordinary-account helper/VM
checks and 3 final retirement checks. These cover partial-publication continuation,
repeat installation, live capability/NoNewPrivs/namespace rejection, a real
512 MiB VM and SDK cleanup. Temporary privileges and five added firewall rules
were retired; existing host network configuration and container process/start
identities matched the original baseline. This is bounded single-instance
acceptance, not clean-host automatic provisioning or adversarial isolation
certification. The workflow checks actual devices/capabilities rather than provider
names, rejects blocked Nodes before writing, and rechecks the live platform and
fixed runtime identity inside the reviewed installer. It does not provision
accounts, packages, modules or storage services. Dynamic enrollment, drain,
maintenance/resume and retirement now use the management API with root-policy
authorization, expected versions and retained request identities. New Nodes use
private CA-issued bundles and are discovered without controller configuration
changes or restarts. Membership and in-flight counts share the IAM CAS;
retirement requires zero held admissions and owning Edge/episode cleanup proof.
UNKNOWN or unreachable owners cannot be retired. Two-host acceptance used two
Linux container Nodes, two API replicas and three etcd members colocated on the
Mac. It checked discovery, real work while draining, maintenance/resume/retirement,
gateway recovery, one-API/one-member loss and fail-closed quorum loss. It is not
independent-host HA or clean-host provisioning evidence. Compatible core-runtime
upgrades now stage verified wheels in separate environments and reuse the Node
maintenance CAS for pending source/target releases. Commit and rollback require
the actual gateway, Edge and configured episode service to prove the requested
loaded build. Bounded two-host acceptance covered successful Node upgrade,
candidate startup failure with explicit source recovery, a new transaction back
to a retained version, preserved stopped episode/reward history, and per-replica
API image upgrade/rollback without rebuilding etcd members. The baseline and
candidate were controlled test builds; this is not evidence for arbitrary older
releases, state-format migration, privileged/VMM updates or independent-host HA.
Certificate/key rotation and clean-host operator workflows remain next.
CPU/GPU mixed deployment keeps trainers/model servers
external and gives DSec explicit sandbox budgets. These operational flows remain
open where their corresponding real-host evidence is not yet recorded.

Runtime storage now has a shared interface for catalog preparation, private
disks, checkpoints, integrity and device cleanup over the existing file and
OverlayBD components. Live identities/commit state remain Edge-owned.
TB2.1 task adapters, pinned suite/verifier rules, command normalization and
cache manifests now live in the optional application. Generic catalog loading
and Miles policy sessions do not require it; legacy CLI/import/operation names
remain compatibility bridges. R2 local responsibility separation is complete;
R3 has passed real old-to-new VM/container adoption, restart recovery, shared
node admission, ready checkout, prepared-state forks, a TB2.1 official verifier
and a four-step native MBPP/verl GRPO integration with nonzero gradient.
Installed Linux regression: 322 passed and one expected release-boundary skip.
Real 3FS-source acceptance also passed after restoring a separate backup clone:
two VMs opened the same 3FS-backed EROFS layer, preserved pause/restore state,
isolated private writes and released their leases. The publication hash check
warmed this object; this is functional evidence, not a cold-read or distributed
performance result. Planned R3 functional gates are complete. Second-host
installation and broader performance/fault campaigns remain separate gates. See the
[installation acceptance](docs/reports/DSEC_V01_INSTALL_ACCEPTANCE.md).
Native shell sessions, binary files and bounded streaming now have an
SDK/Edge/shared-C-agent implementation with single-host Linux acceptance:
real vsock, idle-session pause/restore, layered containers, idle/paused Edge
reattachment and active-stream crash recovery with UNKNOWN/no replay.
The old guest still executes legacy commands and rejects unsupported native
calls. A TB2.1 task passed its official verifier, and 32 fixed-code MBPP
episodes passed the existing adapter's reward and cleanup checks. A short
Qwen3.5-2B/verl GRPO run completed four updates with nonzero gradients and
validation; it is an integration gate, not a new model-quality benchmark. Native SDK
adoption by trainer loops remains separate from preserving those adapters.
HTTP response streaming, PTY/interactive stdin and in-flight-command snapshot restore
remain outside this iteration. See the [native SDK contract](docs/architecture/AGENT_ENVIRONMENT_CONTRACT.md).
Linux aether/chronus responsibilities now have canonical proxy/session/file/HTTP
modules and bounded execution gates. Session-scoped files and optional guest-curl
HTTP are supported; complete cross-platform aether/chronus and QEMU production
containment remain later capabilities. See the
[execution/isolation boundary](docs/architecture/EXECUTION_ISOLATION.md). This does not change the mechanism
validation statuses below; moving a component does not establish a missing
paper capability.

## Current alignment with the paper

Here, **validated** means a named real execution has passed, **partial** means
the mechanism exists with missing integration or a narrower validation scope,
and **missing** means there is no complete supported path. Validation on one
path does not establish the same result for every storage or runtime backend.

| Paper capability | v0.1 status | Remaining boundary |
| --- | --- | --- |
| libdsec-style client and backend selection | Partial | Public Container and microVM paths exist. A private QEMU/KVM primitive passed real execution/fault and concurrent CID recovery gates; outer-VM Container, FnCall and public FullVM integration remain unfinished. Some run arguments are unsupported. |
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
| Scheduling and resource accounting | Local runtime validated; HA control development preview | Resource budgets, queue reasons and leases exist. IAM/placement/watcher have bounded acceptance; independent-host availability, scale and production failure behavior are not established. |
| Production security and platform scale | Partial local isolation | netns/helpers and IAM preview exist. Enforced AppArmor/eBPF policy, QEMU backend containment and production distributed operations remain unfinished. |

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
| RM-001 | Public source release and contribution entry points | Public repository and development release exist, with license notices and quickstart/roadmap/support instructions. Modular R3 changes are locally committed and validated; review and publication of this revision remain pending. Keep the exact tested artifact identities in the acceptance report. |
| RM-002 | CI for the release boundary | Linux source build, package-content checks, selected core/application regressions and documentation-link checks pass. Actual KVM checks run on a separately configured runner; ordinary CI must not imply that it exercised a VM. |
| RM-003 | Clean-host installation acceptance | A second Linux host provisions the documented external dependencies and a fresh non-TB guest, then passes create/execute/evaluate/stop, restart recovery and cleanup. A new venv on the existing experiment host is not this gate. |
| RM-004 | Formal concurrency contract and implementation correspondence | Finite TLA+/TLC safety models and required unsafe counterexamples are implemented and wired into CI. Controlled implementation traces now cover the C-agent signal/result boundary, queued cancellation, stop finality, old-incarnation callbacks and container native/stop admission. Deterministic regressions and required code mutations catch reintroduced faults; fixes passed isolated Linux acceptance with real Firecracker, snapshots/adoption, and Docker/EROFS on 2026-10-10. Remaining: broader composition (files, leases, network containment and distributed recovery). These bounded checks do not establish full runtime refinement or unbounded linearizability. |
| RM-005 | Product API entry and application migration | The public `DSecClient` now requires verified HTTPS API endpoints, IAM token/project and explicit creation demand, with no UDS fallback. `DevEdgeClient` and old compatibility imports are development-only; local worker transport is not publicly exported from the SDK. The durable rollout/task API, TB2.1/Miles and MBPP/verl adapters, and the MBPP native launcher use the authenticated API. The separately released historical Miles full-suite recipe still needs launcher migration. Controlled tests cover interrupted replies, project namespaces, verifier failure and proof-only quota release; required replay/cleanup code mutations fail. Linux TLS/mTLS acceptance passed eight fixed-response gates: the Miles loop, five MBPP fixtures, eight independent MBPP episodes at concurrency two, and the original TB2.1 regex-log verifier. IAM usage, Node leases and active worker episodes returned to zero. Linux regressions ran 376 tests with one skip. These gates use a CAS coordination fixture, not an etcd quorum, and are separate from historical training-quality scores. Remaining: evidence-based recovery for indefinitely unknown dispatched creates, queued episode cancellation and another full training campaign through the API. |

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

## HA cluster preview — implementation on 2026-10-11

The 2026-10-12 deployment preview adds a source-shipped Docker Compose topology:
two installed-wheel API/controller containers, three real TLS/authenticated
etcd members, and an external provisioned Linux Node. Controlled API reply loss
and etcd quorum failures passed against a real microVM, with preserved owner,
VM state and quota and no duplicate command effects. The generator pins images
and wheel identities and exports private Node/client configuration. See the
[Docker deployment instructions](docs/guides/CLUSTER_DEPLOYMENT.md#docker-preview-with-an-external-linux-node).
This closes the single-Docker-host deployment rehearsal. The follow-up also
passed a quorum distributed over two hosts and API failover across those hosts.
Multi-Node placement, three independent quorum failure domains and ingress
availability remain open. Physical-host loss was not performed.
The follow-up implements typed worker-readiness UNKNOWN and explicit
`recover_create`: a retained ADMITTED receipt may cross its original first-send
gate; dispatched receipts are inspection-only. Controlled code tests and the
RolloutDispatch model enforce the health boundary, with a required unsafe
mutation. etcd service login/renewal uses a prefix-limited identity and private
password file, preserves valid tokens during transient renewal failure, and
fails closed after expiry. Cross-host role exports provide explicit peer/client
addresses, per-host UID/GID, private data and limited credentials; only the
administrator host receives initializer credentials. The two available hosts
cannot establish arbitrary physical-host HA; three fault domains remain the
final availability gate. Seven two-host service-fault gates and the three
real-auth renewal/denial gates passed; eight installed-wheel application checks
passed through the remote API. No physical-host loss or GPU training was run.

Live-episode service restart acceptance also passed nine checks on the same
two hosts. Both API replicas, then gateway/worker, then API/gateway/worker/Edge
were stopped and restarted around a committed agent action. The exact VMM
process identity, dialogue, history and held quota survived; duplicate action
submission did not replay effects. Continued execution and counter verification
returned reward 1, followed by zero quota/leases. The source includes
`tools/verify_rollout_restart.py` and a real-worker protocol regression. This
covers quiescent committed work with surviving hosts/quorum, not a killed
in-flight verifier, stream or model session, or physical-host recovery.

IAM, stateless API ingress, placement and watcher modules now have a development
implementation with active controller replicas and quorum-backed IAM admission.
Node physical resources and execution truth remain owned by the existing Edge.
See [cluster design and acceptance](docs/architecture/CLUSTER_CONTROL_DESIGN.md)
and [deployment guide](docs/guides/CLUSTER_DEPLOYMENT.md). Local process/member
failure and a real Linux Node have been exercised; independent-host availability,
operator recovery for indefinitely unknown dispatches, quota resizing and long-lived route-signing key
rotation remain open. This preview does not replace existing milestone gates.

## Cluster management console preview

The optional [management console](docs/guides/CLUSTER_CONSOLE.md) provides
project/Node/allocation/admission/audit views and existing create/stop/IAM/native
operations through the authorized cluster API. It owns no new VM registry.
Browse reads validate IAM at the return boundary, missing Nodes remain visible,
and BOUND is not presented as RUNNING. The preview has portable authorization
and HTTP gates; browser simulation is distinct from real KVM acceptance.
Production ingress/independent failure domains, long-lived route-signing and
etcd peer/JWT root rotation, indefinite
unknown-dispatch recovery, indexed inventories above 4,096 records and final
release-artifact live acceptance remain release gates.


The CPH-011 rotation extension separates independent Node authentication keys
from persistent route signatures, adds explicit IAM access-token overlap and
completion/cancellation, and supports reloadable versioned TLS trust/identity
and prefix-limited etcd service identities. Operators publish/distribute private
material explicitly and verify each peer before retirement; no unknown mutation
is automatically replayed. See the [rotation guide](docs/guides/CLUSTER_DEPLOYMENT.md#certificate-and-credential-rotation).
The bounded safety gates cover 48 finite-model cases and five dedicated security
mutations in addition to the twelve cluster controls. Automated fleet-wide
credential distribution/acknowledgement, long-lived route-key migration and
etcd peer CA/JWT signing-root migration remain separate work.

Installed-wheel rotation acceptance passed 21 checks on the two available hosts:
an existing container and SDK object remained usable through Node key, API token,
API/Node CA and leaf, etcd client leaf and restricted service-account rotation.
Old identities were rejected and all five control-plane plus three Node service
processes remained unchanged. Cleanup returned held work to zero; existing host
containers were unaffected. This is bounded transport/credential evidence,
not independent-host HA or the remaining root-key migration mechanisms.
