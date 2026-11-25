<a id="rl-框架接入"></a>

# RL framework integration

**English** · [简体中文](RL_FRAMEWORK_ADAPTERS.md)

DSec provides the persistent task execution environment. The training framework owns inference, tokens and logprobs, trajectories and the optimizer. See the [agent environment contract](../architecture/AGENT_ENVIRONMENT_CONTRACT.md).

<a id="已有-miles-接入"></a>

## Miles integration

After installing the core's optional client dependencies, configure Miles to use the following installed modules.

```sh
python -m pip install '.[miles]'
```

| Miles option | Entry point |
| --- | --- |
| `--custom-generate-function-path` | `dsec_adapters.miles_dsec_generate.generate` |
| `--custom-agent-function-path` | `dsec_adapters.miles_dsec_agent_function.run` |
| `--custom-rm-path` | `dsec_adapters.miles_dsec_generate.reward_func` |

These entry points supplement the full model and training configuration. Format prompt data as Miles message lists and include `task_id` and `dsec_environment` in metadata. The environment appends task instructions at reset. Keep solutions and verifier contents out of policy prompts.

Point `DSEC_API_CONFIG` at the API client configuration. The training process does not access a worker socket. For TB2.1, prepare tasks, register the environment catalog and configure verifier artifacts explicitly. See the [optional application](../../apps/tb21/README.md). Installing the core downloads neither tasks nor models.

<a id="轨迹和奖励约束"></a>

## Trajectories and rewards

The agent loop records the raw reply, parses and submits an action, then appends the observation to the multi-turn context. The Miles session server records TITO. The generation adapter checks the tokens, logprobs and masks against the session prefix.

Only trusted task verdicts may become training rewards. Reject infrastructure failures and samples missing scoring evidence. A valid zero means that the model failed the task. If the training process restarts and loses its TITO, reject the episode for training even if its sandbox recovers successfully.

To save job evidence, explicitly set the following directories.

- `DSEC_MODEL_OUTPUT_DIR` stores raw model replies.
- `DSEC_TITO_AUDIT_DIR` stores per-turn tokens, logprobs and request records.
- `DSEC_EPISODE_REGISTRY_DIR` records episode IDs owned by the job for targeted cleanup.

<a id="训练侧兼容修复的边界"></a>

## Training-side compatibility fixes

A short Qwen3.5-4B Thinking GRPO run passed using a pinned Miles image on a single GPU. See the [latest GRPO acceptance](../reports/DSEC_V01_GRPO_ACCEPTANCE.en.md). The run used training-side fixes for decoder memory release, single-rank NCCL lifecycle, LoRA-only NVMe storage and chunked output projection. These fixes are external experimental dependencies. The DSec core wheel and default source archive exclude them.

The current API entry passed a fixed-response Miles agent loop and TB2.1 official verification. Historical GPU training used the former local worker entry. This migration did not rerun GPU training, and the old full-suite recipe has yet to be migrated to the API configuration. Short training does not establish success on all 89 tasks, capacity for every long context or improved model performance. The upstream draft for single-rank NCCL is [Miles #3914](https://github.com/radixark/miles/pull/3914).

<a id="当前-api-训练接入"></a>

## Current API training entry

Prepare tasks, environments and verifiers on the Node, deploy the [API/Node/worker bridge](CLUSTER_DEPLOYMENT.md#remote-native-sdk) and give the API client configuration to the trainer and reward processes. The three Miles adapter entry points and `DSEC_API_CONFIG` are listed above. Select the task using `task_id` and `dsec_environment: "tb2"` in metadata. The configuration carries the endpoint, project and references to private token/CA files. Keep secrets out of the training arguments.

First check that the prepared task executes, receives an official score and releases its resources through the API.

```sh
python -I /data/dsec-source/tools/verify_rollout_api.py \
  --api-config /run/dsec/api-client.json \
  --tb21-tasks /data/tb21/tasks --tb21-catalog /data/tb21-catalog.json \
  --tb21-trajectory /data/dsec-source/tools/fixtures/tb21-regex-log.json \
  --out /data/evidence/api-harness-unique
```

The example requires the Node to register `default` and `tb2-regex-log`, provide a boot template with the right layer count and timeout budget, and configure verifier networking or a complete cache. Fixed replies test integration only and are unsuitable as training samples or model-quality evidence. Provide the full Miles model, sampling and GPU configuration in an independent training environment. There is no new full-suite API training launcher. The current MBPP/verl launcher accepts `--api-config`. See its [application guide](../../apps/mbpp/README.md).

<a id="历史-qwen35-4b--全量-tb21--grpo-recipe"></a>

## Historical Qwen3.5-4B GRPO recipe for the full TB2.1 suite

The `20261006-r6` release recipe below uses `worker_socket` and a pinned earlier core. It records the original clean deployment. Renaming a field to `api_config` does not migrate that artifact to the current API-only SDK. For an API integration, use the previous section and keep UDS endpoints private to the Node. The historical launcher and full GPU rerun still need migration.

<details>
<summary>Expand the historical recipe, configuration and acceptance criteria</summary>


This procedure uses a separate optional trainer artifact. GPU memory patches are excluded from the DSec core wheel and default source release. Acceptance used Linux x86_64 and one 16 GiB NVIDIA GPU. This gives no memory guarantee for unpatched Miles. The recipe starts from the original model with fresh episodes, reuses explicitly supplied read-only environment artifacts, and uses no previous trajectories, ready VMs or training checkpoints.

<a id="1-安装和准备任务"></a>

### 1. Install and prepare the tasks

Follow the [quickstart](QUICKSTART.md) to install the core and build tools in a fresh venv, then install the optional application.

```sh
.venv/bin/python -m pip install --no-build-isolation . ./apps/tb21
git clone https://github.com/harbor-framework/terminal-bench-2-1.git "$HOME/tb21-source"
git -C "$HOME/tb21-source" checkout 7131e4375048a0e408a8fb404b5f499d726b695b
.venv/bin/dsec-tb21 stage --repo "$HOME/tb21-source" --all --out "$HOME/tb21"
.venv/bin/dsec-tb21 check --suite "$HOME/tb21"
.venv/bin/dsec-tb21 register --suite "$HOME/tb21" \
  --catalog "$HOME/prepared-environments.json" --out "$HOME/tb21-catalog.json"
.venv/bin/python -m build --wheel --no-isolation --outdir dist/core .
```

Supply the catalog for 89 prepared environments, their read-only layers and roots, the VMM/kernel, and the verifier disk and manifest. Registration does not construct these artifacts. The device-count and GPU-task limits of `prepare-image` still apply. This command cannot rebuild all 89 environments from scratch.

After transferring artifacts, update all absolute paths in the catalog. The OverlayBD root descriptor includes lower paths. After editing it, recompute its `image_sha256` while keeping content hashes for the actual lowers and EROFS layers unchanged. Copying the catalog alone is insufficient. This recipe can use local read-only layers. 3FS is optional. The model and trainer image are also separate inputs.

<a id="2-部署独立的联网实例"></a>

### 2. Deploy an isolated networked instance

Follow the [host configuration guide](DSEC_HOST_CONFIGURATION.md) to create an instance with a fresh state directory. Configure the registered catalog, complete task suite, verifier and proxy. Preserve task-specific verifier and DAX settings. Size the scheduler budget for the largest single task selected. `capacity` and actual concurrency may be lower than the number of samples per task.

Configure `network.max_slots` and `dns` before generating privileges for administrator installation. If `helper` is omitted, the entry point uses the same per-instance path as the installer. You can also specify it explicitly.

```sh
.venv/bin/dsec-host --config host.json render-privileges \
  --user "$USER" --slot-offset 6144 --out reviewed-privileges
sudo python3 reviewed-privileges/install-privileges.py --apply
```

Avoid overlap with any existing instance's address range. The external OverlayBD/ublk service must allow writes in the new state directory and access to the artifacts. The network installer does not provision storage. After installation, use the generated `host-with-network.json` and follow the quickstart to render and start the two user services, then run `doctor --live`. Configure and install the helper before attempting a networked startup check.

Before training a model, use `tools/verify_installed_task.py` and the supplied fixed trajectory to check creation, command execution, official scoring and cleanup on one task. The known-answer fixture is exclusively for independent acceptance. Exclude it from training data. Full training still uses the official `tests/test.sh`. Verifier failures must not become zero rewards.

<a id="3-获取独立训练-recipe"></a>

### 3. Obtain the separate training recipe

```sh
mkdir -p "$HOME/dsec-training/recipe"
gh release download v0.1.0-dev.0 --repo QingdiMeng/dsec-reproduce \
  --pattern qwen35-tb21-grpo-recipe-20261006-r6.tar.gz --dir "$HOME/dsec-training"
sha256sum "$HOME/dsec-training/qwen35-tb21-grpo-recipe-20261006-r6.tar.gz"
tar -xzf "$HOME/dsec-training/qwen35-tb21-grpo-recipe-20261006-r6.tar.gz" \
  -C "$HOME/dsec-training/recipe"
```

The artifact SHA-256 is `7636f1cfabb358e605bec86d584659eae8b065e5322cdc9b193b3db9238b1108`. Its `source-manifest.json` pins each source file. The artifact includes MIT and Apache-2.0 license texts and NOTICE. Download the pinned release using a configured GitHub CLI or transfer the same verified artifact offline.

Supply a full Qwen3.5-4B Hugging Face snapshot and cache this trainer image: `radixark/miles@sha256:45ae7833f97a01369dbd8f336784830a87d94587bc010c7b8065291cc15cd7ca`. Mirror repository names may differ. The digest must match. Use the full RepoDigest available locally in the configuration. The recipe uses `--pull never` and does not download or modify the trainer image during the run.

<a id="4-配置预检和训练"></a>

### 4. Configure, preflight and train

Create your own configuration from `config.example.json` in the artifact. Replace these seven absolute paths: `model`, `core_wheel`, `suite`, `catalog`, `verifier_manifest`, `worker_socket` and `output_root`. **Remove the single-task `tasks` field from the example** so task selection uses the full suite. Add the following settings.

```json
{
  "groups": 89,
  "samples_per_prompt": 4,
  "concurrency": 2,
  "timeout": 172800
}
```

Merge these fields into the complete configuration. If retaining `trainer_image`, use the actual local RepoDigest. One pass over 89 tasks produces 356 episodes in 89 GRPO update groups. The four samples of a task use independent sandboxes and trajectories. `concurrency=2` limits concurrent work without reducing the four samples per group. For multiple passes, set `groups` to a multiple of 89. The same model service serves the entire job.

```sh
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" plan --config full-tb21.json
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" doctor --config full-tb21.json
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" train --config full-tb21.json
```

Before starting full training, confirm `task_count=89`, `groups=89` and `episodes=356` in the plan. Preflight requires one idle GPU, a running scheduling worker, free ports and at least 40 GiB of free disk. Training uses Megatron LoRA rank 8/alpha 16, MLP fc1/fc2 and a microbatch size of 1, with alternating inference and training offload. `enable_thinking=true` controls Thinking. The coding preset is `temperature=0.6, top_p=0.95, top_k=20, min_p=0, presence_penalty=0, repetition_penalty=1`, with a 32768-token response cap and 65536-token context cap.

By default the agent allows 16 turns, 120 seconds per command and 1200 seconds per episode. Configure `max_turns`, `command_timeout_ms` and `episode_timeout` as needed. A command may run for at most 900000 ms and cannot exceed the episode budget. Installing many dependencies can exceed 120 seconds. On timeout the command terminates. A later `sleep` does not resume a terminated installer. To allow longer installations, set `command_timeout_ms=600000`, `episode_timeout=3600` and `max_turns=32` explicitly in a new job. These are integration parameters. Official task agent and verifier deadlines are in each `task.toml`. Runs under these integration settings do not establish an official full-suite benchmark score.

The DSec episode clock starts when the sandbox is ready. On expiry, record a zero reward for the budget and continue with the original GRPO group. An outstanding model request is allowed to finish so TITO is preserved. Its completion may exceed the deadline, and a reply arriving after expiry is not executed. Budget zeros carry `reward_source=episode_budget` and are recorded separately from official verifier scores. Missing tokens/logprobs or unknown execution still invalidate a sample. Never convert an infrastructure failure into a model zero.

<a id="5-检查结果与失败"></a>

### 5. Inspect results and failures

The launcher prints `run_directory` at startup. `progress.json`, `resource-samples.csv` and `resource-summary.json` update every five seconds without loading the model or a checkpoint. Replace `/path/to/run` in the example with the directory actually printed. Query once or watch continuously.

```sh
.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" status --run /path/to/run
watch -n 5 '.venv/bin/python "$HOME/dsec-training/recipe/recipe.py" status --run /path/to/run'
```

Progress distinguishes valid official verdicts, positive scores, zeros, aborted/uncertain episodes, complete task coverage and completed optimizer steps. It shows the current task, command or verifier phase, scheduler wait reason and checkpoint count. A valid verdict does not prove admission into training. Nonzero gradients are reported separately. `status` marks a running snapshot that has not refreshed in 15 seconds. A failed exit retains final status and observation errors. The router's `/health_status` returns each worker's quarantine state, forwarded requests in flight, consecutive health failures and error class. Progress records those states, the latest infrastructure error and time since the last log message. A still-running process with an infrastructure failure is not displayed as a healthy rollout.

In the pinned Miles image, health probes originally shared the inference HTTP connection pool. With concurrency 2, two long requests could exhaust the pool. Health requests then hit `PoolTimeout` acquiring a connection, and three consecutive failures quarantined a worker that was still reachable directly. The recipe gives health checks a separate pool while retaining inference concurrency limits and quarantine for real faults. Re-admission after quarantine is deliberately manual because model weight version synchronization must be checked.

The image's original rollout driver caught exceptions from custom generate and continued drawing tasks. When a strict task sweep uses `runner.generate_complete_group`, the recipe cancels and waits for sibling samples, preserves the cause and reports the exception to the job owner. It does not substitute later tasks for a failed one. Other sampling modes retain existing behavior. On exit, cleanup waits for the trainer and Ray to stop, then reclaims running sandboxes and queued creations using the job's ownership records. Worker records and cleanup results are retained. These are integration fixes in the optional trainer recipe. They add no core Miles dependency.

The metric scopes are the whole GPU, host CPU normalized to 100% across all logical CPUs, the training container cgroup, and the PSS of this job's DSec VMMs. Container memory excludes the VMMs. VMM PSS excludes shared storage services. GPU/CPU/PSS peaks and phase durations are sampled estimates. Container `memory.peak` comes from the kernel. I/O is cumulative training-container cgroup bytes. Network is cumulative bytes for all workloads on the host default interface. The disk gauge reports space remaining. These figures cannot be interpreted as dedicated per-sandbox disk or network utilization. Shared-worker scheduling totals are labeled instance-wide. Wait reasons and completed wait durations are filtered by the job's rollout IDs. Missing samples cannot pass final acceptance as zero resource usage.

Each training invocation creates a fresh output directory recording the configuration, actual command, raw replies, TITO, verifier logs, resource samples, worker records and LoRA checkpoints. An infrastructure failure terminates the group and retains evidence. Failed tasks cannot be replaced to fill the expected count. Valid zeros are usable in GRPO. A group of four identical rewards, all zero or all one, can legitimately have zero advantage and gradient even when the model ran.

Success requires `status=passed` in `recipe-result.json`, complete task coverage and valid trajectories. Inspect gradients and adapter changes separately. Full-suite mode requires exactly four training samples for **each** task, beyond the total of 356. `learning_update_observed` proves only that an update occurred. It cannot establish learning gains. On normal container exit or a handled exception, the job directory is returned to the host user who started training so raw replies and failure evidence remain readable. Forced container termination may require administrator permission repair. Preserve the failed output directory, diagnose the fault and correct it. This version has no single-command automatic checkpoint resume.

</details>

<a id="接入其他训练框架"></a>

## Integrating other training frameworks

The first optional verl integration is the [MBPP application](../../apps/mbpp/README.md). It uses the native `reward.custom_reward_function` and `RolloutAPIClient` through the API Server and Node worker to execute Python and the three official tests in an isolated MicroVM. Data conversion pins the original MBPP train/validation/test splits and omits reference answers. Real MicroVM acceptance covered scoring, isolation, timeouts and cleanup. Qwen3.5-2B also completed one full GRPO epoch over 374 tasks with eight samples per task, 2992 samples and 187 updates. This single-turn code generation reward path does not establish multi-turn Agent Loop support. Core installation includes no verl, MBPP data or model. Full training used the old local transport. The current API scoring and concurrency paths passed acceptance, without a repeat of the training before/after comparison. The interface check used verl commit `8718ca30a3f002f93b7c4fd99b9b2506718681bc`.

In that completed case the same 500 test tasks received eight samples each before and after training. Sample success increased from 39.775% to 42.275%. The share of tasks solved at least once in eight attempts rose from 64.8% to 68.4%. The run used Non-Thinking mode, an 8192-token response limit, LoRA rank 8/alpha 16, SGLang inference and native FSDP2 training. See the [application guide](../../apps/mbpp/README.md) for commands, chunking of temporary logprob memory and evaluation-only recovery. The [case report](../reports/MBPP_VERL_FIRST_USE.md) documents evidence and limits.

Start acceptance with correct, wrong and timing-out programs and inspect scoring evidence. Then run model rollouts and perform a GRPO update on eight independent samples of the same task. Verify the model and training configuration separately. An untested verl Qwen3.5-4B path has no 16 GiB fit guarantee. Stateful multi-turn Agent Loop, cross-framework TB2.1 and Uni-Agent integrations remain open. New adapters should own framework sampling and trajectory format, retain stable rollout/action IDs and reject samples lacking valid scoring or TITO evidence. Keep RL algorithms out of task plugins.

Fork prepared state to start independent episodes. Pause and resume to continue an existing episode's original state. Samples in one GRPO group need independent writable state and trajectories. A resumed episode does not count as another independent sample.
