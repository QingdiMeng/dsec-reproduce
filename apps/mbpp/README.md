# MBPP with DSec and verl

This optional application prepares the original MBPP dataset and implements
verl's native async `custom_reward_function`. DSec executes generated Python
and the three official assertions in a fresh scheduled microVM for each sample.
verl owns generation, token IDs/logprobs, GRPO and model updates. No OpenEnv,
AgentENV or SandboxFusion service is required by this adapter.

**Status:** Qwen3.5-2B completed one native verl GRPO epoch over all 374 original
training tasks (187 updates, eight responses per task). Complete before/after
evaluations each cover all 500 original test tasks with eight responses per
task. Live microVM reward/deadline/isolation acceptance and fixed-candidate
8/16/32/48 execution concurrency also passed.
This is a single-turn code-generation application, not a validated multi-turn
verl Agent Loop or a claim of production isolation against malicious graders.

## Completed case and results

Use this case to train a code-generation policy with sandbox execution rewards.
SGLang generates responses; verl's native FSDP2/GRPO path updates the LoRA
adapter; the DSec scheduler creates an independent networkless microVM for
each executable response, runs Python and the tests, and stops the VM. All
episodes share immutable EROFS image layers and have private writable disks.
This case validates shared-image execution and resource admission; it does not
exercise prepared-memory forks, DAX or 3FS.

The tested host used one 16-GiB RTX 4080, Qwen3.5-2B in Non-Thinking mode,
LoRA rank 8/alpha 16, an 8192-token response limit and 32 effective generation
slots. Each executable sample used one guest CPU and 512 MiB of configured
guest memory. These are tested settings, not universal capacity guarantees.

| Metric | Before training | After training |
| --- | --- | --- |
| Successful responses / 4000 | 1591 | 1691 |
| Mean sample success (pass@1 estimate) | 39.775% | 42.275% |
| Tasks solved at least once in eight responses | 324/500 (64.8%) | 342/500 (68.4%) |
| Length-limited responses | 86 | 108 |
| Model-format zeros | 105 | 123 |

The mean sample success gain is 2.5 percentage points (paired-task bootstrap
95% interval: +0.975 to +4.0 points); 41 tasks became solved and 23 became
unsolved. This is one run scored on the three original public assertions,
with independently sampled before/after responses. It is not a hidden-test
result or a Docker/DSec performance comparison. The completed replacement
post-evaluation has no generation, infrastructure or cleanup errors: all 3877
created VMs stopped; the 123 format zeros created no VM. The original failed
post-evaluation is excluded, and recovery performed no new training updates.
See the [case report](../../docs/reports/MBPP_VERL_FIRST_USE.md) for evidence,
the logprob-memory fix and the explicitly marked final-training-dump recovery.

Follow the sections below in order: install the optional application, prepare
the pinned data, build/start the Python sandbox, run fixture acceptance, then
use the complete-epoch training and comparison commands. Installing DSec alone
does not install MBPP, verl or model weights.

## Prepare explicit dependencies

Install the DSec core and this application separately:

```sh
python -m pip install .
python -m pip install './apps/mbpp[parquet]'
```

Fetch `mbpp/mbpp.jsonl` explicitly from the
[official dataset](https://github.com/google-research/google-research/tree/e49bbfe381c9c0e564b937f1c4e163a2273c65cc/mbpp).
The application rejects any bytes differing from SHA-256
`ccf64ceae9c5403bf50a044cb6d505bfd2a2963ee58338ba268fd65beab92a9f`.
Installation does not fetch tasks, images, models or a trainer. Upstream data
and its licensing remain separate from this package's MIT implementation.

```sh
dsec-mbpp --input /data/mbpp.jsonl --out /data/mbpp-prepared --parquet
```

The official split is train IDs 601–974 (374), validation 511–600 (90),
test 11–510 (500), and few-shot prompting 1–10 (10). Outputs include JSONL,
optional Parquet and a hash manifest. Reference `code` and challenge tests are
excluded. The prompt includes the task and the three original public tests,
following MBPP's documented setup; they are also the execution reward tests.
Do not put test-split tasks into training or use reference implementations as
model trajectories. Accuracy on these public tests is not hidden-test safety.

## Build and start the shared Python environment

Install build tools from the [core quickstart](../../docs/guides/QUICKSTART.md).
Supply a trusted **EROFS + OverlayFS-capable** guest kernel, Firecracker, static
BusyBox, GCC/static libc, Docker and a locally available `mkfs.erofs` tools image.
The ordinary Firecracker guest kernel may lack EROFS: selecting it causes a
mount failure before the command agent starts. Image construction is a separate,
offline step; it does not download images or require TB2.1.

```sh
IMAGE_ID=$(docker image inspect python:3.13-slim-bookworm --format '{{.Id}}')
TOOLS_ID=$(docker image inspect your-local-erofs-tools --format '{{.Id}}')
dsec-prepare-image --image "$IMAGE_ID" --tools-image "$TOOLS_ID" \
  --environment-id python-mbpp --output "$HOME/dsec-artifacts/python-mbpp" \
  --kernel /opt/dsec/vmlinux-erofs --agent-source ./guest_agent.c \
  --busybox /usr/bin/busybox --cpus 1 --memory-mb 512 \
  --boot-size-mb 256 --reserve-gib 10
```

The builder uses pinned OCI layers, preserves whiteouts and creates a validated
catalog plus a source/hash receipt. It refuses mutable tags, an existing output
or insufficient disk reserve. MBPP's pinned reference programs require only
Python standard-library modules. The builder does not apply OCI entrypoints,
`WORKDIR` or image-specific environment variables; provision those explicitly
for other applications. All samples share these immutable layers and use
independent writable boot disks. No guest network is required here.

Configure a separate instance with `dsec-host --config host-mbpp.json init
--instance mbpp-verl`, passing `--state-root "$HOME/dsec-state/mbpp"`,
`--catalog "$HOME/dsec-artifacts/python-mbpp/catalog.json"`, the generated
`boot.ext4` as `--template` and your VMM/kernel paths as `--binary`/`--kernel`.
Follow the quickstart's doctor/render/start/wait steps. Use the actual host interface from `ip route show default` for
`--network-interface`: the scheduler measures that interface even for a
networkless guest. Set capacity and admission budgets explicitly for your host;
the default two episode slots are a starting example. No network helper is
needed for this networkless instance. Do not reuse another application's state
root or modify its services.

```sh
dsec-mbpp-verify --worker-socket "$HOME/dsec-state/mbpp/worker/worker.sock" \
  --environment-id python-mbpp --out ./mbpp-acceptance \
  --samples 32 --concurrency 8 16 32
```

This executes correct/wrong/timeout/early-exit fixtures and checks that a file
created by one sample is absent in the next VM. It saves each execution receipt.
Concurrency runs reuse one fixed synthetic cohort: these numbers verify system
execution, not MBPP model accuracy or independent RL samples.

## Native verl integration

Interface inspection is pinned to
[verl revision 8718ca3](https://github.com/verl-project/verl/commit/8718ca30a3f002f93b7c4fd99b9b2506718681bc).
This pin passed the four-update pilot and complete-epoch live GPU case. Install verl and
its supported model/inference backend separately; do not change the running
Miles environment to install it.

```sh
export DSEC_ROLLOUT_WORKER_SOCKET=/srv/dsec/mbpp/worker/worker.sock
export DSEC_MBPP_ENVIRONMENT_ID=python-mbpp
export DSEC_MBPP_EVIDENCE_DIR=/data/mbpp-run/execution-evidence
```

Install a separate Python 3.12 GPU environment from the upstream pinned
revision, leaving other trainers unchanged:

```sh
git clone https://github.com/verl-project/verl.git verl
git -C verl checkout 8718ca30a3f002f93b7c4fd99b9b2506718681bc
cd verl
uv sync --frozen --extra sglang --extra fsdp
export CUDA_HOME=/usr/local/cuda  # Your compatible CUDA 13 development toolkit.
export MAX_JOBS=2
# Use the same interpreter for driver, Ray workers and the reward package.
uv pip install --python .venv/bin/python --no-deps /path/to/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  /path/to/dsec_mbpp_case-0.1.0.dev0-py3-none-any.whl
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --worker-socket "$DSEC_ROLLOUT_WORKER_SOCKET" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-config-check --dry-run
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --worker-socket "$DSEC_ROLLOUT_WORKER_SOCKET" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-pilot --steps 4
```

The lock selects CUDA 13 / PyTorch 2.13 / SGLang 0.5.20 / Transformers 5.12.1;
use a compatible NVIDIA driver and a CUDA 13 development toolkit with a C++
compiler for first-use FlashInfer JIT. Set `CUDA_HOME` to that toolkit and
`MAX_JOBS=2` to bound initial compilation resources on a small host. The launcher
prepends the active venv tools directory to PATH; using its Python alone does
not activate Ninja or other installed console tools. `--dry-run` composes the native Hydra config
without starting a training job. The live command is a four-update acceptance
pilot, not a full MBPP training result. It uses two train prompts per batch,
eight responses each, and four validation prompts (also eight responses each).
`--steps`, `--batch-size` and `--validation-samples` control the explicit scope.
Outputs contain the exact launch arguments, trainer log, rollout/validation
records, checkpoints and per-sample sandbox evidence. Generation evidence saves
the native prompt/response token IDs, response masks and logprobs, effective
sampling parameters and finish reason; execution receipts link their generation
ID to the sandbox rollout ID. This preserves the native trainer token path. Use a new output directory
for each attempt. The validated backend choice is explicit: SGLang generates
responses and FSDP2 trains the adapter; verl itself requires a rollout backend.
The DSec reward hook depends on the unified sandbox SDK, not on either GPU
backend. The pilot uses native full-state FSDP checkpoints, about 4.23 GiB each
on this model, and retains one final checkpoint. Reserve space before launch.
The upstream shuffle seed was null in the earlier pilot; cohorts and scores
were recorded. The launcher now fixes dataset and SGLang engine seeds to 42
(configurable with `--seed`), without claiming bitwise deterministic sampling.

### Complete training and held-out comparison

Run a complete epoch over all 374 training tasks with eight responses each.
The default two-prompt batch gives 187 GRPO updates and 2,992 training samples.
The launcher checks prepared data hashes and task IDs. `--epochs` rejects batch
sizes that would discard the final partial batch; prompt filtering is disabled
and overlong inputs fail explicitly. A 4096-token prompt window includes the
longest original test prompt. The following recipe allows 8192 generated tokens
and 32 concurrent generation sequences, giving a total context window of 12288.
Both evaluations use these same limits.

```sh
.venv/bin/dsec-mbpp-train --verl-root "$PWD" --model /models/Qwen3.5-2B \
  --data /data/mbpp-prepared --worker-socket "$DSEC_ROLLOUT_WORKER_SOCKET" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-full \
  --epochs 1 --batch-size 2 --prompt-length 4096 --seed 42 \
  --response-length 8192 --generation-concurrency 32 \
  --gpu-memory-utilization 0.6 --mamba-cache-slots 160 \
  --logprob-chunk-size 128 \
  --evaluation-split test --validation-samples 500 --evaluation-batch-size 32 \
  --evaluate-before-train --checkpoint-every 20
.venv/bin/dsec-mbpp-compare --run /data/mbpp-full --out /data/mbpp-comparison.json
```

SGLang may cap effective concurrency below the requested value when GDN state
slots are insufficient. On the pinned backend, allow up to five state slots per
running request for radix caching and overlap buffers. This recipe explicitly
reserves 160 slots for 32 requests without changing state precision. Verify
`max_running_requests` and `num_running_reqs` through SGLang's `/v1/loads`
endpoint; changing `max_num_seqs` alone does not guarantee the requested capacity.
The 60% inference memory budget must fit the target GPU. Native verl releases
the inference engine's memory for the training phase.

The launcher forwards `--logprob-chunk-size` (default 128) to every Ray actor
through `SGLANG_LOGPROB_CHUNK_SIZE`. Qwen3.5's 248320-word vocabulary makes each
2048-row FP32 logprob workspace about 1.89 GiB; a row-selection copy can coexist
with that workspace. The smaller chunk bounds these temporary allocations
without reducing concurrency, response length or state precision. It does not
guarantee that every other GPU allocation fits.

If final evaluation fails after the model checkpoint was saved, reuse the same
recipe with `--evaluate-checkpoint /data/mbpp-full/checkpoints/global_step_187`
and a new `--out /data/mbpp-post-eval`. The launcher restores the native
checkpoint, synchronizes its weights and sets `trainer.val_only=true`; it
performs no further gradient updates. Keep the failed evaluation separately
and compare only a complete replacement evaluation. Native verl logs the final
training batch after validation, so a validation failure may leave that batch's
dump missing even though its optimizer update and checkpoint are complete.

After the replacement evaluation completes, compare it with the original
training run and its complete initial evaluation:

```bash
dsec-mbpp-compare --run /data/mbpp-full --post-run /data/mbpp-post-eval \
  --out /data/mbpp-full/comparison-recovered.json
```

Start from the base model with a fresh LoRA adapter; this command does not
resume the earlier pilot. Both initial and final evaluations sample eight
responses for each of the same 500 test tasks. The native `val_files` channel
is used for this held-out evaluation only: test scores never enter gradients
or checkpoint selection. Evaluation runs before training and at the final
step; intermediate checkpoints retain only one actor state.

The comparison checks complete train/test coverage, reward/generation linkage
and VM cleanup, retaining format and candidate-timeout zeros. It reports mean
sample success (the pass@1 estimate), pass@8, newly solved/unsolved tasks,
length-stop counts and a paired-task bootstrap interval for the pass@1 change.
Samples before and after training are independent draws under the same preset;
the interval does not establish universal improvement from a single run.
Evaluation rows link directly to receipt IDs. Native training dumps omit
custom reward fields, so training rows are matched by task ID, output SHA-256
and score, preserving duplicate counts and prohibiting receipt reuse. Identical
outputs cannot be tied to a particular native uid through that fallback.

### Selected acceptance preset

The first MBPP/verl experiment uses **Qwen3.5-2B**, GRPO and **eight sampled
responses per prompt**. Keep the previous experiment's LoRA hyperparameters:
rank 8, alpha 16, and language-model MLP projections only. The previous
Megatron `linear_fc1`/`linear_fc2` targets correspond to HF/PEFT
`gate_proj`, `up_proj`, and `down_proj`; verify the actual trainable-module
list and the SGLang adapter targets before accepting a training run.

Start in **Non-Thinking** mode with the previous Non-Thinking sampling preset:
`enable_thinking=false`, temperature 0.7, top-p 0.8, top-k 20, min-p 0.0,
presence penalty 1.5, and repetition penalty 1.0. The pinned verl single-turn
agent loop forwards temperature/top-p/top-k and sets repetition penalty 1.0;
it does not forward presence penalty. Our thin native-loop extension explicitly
forwards presence penalty 1.5 and min-p 0.0; saved requests confirmed the preset
in the live pilot. Native response token IDs, masks and logprobs are preserved.

The validated short pilot used **4,096 generated tokens for the entire response**,
using both `data.max_response_length=4096` and
`actor_rollout_ref.rollout.response_length=4096`. Disable Thinking through
`data.apply_chat_template_kwargs.enable_thinking=false`. The launcher sets both limits and the native chat-template option. GPU
acceptance of the 4096-token pilot passed. Use `--response-length` and
`--generation-concurrency` to change the limits together for training and
evaluation; these values are recorded in `launch.json`. Inspect the saved
effective request parameters and token counts when changing backends or versions.
Record `finish_reason`, response length and incomplete-code/thinking rates;
report the bounded-response baseline without discarding length-limited
samples. A length stop is not by itself an execution failure: complete code
can still be graded, while incomplete output earns the documented format
zero. Preserve the native trainer's token IDs and masks, and keep service
failures separate from model failures.

Eight responses per prompt do not impose an eight-sandbox concurrency limit.
For system throughput, use the same fixed cohort of generated candidates at
each concurrency level, with a fresh sandbox per execution. Increase execution
concurrency through 8, 16 and 32, continuing higher when measured resources
allow. Separately exercise the end-to-end path with multiple prompt groups;
report generation, sandbox execution, queue wait and total wall-clock time,
execution throughput, p50/p95 latency, failure/timeout counts and peak CPU,
host memory, disk and network usage. Keep cold creation and repeated warm
image/cache use as labelled conditions. Resource admission remains the
scheduler's responsibility, and repeated load-test executions must not be
counted as independent GRPO training samples.

To replay a fixed generated cohort, supply JSONL records containing
`solution_str`, prepared `ground_truth` and the original `expected_score`
(0 or 1). The same records are used at every concurrency level; a passed load
check means the original verdict was reproduced, including original zeros.

```sh
dsec-mbpp-verify --worker-socket "$DSEC_ROLLOUT_WORKER_SOCKET" \
  --environment-id "$DSEC_MBPP_ENVIRONMENT_ID" --out /data/mbpp-generated-load \
  --candidates /data/fixed-candidates.jsonl --concurrency 8 16 32 48 \
  --host-interface YOUR_INTERFACE --disk-device YOUR_BLOCK_DEVICE
```

Optional resource sampling reads existing Linux counters every 0.25 seconds.
Reported peaks cover the whole host and include background activity; they do
not attribute host memory or disk I/O to a particular sandbox. `--disk-device`
is a `/sys/class/block` name, such as `nvme0n1`. Configure the instance capacity
and budgets before raising concurrency. The validated fixed cohort plateaued
around 13 executions/s at 32, with higher latency at 48; this short test does
not establish a host-wide capacity limit.

## Reward and evidence

Passing all three assertions earns 1, assertion/runtime failures earn 0.
The candidate child has a ten-second deadline by default; a handled candidate
timeout earns 0. The outer verifier has a separate thirty-second deadline;
missing/truncated envelopes, unknown execution outcomes or service failures
raise and must not be represented as model zero scores.

The model must return one complete Python code block; an unfinished thinking
segment or ambiguous format earns a separately labelled `model_format` zero
without executing it. Raw responses, source/profile identity, execution result,
verdict and cleanup/error are preserved under the evidence directory.
Transport failure does not blindly retry candidate side effects: attach and
reconcile the recorded rollout ID before deciding whether to proceed.
The worker journals the execution action; the application records its parsed
verdict in the host receipt. This is not the worker's TB2.1 canonical-verifier
record, and it does not implement durable resume of verl optimizer sessions.

Acceptance order: known correct/wrong/timeout fixtures in real DSec microVMs;
bounded model evaluation; independent eight-sample groups; one observed real
GRPO update with intact token/mask accounting; held-out test evaluation.
Record zero-advantage groups honestly rather than forcing or replacing scores.
After this execution-reward path passes, add the stateful multi-turn Agent Loop
and run a cross-framework TB2.1 case; neither is claimed by this first adapter.

Actual onboarding issues and validation evidence are recorded in the
[first-use report](../../docs/reports/MBPP_VERL_FIRST_USE.md).
