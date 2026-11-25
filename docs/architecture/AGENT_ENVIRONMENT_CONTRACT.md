<a id="agent-环境接口"></a>

# Agent environment interface

**English** · [简体中文](AGENT_ENVIRONMENT_CONTRACT.zh-CN.md)

<a id="通用性设计方法2026-10-30"></a>

## Designing for generality (2026-10-30)

The goal is **a general-purpose DSec sandbox system**. TB2.1, MBPP, SWE-Gym, OpenHands, mini-swe-agent and MiMo supply requirements and test cases. They do not define the structure of the core. This design neither introduces a universal agent framework nor requires external applications to use the rollout interface below.

When an application exposes a problem, proceed in this order.

1. Describe the required execution behavior independently of a benchmark, agent or trainer. For example, files persist in the same instance while each command starts a fresh shell.
2. Identify the category (execution, state, artifacts, resources, networking, recovery or deployment) and the existing owner of the state.
3. Distinguish application policy from system guarantees. The application determines prompts, steps, context, scoring and training sample admission. DSec establishes admission, isolation, execution results, state identity and resource release.
4. Extend existing protocols and modules first. Introduce a versioned capability only for system semantics they cannot already express.
5. Write acceptance criteria independent of any model or agent, then exercise more than one application to check reuse. Report interface tests, real Linux execution, complete application runs and performance findings separately.

<a id="关键决策"></a>

### Design decisions

| Decision | System constraint | Source in practice |
| --- | --- | --- |
| GEN-001: The caller owns the interaction loop | Use the public SDK directly or opt into `AgentEnvironment`. The core neither parses bash code blocks nor manages thinking, summaries, training tokens or logprobs. | TB2.1 feedback and TITO, OpenHands context and tool protocols |
| GEN-002: Match capabilities, not application names | Declare versioned execution capabilities on creation. They constrain placement alongside environment ID, execution mode and resource demand. API support does not imply support on every Node. | Portability of tools, files and streaming SDK operations across backends |
| GEN-003: Preserve state semantics across interfaces | File persistence and shell persistence are distinct. FnCall invocations are independent, Container retains a workspace and MicroVM has its own recovery boundary. Reject unsupported semantics explicitly without switching mode or falling back to the host. | Multi-step code repair, independent MBPP evaluation and cross-episode forks |
| GEN-004: Recover state at its owner | Edge proves instance state and execution. The control plane handles admission and routing to the original node. The application recovers its own state. Sandbox recovery does not establish recovery of the external agent, model trajectory or trainer checkpoint. | Reconciliation after disconnects, native tool epochs and SAO training |
| GEN-005: Preserve budget and result semantics | Treat queue, creation, command, caller wait, total application and scoring budgets separately. A connection timeout cannot become a command timeout. The core reports execution and truncation evidence. The application interprets its scoring implications. | TB2.1 verifier, long trajectories and incomplete feedback |
| GEN-006: Environments are reusable artifacts and mutable state belongs to an instance | The core accepts published environment identities. EROFS/OverlayBD/ublk, applicable pmem/DAX and local/3FS storage paths are preserved. Immutable layers are shared and private writes isolated. Task mapping and dependency preparation belong to optional applications or publication. | Multi-task images, dependency installation and repeated rollouts |
| GEN-007: Completion requires evidence | Prove command completion, valid task scoring, instance stop and resource release separately. UNKNOWN retains its identity and forbids side-effect replay. It neither creates a zero training reward nor releases resources. | Failed trajectories, official verification, shutdown and lease release |

GEN-006 also applies when deriving environments. The build instance owns private writes. The publisher separately specifies the parent revision and the content allowed into the new version. The original catalog remains usable on publication failure. The child has a distinct environment identity and requires fresh verification of its execution capabilities. See the [artifact publication guide](../guides/ARTIFACT_PUBLICATION.en.md#从隔离构建沙盒发布文件增量) for the direct ext4 file-delta implementation and limits of TASK-024.

TASK-026 distinguishes two revisions for MicroVMs. Publisher and caller identify the artifact by its portable manifest SHA-256. Node and Edge verify the concrete target with a deployment revision incorporating local paths. The control plane pins both Node and deployment binding. Duplicates, pause and resume route to the original owner. See the [publication guide](../guides/ARTIFACT_PUBLICATION.en.md#跨-node-交付同一-microvm-环境) for artifact delivery.

<a id="能力声明与环境绑定"></a>

### Capability requirements and environment binding

Public `DSecClient.run_microvm/run_container` accepts optional `required_capabilities`. It reuses [native protocol capabilities](../../src/dsec/contracts/native.py), Node capability views, PlacementEngine and IAM admission without introducing another environment scheduler.

- The supported requirements are `native-sessions-files-v1`, `native-stream-v1`, `native-aether-chronus-v1` and `native-http-v1`. Extensions implicitly require the base native protocol.
- `sandbox-requirements-v1` and `environment-requirements-v1` signal that API and Node understand creation requirements and environment binding. **They do not advertise execution capabilities.** The SDK rejects a requirements-bearing creation on an old API. Placement excludes Nodes that cannot understand the contract.
- Normalize requirements as a set and include them in the IAM request identity. Changing requirements under the same ID must conflict. An empty set preserves the old digest and Edge wire format. Applications cannot supply a forged `environment_contract` in a creation request. The control plane selects the environment contract from Watcher observations and pins it in the IAM receipt.
- The Node obtains its execution capabilities from live Edge health and rechecks before dispatch. If Watcher data is stale and the Node no longer meets the request, reject and release admission only after confirming no dispatch and no Edge intent. For dispatched or UNKNOWN requests query the original owner without selecting another Node.
- `EnvironmentView` describes one environment's ID, backend, execution mode, deployment revision and explicitly declared protocol capabilities. A capability available in another environment on the same Node does not satisfy a requirement. An undeclared capability is unknown. Never infer it from the runtime name or Node protocol.
- Bind capability claims to the artifact pins in the environment descriptor. Changes to the kernel, boot disk, layers, mode or configuration invalidate the old claim. The publisher rebinds rewritten deployment paths only after verifying the source claim and bytes. The deployment revision includes paths and configuration. **It is neither a cross-host content-deduplication key nor an OCI image digest.**
- The Node checks the selected revision before dispatch. Edge checks again before journaling a new create intent, preventing a service replacement after the Node check from running another environment. The binding enters the Edge creation digest. Reconcile accepted or completed requests against the original journal even if a newer catalog withdraws the environment. Do not replay them or forward the binding to the guest or VMM.
- These are **trusted publisher claims bound to artifacts**. They do not automatically probe a guest, formally verify a guest ABI or prove application dependencies. pmem/DAX, networking policy, production isolation and snapshot compatibility require their own evidence. A string or claim alone cannot establish support. Existing runtime artifact integrity checks still apply.
- These requirements apply to direct SDK creation. They preserve the optional rollout API, FnCall invocations and existing task plug-ins. They neither select execution modes automatically nor turn FnCall into a persistent session.

`tests/unit/test_api_sdk.py` and `tests/unit/test_cluster_control.py` cover old-endpoint rejection, requirement filtering, normalization and conflicts, capability withdrawal, unknown outcomes without replay, and compatibility with old digests. These control-plane and transport fixtures do not validate a mixed physical fleet, old guests or training. Upgrade all API replicas first in a rolling deployment. A health check does not establish protocol compatibility across old replicas.

On 2026-10-30, 203 of 205 local regressions passed. Two Linux mountinfo/`/proc` checks were skipped on macOS. All 14 cluster guard-removal mutations failed as required, including final MicroVM and Container environment guards. Documentation links and diff formatting passed. These were local checks.

The [EnvironmentBinding model](../../verification/EnvironmentBinding.tla) covers two requests, two revisions, a stale Watcher, an environment change after the Node check and rejection by Edge. `selected` maps to the IAM receipt binding, `NodeCheck` to the Node pre-dispatch check, and `EdgeAccept` to Edge validating the pinned catalog version before journaling admission. The model assumes the catalog and artifact pins cannot be reinterpreted within one Edge lifetime. Existing catalog and backend checks establish content integrity. The model does not prove that build claims are true, binary semantics or unbounded liveness. `tools/check_concurrency_model.py` requires the safe model to pass and removing the Edge guard to produce a `AcceptedRevisionPinned` counterexample. `tools/check_cluster_refinement.py` maps the guard to local MicroVM and Container RPC regressions, both of which must fail their behavioral assertions when it is removed. The safe model passed after exploring 484 distinct states. The unsafe model found the required counterexample. This relates bounded safety checks to specific guards without proving equivalence of arbitrary implementations.

[Independent Linux acceptance on 2026-10-30](../reports/DSEC_V01_INSTALL_ACCEPTANCE.en.md#generality-linux-20261030) passed 27 live execution checks and 77 installed-package regressions. Real Firecracker MicroVM and QEMU Container workloads used the public HTTPS API and an mTLS Node for capability-based creation, binary files, persistent sessions, streaming and reconnection. FnCall invocations remained independent. The test replaced the Edge environment revision in two controlled windows: after API admission but before dispatch, and after the Node check but before the Edge create RPC. Both returned AdmissionDenied, left no Edge create intent and released IAM quota. The previous DONE proof remained queryable. This covers base native/stream conformance on two guest types. HTTP/session-file extensions, other fault windows, cross-node execution and training were outside scope. Publisher claims remain distinct from guest attestation.

<a id="后续按共性缺口推进"></a>

### Address shared gaps in later iterations

Keep accepted capabilities closed when a later test omits them. Session files passed real MicroVM acceptance on 2026-10-12. Edge restart, execution interruption and UNKNOWN without replay have real run records from 2026-10-10 and 2026-10-25. Original-request lookup after API failover and stream reconnection were also accepted. HTTP has local C agent/SDK, timeout and POST deduplication tests. The records here do not establish end-to-end HTTPS API → QEMU guest HTTP verification. See the [execution and isolation contract](EXECUTION_ISOLATION.md) and [historical installation acceptance](../reports/DSEC_V01_INSTALL_ACCEPTANCE.en.md). Add only the missing combinations. Rerun relevant tests when code, protocol, guest or deployment boundaries change. To open a new defect, identify its trigger, missing guarantee and finite acceptance criterion.

Maintain capability status, evidence and candidate work in the [project register view](../PROJECT_STATUS.en.md). For new semantics, first state the invariant, then test distinct application patterns such as stateful code repair and independent code execution within the selected task's scope. Target regressions to actual changes. There is no need to rerun every application on each iteration. Model score and sandbox correctness require separate evidence. This iteration adds no MiMo/mini-swe-agent-specific adapter, training run or model-management responsibility.

<a id="可选-rollout-组合"></a>

## Optional rollout integration

`AgentEnvironment` connects a task to a training framework. Its implementation lives in [dsec.rollout.environment](../../src/dsec/rollout/environment.py), with [agent_environment.py](../../src/dsec/rollout/environment.py) retained for compatibility. The sandbox SDK can also be used directly. No OpenEnv service is required.

<a id="职责"></a>

## Responsibilities

| Layer | Responsibility |
| --- | --- |
| Trainer's agent loop | Model sampling, reply parsing, multi-turn messages, tokens/logprobs and the RL algorithm |
| `TaskEnvironmentAdapter` | Instructions, environment and resource selection, action execution and trusted scoring |
| `DSecAgentEnvironment` | Connect the task lifecycle to the durable rollout worker |
| worker / sandboxd | Scheduling, sandboxes, action journals, pause/resume and cleanup |

DSec manages sandbox resources. External training and inference services own model weights, KV cache, optimizer state, training/inference GPU memory and model offload. Sandbox admission can wait for node headroom and external load to ease. It will not change model service settings or unload a model to get resources.

The public `DSecClient` is implemented in [sdk.client](../../src/dsec/sdk/client.py). It accepts an HTTPS API Server, IAM credential and project only. `DSEC_SOCKET` does not connect it directly to Edge. The development client is [DevEdgeClient](../../src/dsec/sdk/development.py); `libdsec_compat` and `dsec.compat.libdsec` remain module aliases for that implementation. The [Container Edge](../../src/dsec/runtime/container_edge.py) owns the backend, artifact verification and lifecycle journal. Clients neither read host artifacts nor start Docker. Existing `run_container`, `attach_container` and execute/query/stop operations share the sandbox service transport. The worker still accounts for actions and scores. RPC migration preserves existing request digests. An explicit `ServiceBusy` signals non-admission. The SDK does not silently generate a new ID for a request with a stable ID. The caller may resubmit after definitive non-admission. Reconcile UNKNOWN first.

```text
Trainer agent loop
    → AgentEnvironment: reset → step* → evaluate → stop
    → TaskEnvironmentAdapter / RolloutAPIClient
    → API Server → authenticated Node gateway
    → private rollout worker → Edge → microVM or container
```

<a id="生命周期"></a>

## Lifecycle

The caller saves a stable `rollout_id` for each episode.

- `reset(policy_prefix)`: the task plug-in returns `EnvironmentSpec`. Create or attach to the rollout, wait for scheduling, append the task instruction as a user message after the prefix and return the persistent dialogue.
- `step(action, policy_message=...)`: submit stable `step_id`, `action_id`, action type, payload and the original assistant message. Return `EnvironmentObservation`.
- `dialogue()`: query the persistent dialogue and wait for committed actions to complete. Reconcile unknown state.
- `evaluate()`: return `EnvironmentVerdict` with a finite score, evaluator identity and evidence.
- `stop()`: release this episode's sandbox and lease.

`EnvironmentAction.shell(...)` is a convenience constructor for shell actions. The protocol supports other `kind/payload`. The task plug-in decides whether an action is accepted. The interface prescribes no bash fence format, step limit or sampling parameters.

<a id="上下文与评分"></a>

## Context and scoring

Each action should carry the complete original assistant message, including text that was not executed. The task plug-in turns command output into the next observation. Trainers obtain messages from the returned `dialogue["messages"]`. Text cannot reconstruct missing tokens or logprobs.

The Miles Qwen3.5 Thinking adapter follows the [official template](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/chat_template.jinja), extracting the body after the last `</think>` and tolerating a repeated closing marker before the code block. It rejects a code block, tool call or finish signal between end markers, or a reopened thinking segment in the body, so no candidate action is silently discarded. Each turn executes only the first bash action in the body. The raw assistant reply and TITO remain unchanged. `model_outputs` records `thinking_end_tag_count` and `thinking_boundary_normalized`. The episode's `agent_metrics.normalized_thinking_steps` identifies the affected steps.

<a id="shell-反馈"></a>

### Shell feedback

The worker dialogue for a new shell episode uses `dialogue_feedback_version=2`. It returns each execution result in one user message containing `dsec.shell_observation.v1` JSON. The trainer should consume that message directly, without extracting only `output`. The task plug-in still defines observations for other actions.

| Field | Meaning |
| --- | --- |
| `step_id`, `action_id` | Identity of the durable action |
| `status` | `succeeded`, `failed`, `timed_out` or `unknown` |
| `exit_code` | Actual process exit code. Missing is `null` and must not be replaced with zero. |
| `timed_out` | Command timeout as reported by the execution layer. Missing is `null`. |
| `capture_truncated` | Whether output was truncated during capture. Missing is `null`. |
| `feedback_truncated` | Whether already captured output was further truncated to fit the model context. |
| `captured_output_chars`, `feedback_omitted_chars` | Number of characters captured and omitted from the model's observation. |
| `output` | Combined captured stdout/stderr, retaining at most 4000 characters. |

Always preserve status and truncation flags. Even empty output has a complete result. `(no output)` is no longer a substitute for execution state. Long observations retain the beginning and end with an omission marker. The action journal retains the entire captured output. If capture itself was truncated, feedback cannot recover the missing tail and must report that fact through `capture_truncated`.

Only explicit `timed_out=true` establishes a command timeout. Exit code 124 alone is insufficient. An RPC timeout or disconnect instead triggers UNKNOWN, attach and reconciliation. Never synthesize a command failure and automatically retry. The shell API waits for completion and provides no background process handle. A slow or silent command cannot be inferred to be running in the background.

Persist the feedback version with the episode. For a dialogue started before the upgrade without a version field, reconstruct version 1 and keep the old format for later actions to preserve the existing session/TITO prefix. New episodes and episodes with no dialogue yet use version 2. Reconnect, deduplication and worker restart leave existing messages intact.

The task plug-in supplies the instruction. The TB2.1 plug-in runs the original `tests/test.sh`, checks CTRF results and integrity, then interprets the result as a binary reward. Reject the sample on missing, invalid or unverifiable results. None of these is a valid model zero.

<a id="worker-评分插件"></a>

### Worker scoring plug-ins

The [scoring contract](../../src/dsec/contracts/evaluation.py) contains only the context, result and plug-in interface. It neither starts a sandbox nor loads task code. Operators register trusted implementations with `RolloutWorker(..., evaluators={id: plugin})`. RPC callers select a registered ID and cannot provide a Python module or load code. Change the versioned ID when scoring semantics change. `validate` validates the task, environment and arguments before execution. `evaluate` uses the same episode sandbox and returns `EvaluationOutcome` with finite `value`. `EvaluationFailure` means there is no valid verdict and its diagnostics are saved in the worker journal.

Call `ScheduledSandbox.evaluate(evaluator_id, parameters, timeout_s=...)`. Before execution the worker persists the evaluator ID, JSON arguments and digest. A completed evaluation with the same identity returns the original reward. Different evaluators or arguments are rejected. For old completed records without evaluator identity, the plug-in checks the original reward format. A multi-command verifier interrupted by cancellation, error or restart remains UNKNOWN. Never automatically rerun it or invent a zero. The transport timeout controls only the client's wait time, independently of the task's verifier limit.

The [official TB2.1 scoring plug-in](../../apps/tb21/src/dsec_tb21_case/worker_evaluator.py) is distributed with the optional application. A compatibility bridge registers it for the old `tb2_evaluate` request and `--tb2-tasks-dir` setting. Without TB2.1 configuration, importing or constructing the generic worker does not load the application. The old counter task retains single-RPC proof reconciliation. TB2 adapter, manifest validation and command PATH compatibility bridges still awaited migration at this stage.

The action journal saves both the user's `command` and the actual `execution_command`. Restart reconciliation uses the latter so a changed command transformation cannot reinterpret past effects. For old records lacking this field, the worker checks the original request under the compatibility rules. Deduplication still uses the original step/action ID and user payload.

<a id="episode-预算结束"></a>

### Episode budget exhaustion

The Miles DSec adapter starts its agent budget after sandbox readiness. Queueing and verification have separate budgets. Agent shell `command_timeout_ms` and official verifier `verifier_timeout_ms` are independent. TB2.1 reads the verifier ceiling from the pinned task's `task.toml`. Only the host evaluator's `run_verifier_shell` selects this ceiling. Normal agent actions use the command limit. Out-of-range requests fail before execution and report the scope, requested value and allowed range. That rejection is not a test timeout. At budget expiry no further model requests or actions begin. In-flight shell timeouts are capped by remaining time. An already submitted model request can return its full response to preserve actual tokens/logprobs. Cleanup may therefore exceed the budget. `budget_overrun_seconds` records that interval separately. The budget is not a hard wall-clock termination guarantee.

A genuinely expired trajectory returns normally with `exit_status=timeout`, `end_reason=episode_timeout` and reward 0. `reward_source=episode_budget` and `dsec_budget_verdict=true` identify the score source. No task verifier is run. Its diagnostic `raw_reward/harness` is `null`. Never label a budget zero as an official test score. Admission of this sample requires complete saved TITO for that episode, at least one complete model response, evidence that its time budget expired and no recovery across training processes. Miles still verifies native token/logprob alignment. Keep the zero in its original GRPO group without replacing the task or stopping other episodes. Write training outcomes to the ownership record so progress and final audits distinguish budget expiry, official grading and unresolved errors.

Transport timeouts, UNKNOWN execution, missing TITO and external cancellation do not qualify as budget zeros. Report errors, attach or reconcile instead of inventing tokens, exit codes or verifier results to complete a group.

<a id="恢复与隔离"></a>

## Recovery and isolation

On `ScheduledOutcomeUnknown` or `UnresolvedAction` retain the original rollout/action ID, attach and reconcile. Do not use a new ID to replay a potentially effective action. On definitive initialization failure, try to stop any sandbox already created.

Environment and dialogue may recover after trainer restart. An episode whose TITO was lost cannot update the policy. The trainer owns durable model trajectories.

`baseline_rollout_id` creates independent episodes from a sealed prepared state. Children have private writable state and independent histories. Pause/resume retains the original episode identity. These are distinct forms of reuse.

<a id="实现与验证"></a>

## Implementation and validation

- [General counter task](../../src/dsec_adapters/counter_dsec_environment.py)
- [TB2.1 task plug-in](../../src/dsec_adapters/tb2_dsec_environment.py)
- [Task registry](../../src/dsec_adapters/dsec_task_registry.py)
- [Miles agent adapter](../../src/dsec_adapters/miles_dsec_agent_function.py)
- [Interface regressions](../../tests/unit/test_agent_environment.py)

The supported entry goes through the API Server and authenticated Node gateway. Local Unix sockets are reserved for explicit development and internal services. Lifecycle semantics resemble the paper's libdsec design. This agent protocol is an engineering extension without a claim of wire compatibility with the original DSec apiserver/IAM.

See [GRPO acceptance](../reports/DSEC_V01_GRPO_ACCEPTANCE.en.md) for the latest real training, recovery and fork checks. This short acceptance does not establish success on all 89 tasks, long training or multiple hosts.


### Runtime execution boundary

The core `contracts.execution` request/result values and `runtime.sessions`
channels sit below the existing reset/step/evaluate API. Edge dispatch owns
command authorization, scope/deadline validation, the sandbox lock and automatic
resume; a channel only exchanges the bounded command and its result. The
microVM keeps its existing guest-vsock bytes and Edge request journal, while
Docker keeps its guest request IDs and query proofs. Neither channel retries a
command after a missing reply. A completed command timeout is returned as
execution evidence; transport uncertainty remains UNKNOWN. Legacy results with
no timeout evidence retain the missing field. The legacy command boundary
remains unchanged. Native sessions, files and bounded streaming use an additional
protocol described below. Neither path manages policy/model state.

### Native sandbox SDK

The public SDK reaches Edge through the API Server and authenticated Node
gateway. Edge calls one shared C agent, through native vsock port 5001
in a microVM or a private UDS in a layered container. Port 5000 retains the
original command framing. `native-sessions-files-v1` and `native-stream-v1`
are explicit capabilities; a new SDK cannot silently emulate them on an old
guest. Agent upgrade is a separately built immutable artifact. The Rust
OverlayBD/ublk path is unchanged.

```python
from dsec.sdk import DSecClient

async with DSecClient(api_endpoints, token=iam_token, project=project_id,
                      ca_file=api_ca_file) as client:
    sandbox = await client.attach(sandbox_id)
    async with await sandbox.open_session() as session:
        await session.run_shell("cd /workspace; export MODE=test")
        result = await session.run_shell('pwd; printf "%s" "$MODE"')
        await sandbox.write_file("/workspace/input.bin", b"\x00\xff")
        assert await sandbox.read_file("/workspace/input.bin") == b"\x00\xff"
        async for event in session.stream("printf started; sleep 1; printf done"):
            if event["type"] in ("stdout", "stderr"):
                print(event["type"], event["data"])  # bytes; decode incrementally if needed
            else:
                print(event["result"])
```

Each session has a persistent `/bin/sh`, cwd and environment. `run_shell` on
the sandbox remains one-shot. Commands on one SDK session serialize; different
sessions can run concurrently and share only the sandbox filesystem. A second
handle contending for a busy session receives provable non-admission and the
SDK waits with the same ID. A stream collector also waits on explicit guest
non-admission, for at most 35 seconds; `queue_wait_ms` records that wait.
Queued stream cancellation durably records its intent and prevents dispatch.
A dispatched stream may be in transit; cancellation retries only its exact ID.
`cancel_requested=true` acknowledges the intent, not confirmed termination.
Completed operations return false with their terminal state; no cancellation
targets a different command occupying the shell.
There is no PTY or interactive stdin. Commands receive `/dev/null` stdin;
detached child jobs are cleaned up after a command on Linux. Timeout, cancel,
`exit`, or loss of the shell's control FD ends the session. A subsequent call
reports `NativeSessionReset`, rather than silently creating another shell.

Native results retain separate stdout/stderr plus `exit_code`, `timed_out`,
`cancelled`, `truncated` and `session_reset`. An unknown exit code is `null`.
Shell arguments still use the environment's command deadline and proxy policy.
Output is bounded by the caller's limit, at most 1 MiB across both streams.

File transfers use 64 KiB chunks and a 64 MiB file limit. A guest reserves at
most 32 unfinished uploads and 64 MiB in aggregate. Writes stage in the target
directory and publish with fsync/rename; a premature commit cannot replace the
target. A rename whose durability cannot be confirmed is UNKNOWN. Atomic write
replaces the named file, including a symlink, rather than following that
symlink. Reads follow guest symlinks and require a regular file; they compare
device/inode/size/mtime/ctime across chunks and reject a changing file instead
of returning a mixed version. `abort_file_write(path, transfer_id)` explicitly
cleans an unfinished upload. Save a stable write request ID as the transfer ID.

Edge persists native intents in its existing request journal. Repeated IDs
with identical arguments attach/retrieve the original result; conflicting
arguments are rejected. Lost replies and failed commits remain UNKNOWN and
are never replayed. Stable IDs also cover upload chunks. No guest-side second
execution journal is introduced.

`session.stream(..., request_id=operation_id)` starts bounded background
collection at Edge. Subscribers read captured events using their returned byte
`cursor`; `session.events(operation_id, cursor=cursor)` reattaches without
resubmission. Detaching a subscriber does not cancel execution. The per-command
event spool is at most 8 MiB; overflow drops further output events while draining
the guest, and the final result sets `stream_truncated=true`. Final success is
exposed only after the journal commit. `session.query(operation_id)` reads that
same journal. `session.cancel(operation_id)` targets the exact active command,
and does not kill a later command. Cancellation of an executing command resets
its shell; queued cancellation leaves the existing session and other work intact.

MicroVM lifecycle admission uses the same durable registry: idle sessions can
be included in VM snapshots; active native calls/streams reject pause before
effects. An Edge restart with an interrupted native operation retires that VM
and preserves UNKNOWN, consistent with the existing interrupted-command
policy. Idle-session reattachment does not create a VM or shell. Edge graceful
shutdown waits for bounded stream collectors before releasing ownership.
Container memory snapshots remain unsupported. Old TB2 OpenEnv server images
do not acquire native features implicitly; use a prepared microVM/layered
container with the new agent.

Local tests execute the compiled agent with real shells and exercise SDK →
Edge → UDS, deduplication, commit faults, slow/detached subscribers and bounded
output. On 2026-10-10, installed-package Linux acceptance additionally verified
real vsock, idle-session snapshot restoration, layered-container deployment,
idle/paused Edge restart, and SIGKILL during a native stream: the request became
UNKNOWN, the old VM was retired and resubmission was rejected. A pinned TB2.1
task passed its official verifier and 32 fixed-code MBPP episodes passed their
expected scoring/cleanup checks. These application regressions retain the
existing one-shot adapter path; they do not migrate training to native sessions.
The [installation report](../reports/DSEC_V01_INSTALL_ACCEPTANCE.en.md) records the
conditions and scope. These checks do not establish production containment,
all-task correctness, high-concurrency performance or every storage combination.

### Formal concurrency model

The executable [TLA+ lifecycle model](../../verification/NativeLifecycle.tla)
specifies a **target concurrency contract**, not a claim that the current
runtime refines it. The separate
[signal model](../../verification/ShutdownSignal.tla) exposes the concrete
`ns_stopping = ns_run(...)` read/return/assignment window. Neither model belongs
to the installed runtime; Java/TLC is a contributor/CI dependency only.
The [official TLC tools](https://github.com/tlaplus/tlaplus) perform exhaustive
finite-state exploration. The
[runner](../../tools/check_concurrency_model.py) pins version 1.7.4 and checks
the jar's SHA-256 before executing it.

The lifecycle configuration has two stable request IDs and two VM incarnations
(`MaxEpoch=1`, epochs 0 and 1). Edge and guest actions interleave independently.
Start permits, completion/failure callbacks and cancel messages may be delayed;
callbacks may be observed repeatedly. Edge may crash/restart at any boundary.
There are no depth cutoffs, state constraints, random simulation or symmetry
reductions in this gate. Every reachable state of this finite abstraction is
visited by TLC. Increasing the bounds is a separate check, not an inference
that two requests cover arbitrarily many clients.

The authority is Edge's durable request/lifecycle record. Guest actions do not
read that record. A start permit is sent only after execution admission, so an
admitted-but-not-yet-started command must use the running-cancel path. Queued
cancel can finish immediately only before any permit exists. Running cancel
finishes after receiver-side revocation/termination; it does not undo shell
side effects. A completion already committed remains a completion.

`RequestStop` closes admission and enters `STOPPING`. A permit admitted earlier
may still start while shutdown is in progress. `STOPPED` is committed only after
confirmed VM death. Recovery creates a new incarnation after the old VM is
dead. Incarnation identity must fence state-changing callbacks at their write
boundary; it is distinct from snapshot generation and must not be inferred
from a reusable PID alone.

| Property | Meaning |
| --- | --- |
| `AtMostOnce` | A stable command request is started at most once in the modeled history. |
| `TerminalNoNewExecution` | No new start follows a committed success or confirmed cancellation. |
| `StoppedIsFinal` | Late callbacks cannot undo `STOPPED` in the same incarnation; explicit recovery changes incarnation. |
| `StoppedIsQuiescent` | A stopped sandbox has no live VM or active command. |
| `PausedIsQuiescent` | Pause has no admitted/running native work. |
| `UnknownNeverReadmitted` | An uncertain request is not admitted again. UNKNOWN does not mean the original command had no side effects. |
| `StartIsFenced` | A start is delivered only to the matching live incarnation. |
| `CallbackIsFenced` | Old-incarnation failure callbacks cannot mutate current state. |
| `StopIsMonotonic` | Once the signal requests shutdown, ordinary return handling cannot clear the flag. |

Assumptions are explicit: an exclusive Edge owner; atomic durable admission and
result transitions; no automatic retransmission of an admitted command;
transport delivery of one submitted start at most once; correct acknowledgement
of process termination. Client retries attach to the existing journal. The
model's `seen`, execution counters and settled counts are history monitors,
not a proposed second guest execution journal. It does not model Byzantine
guests, filesystem durability internals, arbitrary shell side effects, multiple
nodes, file-transfer races, session exclusivity, event-spool correctness,
network isolation or resource-lease cleanup. It checks safety only: no fairness,
eventual-delivery or eventual-completion claim is made. Deadlock checking is
disabled because terminal/quiescent states may stutter; this does **not** prove
deadlock freedom. Full linearizability needs invocation/response histories and
a refinement mapping, beyond these safety invariants.

| Model boundary | Code boundary / remaining correspondence |
| --- | --- |
| Admission before start; request-ID dedup | Existing `RequestJournal` and `NativeJobs.start`; EBUSY proves guest non-admission. Check the gap between Edge acceptance and guest admission. |
| Queued cancel / running cancel / confirmed terminal result | `NativeJobs.cancel` durably records a pending stream cancellation under the collector control lock. A queued intent prevents dispatch; an in-transit dispatch retries only cancellation for the exact operation ID. Intent acknowledgement is distinct from the final execution result. |
| Pause activity fence | `native_operation` and `LifecycleController.pause` already register/check native work under the sandbox lock. |
| Stop completion and late callback rejection | `native_operation` captures the durable process incarnation; late UNKNOWN callbacks only retire the same RUNNING incarnation. `LifecycleController.fail` preserves STOPPED. Container activity registration and stop admission share the existing lifecycle lock, with activity retained through native I/O. |
| Interrupted work becomes UNKNOWN; explicit recovery | Existing registry/journal recovery retires interrupted VMs. `native_incarnation` advances and is persisted before restore starts a replacement process; it is separate from snapshot generation. Old callbacks cannot fail or touch the replacement. |
| Monotonic signal | `ns_session` now writes `1` only when the return value requests stopping and otherwise makes no write. The real C-agent trace check covers this boundary and requires rejection of the previous assignment. A read-modify-write `stop |= returned` would still race with a signal. |

Unsafe variants deliberately violate queued cancellation, stopped-state
finality, old-incarnation fencing, UNKNOWN non-replay, start fencing and signal
monotonicity. CI requires an exact expected invariant violation and a TLC
counterexample, not merely a nonzero exit. The broad lifecycle variants are mutation checks of the target contract.
Targeted models below additionally replay actual implementation observations
and require corresponding code mutations to fail the canonical regressions.

Run `python tools/check_concurrency_model.py --fetch --out /tmp/dsec-model-check-unique`
with Java 11+, or provide the pinned jar with `--jar`. Output directories cannot
be overwritten. Each run preserves model/config copies, raw counterexamples,
state counts, tool/model hashes and `report.json`. Incomplete searches fail.
The targeted correspondence gates below map selected lifecycle actions to
code and replay counterexample orderings with deterministic barriers. They
check those mappings and complement model exploration; the broader lifecycle
model still lacks full implementation refinement.

#### Worked implementation correspondence: shutdown signal/result race

Run `python tools/check_shutdown_refinement.py --fetch --out /tmp/dsec-shutdown-check-unique`
with Java 11+ and a C compiler. The
[correspondence tool](../../tools/check_shutdown_refinement.py) compiles the real
`guest_native.c`, executes its persistent-shell command path and uses
[compile-time probes](../../verification/native_shutdown_probe.h) to deliver
SIGTERM at a chosen boundary. Probes and their environment variables are absent
from normal builds. Handler observations use fixed-size async-signal-safe writes;
no Python FSM decides which state transitions are legal.

| Observation | Model projection |
| --- | --- |
| Before the real `ns_run` call | `phase=RUN`, actual `ns_stopping`, initial return value/signal marker |
| After `ns_run` returns, before applying its result | `phase=ASSIGN`, actual return value and stopping flag |
| Inside the real SIGTERM handler, after setting the flag | `Signal`: actual stopping flag and signal marker; current phase retained |
| After the caller applies the result | `phase=IDLE`, actual stopping flag and cached return value |

`Return` allows either Boolean value because timeout, exit and shell reset can
request stopping without SIGTERM. This corrects an overly narrow initial
abstraction (`returned=stop`); `StopIsMonotonic` is unchanged. The safe caller is
`if (stop_run) ns_stopping=1;`, so normal return handling never writes zero.

The trace wrapper [ShutdownReplay](../../verification/ShutdownReplay.tla)
extends the same `ShutdownSignal` module and requires every consecutive observed
state to satisfy its original `Next`. `TraceConforms` rejects a prefix for which
the next observed state is not legal. It does not infer success from event names.
Five controlled scenarios cover no signal, signal before return, signal between
return/application, signal after application and explicit shell exit.

A required negative control replaces only the result-application statement with
the historical `ns_stopping=stop_run;`. Its real trace matches the unsafe model
and violates `StopIsMonotonic`; the identical trace is rejected by the safe model
through `TraceConforms`. Thus the gate detects a reintroduced assignment rather
than passing any compiled agent. The tool refuses to guess a mapping when the
marked code boundary is removed/refactored. Review that mapping and regenerate
traces when changing the implementation or model.

Each run retains original/mutated C sources, build logs, binary/probe/model hashes,
raw binary observations, projected JSON states, generated replay inputs and TLC
verdicts. Local validation on 2026-10-10 passed the five positive traces and two
required negative checks. The captured prefix covers this command-return/signal
window only; it is not all instruction-level interleavings, full runtime
refinement, a guarantee of shutdown latency or a liveness proof. Ordinary native
SDK/agent tests still verify execution and cleanup independently.


#### Implementation correspondence: queue, callbacks and container admission

Run `python tools/check_native_race_refinement.py --fetch --out /tmp/dsec-native-race-check-unique`.
The canonical [regression suite](../../tests/unit/test_native_races.py) executes
real `NativeJobs`, request journals and lifecycle methods. Queue tests use the
compiled C agent and actual Unix-socket commands. Host process and container
drivers are instrumented fixtures; this is local contract validation.

| Counterexample ordering | Implementation boundary and deterministic regression |
| --- | --- |
| Queue → cancel returns false/PENDING → dispatch executes | [QueueCancellation](../../verification/QueueCancellation.tla): `before_dispatch` barrier holds the collector; cancel must durably record intent, return true/PENDING, and finish queued/cancelled without creating the command's marker file. Reattaching the same ID cannot execute it. |
| Dispatch permit → cancel sees ENOENT → guest admits | A transport barrier holds the real stream before sending. The first exact-ID cancel reaches the guest and gets ENOENT. Release dispatch; cancellation must retry that ID and obtain a cancelled terminal result. It never retries the execution. |
| Result commit → cleanup throws → late cancel | After-commit exception injection must preserve the durable DONE result. Cancel returns false/DONE; evidence and the command effect remain unchanged. |
| Admit → stop completes → old transport exception | [NativeCallbackFence](../../verification/NativeCallbackFence.tla): hold the native operation, call the actual stop method, then deliver UNKNOWN. STOPPED and completed resource cleanup must remain final. |
| Admit epoch N → retire → restore epoch N+1 → callback from N | The real restore path advances/persists `native_incarnation` before replacement startup. Release the old UNKNOWN callback; the replacement stays RUNNING, alive, and unmodified. |
| Two native readers → stop / owner close | [ContainerNativeGate](../../verification/ContainerNativeGate.tla): two native contexts coexist. Stop returns ServiceBusy before lifecycle journal admission; owner close also returns ServiceBusy. Releasing both permits stop. |
| Stop holds admission → new native operation | Hold the instrumented backend stop method while the actual lifecycle lock is held. New native admission returns ServiceBusy and never reaches the endpoint; completed stop rejects later native work. |

The [trace wrapper](../../verification/RaceReplay.tla) evaluates each adjacent
state against the original model's `Next` (or an observation with no state
change). Python only projects observations. Queue projection uses the actual
phase, cancellation Event, persisted response and marker-file existence;
callback projection uses actual lifecycle state, captured/current incarnation
and activity map; container projection uses admitted request IDs, activity
removal, backend stop entry/completion and admission rejection. History monitors
record whether a stale callback itself retired a RUNNING replacement. They do
not require a replacement to stay healthy forever: unrelated crashes remain
legal. This distinction corrected an overstrong initial invariant exposed by
TLC, without hiding independent failures or narrowing them out of the model.

Four negative controls restore lost queued cancellation, remove callback guards
(two guards for stopped-state finality), or remove the container stop activity
check. These are reviewed code mutations, not parallel runtime implementations.
Each must produce a behavioral assertion failure in the same regression,
conform to the unsafe model, violate the specified property, and have its SAME
trace rejected by the safe model. The runner fails on source-boundary drift,
missing evidence, skips, parser errors and setup/cleanup errors. It retains source
copies/hashes, original/mutated code, JSON observations, regression logs, generated
TLA+ inputs/configuration, counterexamples and a combined report. CI requires
these checks alongside the exhaustive model and real C signal checks.

Cancellation success in the API acknowledges an intent, not guaranteed process
termination. A command already in transit can execute before cancellation wins;
its journal owns the final result. A queued cancellation prevents future sends.
Interrupted pending intents still reconcile as UNKNOWN; they are not relabeled
CANCELLED or automatically replayed. Container stop remains retryable while
native work is active, consistent with existing one-shot container execution.

Local validation on 2026-10-10 covers seven deterministic regressions, five safe
implementation traces and four required faulty-code traces. The correspondence
runner has 22 gates. Full model checks enumerate finite states without fairness
or timing assumptions. These results do not prove all instruction-level races,
linearizability of the whole system, eventual cancellation, physical child
termination, filesystem/lease cleanup, or Linux deployment acceptance. The isolated Linux acceptance below validates these targeted runtime changes;
broader composition and production release gates remain separate.


#### Real Linux acceptance (2026-10-10)

The [Linux acceptance tool](../../tools/verify_native_races.py) passed seven
controlled checks on the experiment host with the installed candidate wheel:
VM/container queued cancellation without any execution send; exact-ID cancel
when dispatch is in transit; completed stop followed by a late UNKNOWN callback;
old-incarnation callback after real snapshot restore; and both container
native/stop admission orders. Container admission reached the real native agent,
not an emulated endpoint. The callback tests executed a real guest command,
then deliberately withheld/lost its response at a host barrier. They used real
Firecracker processes, snapshot files and Linux pidfd attestation/adoption.
EROFS boot/layer artifacts were immutable, private writable disks were isolated,
and the recovered guest successfully executed commands before and after Edge
ownership adoption. Its idle deadline was unchanged by the old callback.

The installed core wheel SHA-256 was
`4e6851aaa5788423dd904f442c0760d7c7bd8f0478b4723fd71f2ba26f686769`.
The physical-run source manifest SHA-256 was
`c7b83a4479468b08c301d11eb76d5ff1f6c55b543bf04a069877942904c96c8b`.
The guest C source SHA-256 was
`6df612523d2e39dfd188955b5155bc960f91dd2991b7a565e8f3ad7b2bcc59fb`.
Raw reports and logs live in the dedicated
`/home/xiaoxiaohu/dsec-race-20261010` acceptance directory. The physical verdict
is `runtime-r4/report.json`; model, signal, mutation and Linux unit evidence is
in `evidence/`. Failed earlier runs are preserved. Their two harness defects
were conflicting default arguments on same-ID stream reattachment and an
unreaped original child handle when both Edge owners lived in one OS process;
no runtime guard was relaxed. The harness reaps only its exact original Popen
child after the adopted VMM stops, and still requires `/proc` disappearance.

Linux reran 16 exhaustive model gates, seven C-signal correspondence gates and
22 implementation/mutation gates. The full unit suite ran 285 tests: 284 passed
and one legacy experiment-alias test was inapplicable to the exported checkout.
Its optional TB2.1 test package was built from the same snapshot solely to run
the application-related regression collection. The physical acceptance does
not need TB2.1 task downloads, a model or GPU. Six real backend traces are replayed
against the same three original TLA+ specifications using
`check_native_race_refinement.py --observed /path/to/report.json`.

Cleanup verified six VMM process incarnations no longer existed, no owned Docker
containers/private directories remained, no native activity remained, and no
cleanup error occurred. Existing Docker/Grafana/Prometheus and DSec user services
were not restarted by acceptance. This is a targeted single-host contract gate;
it does not establish unbounded linearizability, every cancellation delivery
window, child termination under arbitrary faults, 3FS/ublk recovery, network
containment, scheduler lease correctness or a performance claim.

## Aether/chronus execution extensions

The [execution and isolation contract](EXECUTION_ISOLATION.md) records the current
Linux proxy/session split and unfinished production backend paths. New Edge and
guest capability checks gate `native-aether-chronus-v1` session-scoped files and
optional `native-http-v1` HTTP requests. Legacy sandbox-wide files remain
compatible. `session.request` returns binary body, raw response headers, HTTP
status, timeout/truncation and session-reset flags; unknown outcomes remain
query-only. These extensions use the existing Edge request journal.


### Rollout API ownership and verification

`RolloutAPIClient` reuses existing `ScheduledSandbox` semantics through the API;
`ScheduledDSecClient` remains a local development/internal transport. TB2.1/Miles
and MBPP/verl use the same API config and keep the existing policy messages,
step/action identities, rewards and evidence. No task or trainer code enters the
core API. Only operator-registered evaluators execute in the worker.

IAM admission uses the public rollout ID as its stable key, canonical creation
arguments and immutable Node. The gateway hashes project/rollout IDs into worker
IDs, validates the worker's task/profile/resources and physical owner before
effects, and projects only identity fields back to public IDs. It never rewrites
model messages or command output. The API stores no trajectory; worker journals
remain execution truth.

`RolloutDispatch.tla` bounds one admitted episode, delayed arrival, one worker
record and a separate cleanup proof. `Dispatch` maps to the IAM CAS before the
one worker create send. `ReachWorker` maps to durable worker reservation;
`ProveCleanup` maps to a matching sandbox status, and `Release` to IAM's CAS.
Controlled regressions cover concurrent duplicate creates, committed reply loss,
the dispatch-before-worker-record gap, namespace isolation, preserved verifier
failure and refused cleanup release. Required code mutations re-send a create
or remove its cleanup guard; they must fail those regressions, alongside the
model's replay and unproved-release counterexamples. Existing `ClusterAdmission`
continues to check quota and immutable placement. Neither model proves arbitrary
implementation refinement, hash collision freedom or cluster availability.
