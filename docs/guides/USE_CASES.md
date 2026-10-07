# Use cases and runnable entry points

DSec provides sandbox execution, scheduling, storage and recovery. Applications
provide tasks, agents and verifiers. Installing the core does not install a
benchmark, start a model or select an RL algorithm.

The table distinguishes shipped entry points from historical experiments and
from complete application launchers that are still missing.

| Use case | Shipped entry | Current scope |
| --- | --- | --- |
| UC-01: TB2.1 task execution and official verification | Optional `apps/tb21` package; fixed-trajectory acceptance tool | Task preparation and representative official-verifier execution validated. All 89 task files were checked; not all 89 tasks have passed a model episode. |
| UC-02: API-driven agents without a GPU | Framework-independent environment and TB2.1 task adapter | The task/environment interfaces are shipped. A standalone full API benchmark CLI is not yet included; bring a model client and agent loop. |
| UC-03: Miles RL rollouts | Packaged Miles agent/generate/reward adapters | Short real GRPO integration validated. Trainer, model and hardware-specific GPU patches are separate; no complete portable training launcher is claimed. |
| UC-04: Repeated episodes from prepared state | Baseline sealing/fork API; installed fork acceptance tool | State isolation, actual snapshot recovery and shared-object cleanup validated on an OverlayBD-backed case. |
| UC-05: Custom tasks outside TB2.1 | Task adapter protocol; counter task and `dsec-host smoke` | Non-TB execution, scoring, restart recovery and cleanup validated. |
| UC-06: MBPP execution reward with verl | Optional `apps/mbpp` preparation CLI and native reward hook | Preparation candidate; fixed data splits and reward/lifecycle regressions implemented. Real VM and verl update acceptance pending. |

Start with the [deployment quickstart](QUICKSTART.md). Commands below assume
the core is installed in the active Linux venv and that an isolated instance
has its external guest/storage/network dependencies configured. Paths under
`/data` are examples to replace with your own paths. Tools under `tools/` ship
in the source archive, not as wheel console commands. Invoke them with the
installed venv's `python -I` to avoid loading runtime code from the checkout.
Use a new output filename for every acceptance run.

## UC-01 — TB2.1 task execution and official verification

Use this case to validate an agent environment before involving a model or RL.
Install the optional application only when needed:

```sh
python -m pip install ./apps/tb21
dsec-tb21 stage --repo /data/terminal-bench-2-1 \
  --tasks openssl-selfsigned-cert --out /data/tb21-case
dsec-tb21 check --suite /data/tb21-case
dsec-tb21 register --suite /data/tb21-case \
  --catalog /data/prepared-environments.json --out /data/tb21-catalog.json
```

The supplied task checkout must be the pinned revision
`7131e4375048a0e408a8fb404b5f499d726b695b`. Registration consumes an existing
validated environment catalog; it does not build disks. If one is unavailable,
use the explicit `prepare-image` recipe in the
[application guide](../../apps/tb21/README.md), including its local image/tool and
device-count requirements. Neither command implicitly downloads task images.

Configure an isolated instance with that catalog, the staged tasks and the
verifier disk as documented in the application guide and
[host configuration](DSEC_HOST_CONFIGURATION.md). Once it is ready:

```sh
python -I /data/dsec-source/tools/verify_installed_task.py \
  --config /data/tb21-host.json --adapter tb2 \
  --trajectory /data/dsec-source/tools/fixtures/tb21-openssl.json \
  --expect-score 1 --out /data/evidence/tb21-fixed-trajectory.json
```

The shipped fixture contains a known successful solution. It is strictly a
harness acceptance input, not model output, model-quality evidence or an RL
training trajectory. The tool executes the staged task's official verifier,
saves the verdict/observations and attempts sandbox/lease cleanup, preserving
errors in the output JSON. Raw verifier logs are saved by the worker.

Add `--restart-services` only on the dedicated acceptance instance; it restarts
that instance's two user services and checks live/paused recovery and committed
action deduplication. It does not restart the host Docker daemon.

## UC-02 — API-driven agents without a GPU

Use the same TB2.1 application with an external model API to evaluate agent
quality. GPU training is not needed. The integration boundary is:

1. Construct `TB2DSecEnvironment` from the staged tasks and registered catalog,
   open `ScheduledDSecClient` for the worker socket and allocate a rollout ID.
2. Create `DSecAgentEnvironment` and call `reset(policy_prefix)`. The task
   instruction is appended by the task adapter; the returned dialogue is the
   starting model context.
3. Call your model API, parse its reply and submit one
   `EnvironmentAction.shell` with stable step/action IDs. Pass the original
   assistant message as `policy_message`, then use the returned dialogue for
   the next model turn.
4. Call `evaluate()` for the official verdict and `stop()` to release the
   sandbox. Record raw model replies, model configuration, termination reason,
   command timings and verifier errors separately from the task score.

An unknown action outcome must be reconciled by identity, not blindly executed
again. The caller owns model credentials, reply parsing, stopping rules and
sampling. External API responses are not automatically valid RL trajectories;
training additionally needs exact tokens, logprobs, masks and weight versions.

These interfaces are installed, but a complete API-client/parser/task-sweep
CLI has not yet been exported as a supported example. Historical API harness
experiments are not a substitute for that missing launcher. See the
[environment contract](../architecture/AGENT_ENVIRONMENT_CONTRACT.md); exporting this application
driver belongs with the benchmark entry points in roadmap `RM-104`.

## UC-03 — Miles RL rollouts

The DSec side provides the task environment and trusted verdict; Miles owns
generation sessions, TITO and parameter updates. Install the optional Python
dependencies and configure the sandbox connection:

```sh
python -m pip install '.[miles]'
export DSEC_ROLLOUT_WORKER_SOCKET=/srv/dsec/tb21/worker/worker.sock
export DSEC_TB2_TASKS_DIR=/data/tb21-case/tasks
export DSEC_TB2_ENVIRONMENT_CATALOG=/data/tb21-catalog.json
```

Add these options to your separately configured Miles launcher:

```text
--custom-agent-function-path dsec_adapters.miles_dsec_agent_function.run
--custom-generate-function-path dsec_adapters.miles_dsec_generate.generate
--custom-rm-path dsec_adapters.miles_dsec_generate.reward_func
```

Use task metadata with `task_id` and `dsec_environment: "tb2"`. Keep the
generate/reward guards: a missing official verdict must not become a false
zero reward. A trainer session that loses its token/logprob history cannot
train on recovered dialogue text alone.

The latest accepted case used Qwen3.5-4B Thinking, two GRPO groups of two
rollouts, 32768 response and 65536 context limits. Rewards were `[1,0]` and
`[0,0]`; the first group produced a nonzero update. This is integration
evidence, not a claim of learning gains. The historical single-16-GiB-GPU run
used separate training-side patches; they are not installed by DSec and the
unmodified trainer is not guaranteed to fit that hardware. See
[RL adapter boundaries](RL_FRAMEWORK_ADAPTERS.md) and
[the latest training acceptance](../reports/DSEC_V01_GRPO_ACCEPTANCE.md).

There is no complete portable model-download/GPU-training launcher in this
preview. The optional MBPP application prepares a native verl execution-reward
hook; it is not yet a validated live training use case. Stateful multi-turn
verl and Uni-Agent adapters remain planned. See [MBPP preparation](../../apps/mbpp/README.md).

## UC-04 — independent episodes from prepared runtime state

Use this case when many episodes share expensive initialization, such as a
started service or populated workspace, but require independent writable state
and fresh agent history. A trusted preparer seals the baseline; new episodes
reference its immutable disk layers and restored memory state.

The installed-system acceptance tool exercises an in-memory HTTP counter,
independent writes, fresh history, snapshot recovery and last-reference cleanup:

```sh
python -I /data/dsec-source/tools/verify_installed_fork.py \
  --config /data/tb21-host.json \
  --trajectory /data/dsec-source/tools/fixtures/tb21-openssl.json \
  --out /data/evidence/prepared-episodes.json
```

This recipe needs a prepared OverlayBD-backed TB2.1 environment and capacity
for the baseline plus a branch. It checks two branches sequentially, not
parallel throughput. `--restart-services` adds restart recovery on the isolated
instance. The fixture still serves only harness acceptance.

The baseline must not contain reference answers or another policy's history.
Prepared memory forking is our extension; it is not the paper's disk-only
`pack_diff` workflow. Account for baseline creation, retained memory/disk and
reuse count when assessing whether preparation amortizes. See the
[environment contract](../architecture/AGENT_ENVIRONMENT_CONTRACT.md)
and [cost report](../reports/DSEC_VERITY_PILOT_REPORT.md).

## UC-05 — custom tasks without TB2.1

The quickest non-TB example is the shipped counter smoke task:

```sh
dsec-host --config /data/counter-host.json smoke \
  --out /data/evidence/counter.json
dsec-host --config /data/counter-host.json status
```

Follow the quickstart to build a fresh non-TB guest and start this instance.
No TB2.1 package, task data, model or GPU is needed. The smoke uses the installed
`CounterDSecEnvironment`, executes a shell action, obtains an exact-value
verdict and checks cleanup. On its dedicated instance, add
`--restart-services` to exercise live and paused service restart recovery.

For your own application, implement `TaskEnvironmentAdapter.prepare`, `step`
and `evaluate`, and use the same `DSecAgentEnvironment` lifecycle. The adapter
owns instructions, work directory, resource demand and verifier semantics;
the caller owns the agent/model loop. A new task adapter can be used directly;
using it through Miles additionally requires explicit plugin registration and
a matching verdict guard. The core does not restrict direct adapters to TB2.1.

## Comparing applications on Docker and DSec

The [comparison protocol](../architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md) defines matched
tasks, trajectories, cache/storage conditions and full backend costs. The
[r5 cost report](../reports/DSEC_VERITY_PILOT_REPORT.md) is historical pilot evidence,
not a shipped general benchmark command. Unified application sweep and cost
drivers remain roadmap `RM-104`. Preserve failures and distinguish functional
acceptance, model quality, RL integration and system performance.
