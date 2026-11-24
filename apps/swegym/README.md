# SWE-Gym through the DSec API

The current OpenHands command uses SDK **v1.58.2**. In a separate Python 3.12+
controller environment, install the core wheel and resolve both optional
packages in one invocation:

```sh
python -m pip install -c integrations/openhands/sdk-constraints.txt \
  './integrations/openhands[sdk]' './apps/swegym[harness]'
python -m pip check
```

Resolving them together keeps browser-use's Click pin and LiteLLM's Hugging Face
Hub bounds intact when the verifier adds its dependencies. Publish an SDK/tools
1.58.2 guest tool layer; an old ACI layer is incompatible. See the
[integration boundary](../../integrations/openhands/README.md).

```sh
dsec-swegym-openhands --task ./tasks/python__mypy-15413.json \
  --config ./sdk-case.json --out ./sdk-results
```

The configuration retains the existing API/environment and `openhands` keys;
set `openhands.thinking_history` explicitly to `current_user_turn` for Qwen
Thinking, `all_assistant_turns` for DeepSeek Thinking, or `disabled`. Sampling
parameters stay explicit. The CLI starts a private controller process before
importing OpenHands. Only the public issue is sent to the official agent;
candidate capture precedes retirement and the official verifier runs in a fresh
sandbox. SDK events and completion logs are retained even on failure. The
legacy evaluator is `dsec-swegym-openhands-legacy`, installed with the integration's
`[legacy]` extra in a different environment. Results from the two agent versions
must be labeled separately.

For a self-hosted SGLang model alias, explicitly configure
`openhands.capability_overrides: {"supports_sampling_params": true}` when the
endpoint supports temperature/top-p. These are the upstream SDK's native
capability overrides, passed through unchanged. Otherwise its reasoning-model
fallback can omit configured sampling values. If Thinking is controlled only
through `sampling_extra.chat_template_kwargs.enable_thinking`, an endpoint
profile can also set `supports_reasoning_effort: false`. Configure these per
endpoint rather than applying them to unrelated providers; check actual server
requests/resolved sampling as well as the case configuration.

For a MiniCPM endpoint that leaves failed XML tool calls in assistant text,
explicitly add `"unparsed_tool_recovery": "minicpm5", "recovery_limit": 3`
to `openhands`. The pinned SDK's Stop hook gives bounded feedback and continues
the same conversation; it never aliases or executes the text call. Apply this
same option to frozen baselines, training and after-evaluation. Leave it unset
for other model formats. Evidence includes the original reply, the next request's
feedback, HookExecutionEvents and `controller/tool-recovery.json`; recovery does
not reset iteration or time budgets. See the
[integration contract](../../integrations/openhands/README.md#optional-recovery-of-unparsed-minicpm-tool-markup).

`context_length` is the **total** model window. The runner reserves
`response_length` plus `context_safety_tokens` (default 1,024), and passes the
remaining input budget to the SDK with its official `LLMSummarizingCondenser`.
For 65,536/32,768 this is 31,744 input tokens; the response ceiling stays 32,768.
For a local Qwen endpoint, also install this case's `[agent]` extra in that
controller environment and set `openhands.custom_tokenizer` to the existing
model directory (for example `/srv/models/Qwen3.5-2B`). This loads the tokenizer,
not the model weights. Keep the tokenizer/version in the run evidence. The
official condenser retains original events on disk while summarizing model
context. Recognized iteration/deadline policy limits still capture and score
the actual candidate; model transport and uncertain tool failures remain unscored.

This optional application prepares pinned SWE-Gym-Lite training tasks and runs
the official SWE-Gym evaluation script and scorer inside prepared DSec Container
sandboxes. Installing the DSec core does not install this case, its dataset or
SWE-bench. Container's writable repository persists across commands; FnCall's
reset-between-invocations semantics are not a replacement for that workspace.

The verification entry point checks execution and scoring with empty, official
or user-supplied patches. A separate optional Qwen3.5/SGLang scaffold records
real multi-turn model trajectories. Oracle success is a harness check, not model
accuracy or RL training. SAO training is a later integration: single rollout, critic,
rollout log probabilities and observation-token masking are required; asynchronous
GRPO alone is not SAO.

## Install and prepare one task

Install the core, then this package in a separate application environment:

```sh
python -m pip install './apps/swegym[data,harness]'
curl -fL https://huggingface.co/datasets/SWE-Gym/SWE-Gym-Lite/resolve/main/data/train-00000-of-00001.parquet -o swegym-lite.parquet
dsec-swegym-prepare --source swegym-lite.parquet \
  --instance-id python__mypy-15413 --out ./swegym-tasks
```

Preparation checks the source SHA-256 and keeps gold/test patches out of
`prompts.jsonl`. The private task JSON files contain scoring data and answers;
do not put them in model context or the agent workspace. The fork is pinned to
`242429c188fcfd06aad13fce9a54d450470bf0ac`, with source checks for the scorer,
parser, constants and script factory. A different `swebench` package is rejected.

## Prepare the environment

An operator first publishes a task environment and advertises its resource
minimum through the normal API/Node configuration. Follow the existing
[QEMU Container host guide](../../docs/guides/DSEC_HOST_CONFIGURATION.md).
Use the official prebuilt instance image, keep its base commit and dependencies,
and ensure UID 1000 can write the repository and its pip user directory.
This includes generated metadata such as `/testbed/mypy.egg-info`, not just the
top-level repository directory. The first image uses `HOME=/work`,
`PIP_USER=1`, and `/work/.local/bin` on PATH. Copy the unchanged repository from a
stopped official-image container into a derived image with
`COPY --chown=1000:1000 testbed/ /testbed/`; no task code needs to execute on the
physical host. Keep Conda's existing contents unchanged.
Image tags are resolved to immutable digests before preparation. Do not install
or expose host Docker in the task; Docker remains inside its QEMU guest.

The first case uses `xingyaoww/sweb.eval.x86_64.python_s_mypy-15413` (the Hub
spelling differs from the dataset's `python__mypy-15413`). Network availability
and offline dependency preparation belong to the environment; the case does not
silently remove official installation/test commands or change the expected tests.
For this image, build the guest with `tools/build_docker_guest.py
--storage-driver overlay2 --disk-mb 8192` and publish a separate catalog entry
with `startup_timeout_s: 240`. The guest imports a large image on cold boot;
`vfs` copies cumulative layers and exhausts the 8 GiB guest disk. These settings
do not modify the physical host's Docker storage driver or tool-command timeout.
Select `disk_cache: direct` in a new environment definition to avoid host dirty
file pages from the import consuming the outer VM's cgroup allowance. Keep the
existing guest RAM, VMM allowance and IAM minimum; verify Direct I/O support on
the actual backing filesystem rather than silently falling back.

### Move shared task files to EROFS/DAX

For a DAX-enabled Node, an operator can split the prepared, immutable **local
image ID** before building the guest. This is an optional image-publication step;
the application and verifier continue to use the same public API.

```sh
PYTHONPATH=src python tools/split_docker_image.py \
  --image sha256:YOUR_PREPARED_LOCAL_IMAGE_ID \
  --out /srv/dsec/prepared/mypy-15413 \
  --layer-root /srv/dsec/layers \
  --shared-dir /usr --shared-dir /opt/miniconda3 --shared-dir /testbed
python - <<'LAYERS'
import json
from pathlib import Path
root = Path('/srv/dsec/prepared/mypy-15413')
value = json.loads((root / 'split.json').read_text())
(root / 'layers.json').write_text(json.dumps(value['readonly_layers']))
print(value['task_archive_sha256'])
LAYERS
python tools/build_docker_guest.py \
  --base sha256:YOUR_PINNED_PYTHON_BASE \
  --docker-tools sha256:YOUR_PINNED_DOCKER_TOOLS \
  --source . --out /srv/dsec/templates/mypy-shared.ext4 --mode container \
  --task-archive /srv/dsec/prepared/mypy-15413/task-lean.tar \
  --task-archive-sha256 SHA256_PRINTED_ABOVE \
  --readonly-layers /srv/dsec/prepared/mypy-15413/layers.json \
  --shared-mount /usr --shared-mount /opt/miniconda3 --shared-mount /testbed \
  --storage-driver overlay2 --disk-mb 2048
```

Create the publication root and install its scoped Node permission as described
in the host guide first. Add the builder's template, inner image ID,
`readonly_layers` and `shared_mounts` to a **new** environment entry; keep
`disk_cache: direct` for its private disk. Point `case.json` at that entry and
use its measured disk minimum (2,048 MiB for this bounded mypy case).

The split exports a stopped container without executing task code on the host.
It preserves the inspected runtime Config, numeric file ownership, modes and tar
metadata. Selected directory contents are removed from the lean image; their
mount points remain. The resulting image ID changes. Cross-partition hardlinks,
image-declared volumes and missing/non-directory roots are rejected rather than
silently changed. This consumes an already flattened Docker export; it is not a
raw OCI-layer whiteout converter. Limit this path to the Node's eight-layer cap.

Container edits and generated package metadata copy up to each VM's private
OverlayFS upper. Keep the original harness's exact Git safe-directory setup;
do not change ownership or skip installation/tests to accommodate a probe.
The mypy baseline/oracle gate must pass again after publication. The 2 GiB disk
and three selected directories are case-specific, not universal defaults. This
path has not established cold-cache lazy-read speed or whole-VM memory savings.

### Publish baseline and ancestor Git objects

Some official task images retain future fixes in Git history. Removing branches
or resetting HEAD does not remove packed, unreachable or reflog-referenced
objects. Before publishing a model-evaluation environment, export `/testbed`
from the stopped image and prepare its Git metadata offline:

```sh
dsec-swegym-git-baseline --repository /srv/dsec/staging/testbed \
  --base-commit EXACT_TASK_BASE_COMMIT --out /srv/dsec/staging/baseline.git \
  > /srv/dsec/staging/repository-policy.json
```

The fresh database includes the original baseline commit, **all its ancestors**,
and all trees/blobs reachable from that history. It preserves commit identities
and is not shallow. Future and unrelated objects, remotes, alternates, reflogs
and extra refs are excluded. Its object pack is rebuilt with deltas restricted
to that explicit object set. The source image is unchanged. Preserve every non-Git file and numeric
ownership/mode when replacing `.git` in a **new** task layer or derived image.
Remove the original Git database from all guest-visible lower layers; a whiteout
or writable overlay alone does not remove leaked objects from those layers.

Copy the emitted JSON into `case.json` as `repository_baseline`. Both the
OpenHands runner and official verifier check this policy in each fresh sandbox,
before exposing the issue or applying any candidate. `repository-check.json`
and verifier reports retain the observed object-set and commit-set digests.
Verify that baseline `git log`, `git show` and `git blame` match the source
repository before publication; a shallow or incomplete source is rejected.
Re-run the empty
patch and oracle controls after publishing, then compare models using this same
environment and fresh conversations. This gate prevents repository-history
leakage; it does not claim to decontaminate model pretraining or internet access.

Save the ordinary client config as `api-client.json`:

```json
{
  "client": {"endpoints": ["https://api.example:8443"], "project": "research", "ca_file": "/run/dsec/ca.crt"},
  "token_file": "/run/dsec/token"
}
```

Save `case.json` using your prepared environment and its **outer VM** minimum
resource demand, not just the inner container memory:

```json
{
  "api_config": "/run/dsec/api-client.json",
  "spec": {"environment_id": "swegym-mypy-15413", "memory_limit_mb": 512, "cpu_cores_limit": 1},
  "resource_demand": {"cpu": 1, "memory_mb": 1280, "disk_mb": 8192, "network_mbps": 0}
}
```

## Check scoring before connecting a model

```sh
dsec-swegym-verify --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --mode baseline --out ./evidence/baseline
dsec-swegym-verify --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --mode oracle --out ./evidence/oracle
```

Expect baseline score 0 and oracle score 1, both with `cleanup=stopped`. Each
episode uses a new sandbox. Repository inspection, patch application and official
verification are separate steps with request IDs, commands, outputs and timing.
The report and full test log survive failure. Unknown outcomes are not replayed,
and missing/incomplete verification is not reported as a valid negative score.
If the official log exceeds the ordinary 8 MiB transfer budget (for example,
`git show` expands a shallow root commit), the runner transfers it compressed
and preserves the complete expanded log for the same official parser. It does
not shorten the verifier script. The compressed transfer is bounded at 8 MiB
and expansion at 128 MiB; evidence outside those bounds remains unscored.
The default per-command bound remains 30 seconds. In `case.json`, set
`command_timeout_ms` for tool/setup commands and `verifier_timeout_ms` separately
for official verification. Both must fit the operator-pinned environment ceiling
(up to 300,000 ms); building a longer-capable guest is described in the host
guide. A timed-out or incomplete verifier remains unscored, with its captured
log retained.

For a new long-command-capable environment, add these case settings (milliseconds):

```json
{"command_timeout_ms": 45000, "verifier_timeout_ms": 120000}
```

Its operator-prepared guest/catalog must both allow at least 120,000 ms. Merely
changing the case config does not extend an old guest's limit. State queries
remain available during a command. A disconnected/cancelled caller should query
the original request ID; it must not assume cancellation stopped execution or
resend an unknown action. See the first-use record's pre-training gate for the
bounded API restart, timeout and resource-wait acceptance.

For a candidate patch, use `--mode patch --patch solution.diff`. Run evaluation in
a fresh sandbox rather than trusting arbitrary state left by a model episode.
The upstream parser/scorer is retained; this does not make task-emitted logs a
proof against a malicious candidate impersonating test output.

See the [first-use record](../../docs/reports/SWEGYM_FIRST_USE.md) for measured
results and remaining integration work. Verification does not start a model,
trainer or SAO implementation.

## Legacy CodeActAgent baseline

For the coding-agent baseline, install the separate
[OpenHands integration](../../integrations/openhands/README.md) in the application
environment. It preserves the pinned upstream CodeActAgent prompt, parser,
history and controller. DSec implements its runtime; the application still owns
the public issue, candidate patch and fresh official verifier. The integration
contains no SWE-Gym dependency and can be used with other harnesses.

Publish the integration's immutable Python/editor tool layer at
`/opt/dsec-openhands` alongside the task layers, using a new environment/template.
Keep the original task interpreter and dependencies. Extend `case.json`:

```json
{
  "openhands": {
    "model": "openai/dsec-swegym-qwen35-2b",
    "base_url": "http://127.0.0.1:31026/v1",
    "native_tool_calling": true,
    "temperature": 0.6,
    "top_p": 0.95,
    "sampling_extra": {"top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0, "chat_template_kwargs": {"enable_thinking": true}},
    "response_length": 32768,
    "context_length": 65536,
    "model_timeout_seconds": 600,
    "max_iterations": 100,
    "episode_seconds": 1200,
    "runtime": {
      "cwd": "/testbed",
      "shell_init": "source /opt/miniconda3/bin/activate && conda activate testbed",
      "transport_timeout_ms": 120000
    }
  }
}
```

Retain the surrounding API/spec/demand and tool/verifier budgets. The endpoint
must actually support OpenAI tool calls (for Qwen3.5/SGLang, configure its tool
and reasoning parsers); the explicit capability overrides the old fork's model
name allowlist, not its action parser. API credentials can be supplied via
`openhands.api_key_env`; never put them in a task prompt. No model is started by
this command:

```sh
dsec-swegym-openhands-legacy --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --out ./evidence/openhands-episode
```

Reports retain upstream source identity, completion-wrapper requests and LiteLLM
`ModelResponse` objects,
OpenHands events/history, DSec operation IDs, candidate patch and fresh official
verification. These are frozen-policy evaluation records; they are not accepted
as exact sampled-token/logprob tensors for SAO. The 100-iteration/65,536-token
pilot is a local resource setting. SAO reports OpenHands with 300 turns and a
128k context, without an exact source commit; this is not an exact reproduction
of those model/agent settings. Keep model success separate from baseline/oracle
plumbing checks.

For the native OpenAI-compatible Qwen Thinking configuration above, the runner
also saves the actual provider request body and response body (no HTTP headers)
under `completions/provider/`. The old LiteLLM converter drops
`reasoning_content`, and this pinned agent does not carry that field in its
history. A per-episode model-boundary bridge restores it by exact tool-call
identity for the current user/tool chain, matching the
[Qwen template](https://huggingface.co/Qwen/Qwen3.5-2B/blob/main/chat_template.jinja).
Earlier completed user turns do not receive it. Tool names, arguments, output,
system/task instructions and the upstream agent/parser/stuck detector remain
unchanged. The bridge rejects unmatched history instead of guessing reasoning;
resuming an old episode without its provider records is not supported. Raw JSON
bodies still do not supply the native sampled-token/logprob training contract.

For DeepSeek Thinking, replace only the endpoint/model-related options, using
an environment variable supplied by your credential manager:

```json
{
  "model": "openai/deepseek-flash",
  "base_url": "https://api.deepseek.com",
  "api_key_env": "DEEPSEEK_API_KEY",
  "native_tool_calling": true,
  "sampling_extra": {
    "thinking": {"type": "enabled"},
    "reasoning_effort": "high"
  }
}
```

The [DeepSeek tool/Thinking contract](https://api-docs.deepseek.com/guides/thinking_mode/)
requires reasoning from **all** previous assistant turns, including replies
without tool calls. This profile restores those replies in exact response order
and serializes assistant text parts as the API's string content, without changing
their text or tool calls. It rejects changed/missing history rather than guessing.
Do not forward Qwen-only `top_k`, `min_p`, `repetition_penalty` or
`chat_template_kwargs`. DeepSeek ignores temperature in Thinking mode; top-p
behavior is model-dependent. Record the effective provider contract rather than
claiming identical sampling across model families. The runner preserves the
actual returned model identifier and full request/response bodies. Keep the
same task, tool definitions, environment, verifier and interaction/time/output
limits for a model comparison.

The pinned controller's evidenced `Agent got stuck in a loop` terminal is a
policy outcome: retain that termination, capture the actual candidate, and run
fresh official grading. Parser rejections also qualify only when they exactly
match an unknown tool or invalid JSON arguments in the completed model reply.
Rejected calls are never renamed, repaired or executed. No synthetic score is
assigned. Unmatched ERROR states, provider failures, unknown execution and
incomplete verifier evidence stay unscored. The provider client's retries are
explicitly disabled independently of OpenHands' retry setting. Size the model
request deadline for the output limit; command and episode limits are separate.

## SAO integration pilot (experimental)

`dsec-swegym-train-sao` is our small-resource implementation of the mechanisms
in the [SAO paper](https://arxiv.org/html/2607.07508), built on a pinned native
verl FSDP2 engine. It is **not the authors' official implementation or an exact
reproduction of their experiments**. A single-task, two-update closed loop
passed on Linux on 2026-10-28. All four episodes scored zero at the 20-iteration
limit; no learning improvement was demonstrated.

The intended algorithm contract is one rollout per admission, a learned token
critic with two updates per actor update, skip-observation GAE, and direct
importance sampling using the rollout engine's actual sampled logprobs. Coding
DIS uses the strict interval `(0.2, 4)`. Actor/critic learning rates are
`1e-6`/`5e-6`; critic attention stays frozen.

The pilot changes the experimental setup deliberately:

| Setting | Paper | This integration pilot |
| --- | --- | --- |
| Coding model | Qwen3-30B-A3B-Thinking-2507 | MiniCPM5-2B-SFT |
| Coding evaluation | SWE-Bench Verified | One SWE-Gym task; no held-out claim |
| Update scope | Frozen-attention MoE critic | Dense MLP LoRA for both actor and critic, plus a learned scalar value head |
| Critic initialization | Emphasizes value-model pretraining | SFT backbone with a newly initialized value head; no value pretraining yet |
| Resources | Asynchronous RL setup | One 16 GiB GPU shared by inference and training; sandbox tools may overlap |
| Training batch | 128, group size 1 | One completed episode per update, group size 1 |
| Critic warmup | 10 steps | None in this two-update lifecycle pilot |
| Coding budget | 300 turns / 128k context | 100 iterations / 65,536 context / 32,768 response; the first lifecycle pilot used 20 iterations |

Both LoRA adapters use rank 8, alpha 16 and `gate_proj`, `up_proj`, `down_proj`.
These are local engineering choices, not SAO requirements. The single-task,
two-update pilot checks the training lifecycle and parameter publication, not
learning effectiveness or reproduction of the paper's scores.

The local SGLang capture gateway saves each request's native prompt IDs, sampled
output IDs, behavior logprobs and published policy version. It rejects mismatched
records. Each generation trains on its own exact context because OpenHands may
reserialize history and condense it; auxiliary summaries are conditioning
context, not policy targets. JSON text capture from a normal evaluation alone
does not meet this contract. The learner consumes scored episodes independently;
the shared GPU lease pauses inference for an update, then publishes the exported
LoRA before admitting another generation.

All of this belongs to the optional application/integration. DSec still owns
only sandbox placement, execution, isolation and lifecycle. It does not manage
actor/critic parameters, model offload or the RL algorithm. This prototype uses
native verl internals at revision `8718ca30a3f002f93b7c4fd99b9b2506718681bc`;
it is not a general distributed-training adapter. Its frozen-head Triton bridge
is explicitly restricted to one rank.

For this pilot, separately prepare the pinned trainer environment, install both
optional application/integration wheels in it, and install the SDK integration
plus application in the controller environment. Publish the task environment
and provide its normal DSec API case configuration first. The model directory
must contain the chosen weights and matching controlled tokenizer/template.
Ports 31026 and 31027 on controller loopback must be unused. Then run:

```sh
dsec-swegym-train-sao --task ./swegym-tasks/python__mypy-15413.json \
  --case-config ./case.json --model /models/MiniCPM5-2B-SFT-controlled \
  --sdk-python /venvs/openhands/bin/python \
  --trainer-python /venvs/pinned-verl/bin/python \
  --out ./evidence/sao-pilot --steps 2 --max-iterations 100
```

Use a new output directory. This runs before/after evaluation and repeated
independent training episodes of that one task, retaining exact generations,
official grading and LoRA/optimizer checkpoints. It does not select a dataset
split or implement checkpoint resume. GPU generation/update serialization is
intentional here; this acceptance does not establish asynchronous throughput
gains or correction of mixed-version trajectories. See the
[first-use report](../../docs/reports/SWEGYM_FIRST_USE.md)
for measurements and limitations.

Evaluation preserves the case's episode/model-request deadlines (the original
case uses 1,200/600 seconds). Training episodes allow 7,200 seconds for the
single-GPU handoff queue; these deadlines are recorded separately from evaluation
and GPU wait intervals are retained in `gpu-timeline.jsonl`.

Before the 100-iteration rerun, the three longest agent contexts from the earlier
successful episode passed a discarded-gradient capacity probe with resident
SGLang release/resume. The longest segment has 26,595 tokens; peak native trainer
allocation was 12,068.38 MiB and sampled whole-GPU use was 15,133 MiB. This is
evidence for those contexts, not a guarantee for arbitrary 65k-token segments.
The historical rollout did not record behavior logprobs, so it is not admitted
as SAO training data. The diagnostic module `dsec_swegym_case.sao_memory_probe`
uses explicit diagnostic losses and discards every update; fresh RL starts from
SFT with newly captured behavior probabilities.

The full variable-shape trajectory exposed allocator fragmentation that this
three-segment probe did not cover. The learner now sets
`PYTORCH_ALLOC_CONF=expandable_segments:True` before CUDA initialization;
SGLang keeps its own allocator settings. Full updates over 56 and 97 native
segments subsequently passed, including the 31,741-token longest segment.
Training succeeded with two actor/four critic updates. The first after-evaluation
was unscored because hourly native-log rotation broke capture; that reader is
fixed and a separate after-evaluation completed with reward zero. The final
model reply used the unavailable tool name `execute_bash` instead of `terminal`,
so no source repair was submitted. The result validates the execution lifecycle,
not a training improvement. See the first-use report for provenance and limitations.

### Diagnose and warm up the critic before another policy run

`python -m dsec_swegym_case.critic_warmup` runs the optional native verl
critic alone. It loads the saved token-classification adapter **and scalar
head**, verifies their exact state digest, and never constructs an actor.
Provide a manifest containing `training` (a list of episode directories) and
`reference` (a separate episode directory). Each directory must contain the
runner's `case/` evidence and exact native `generations/` captures. Episodes
must have matching interaction, sampling, repository and deadline settings,
frozen SFT behavior, completed official grading and retired sandboxes. Failed
infrastructure is not a zero label. Summary generations never become targets.

In the current recipe, gamma and critic lambda are both one, so each action
token's Monte Carlo target is the episode's actual terminal reward. A constant
return has undefined explained variance; the report records `null`, not zero.
It reports MSE, RMSE, MAE, bias and prediction range, retaining per-token values.
Unbounded value outputs are not clipped to improve these measurements.

```sh
PYTORCH_ALLOC_CONF=expandable_segments:True /venvs/pinned-verl/bin/python \
  -m dsec_swegym_case.critic_warmup \
  --model /models/MiniCPM5-2B-SFT-controlled --adapter ./saved-critic \
  --manifest ./warmup-manifest.json --out ./evidence/critic-warmup \
  --steps 10 --lora-lr 0.000005 --head-lr 0.0001
```

Stop the inference server first to release the single GPU. Output directories
must be new. The ten optimizer steps each use two evenly spaced, full-context
generations from every training episode. Before/after diagnostics use fixed
first/middle/last training generations and **every** generation in the untouched
reference episode. The faster scalar-head learning rate is a local warmup
choice, not the paper's online critic rate. Backbone/attention stay frozen;
only MLP LoRA and the scalar head update. The report distinguishes successful
execution, reduced prediction error and a limited mixed-reward pilot gate.
All-positive data cannot pass that gate. A single-task reference is not a
held-out-task evaluation, and this command never promotes a critic to an actor
run automatically.

Cold initialization now zeroes the scalar projection and uses prior `0.5`,
or an explicit `dsec-swegym-train-sao --critic-value-prior VALUE`. This happens
after loading the backbone/PEFT modules and before FSDP/optimizer creation.
It follows the initialization principle in
[JustRL-II](https://github.com/merak0514/JustRL-II/blob/75814318f243af7e39af337c1173fc3927c9635b/miles/backends/megatron_utils/model_provider.py).
Its math recipe's `0.52` is not a universal prior. Our native HF token-classifier
path does not use Megatron's overlapping LM-head checkpoint loader; no such
contamination has been established here. Normal `--critic-adapter` restores the
saved head exactly and rejects a simultaneous cold-init prior.

For an **explicit head-initialization comparison**, add
`--reset-value-head-from-training-mean` to the value-only warmup command. It
first verifies the complete saved head/LoRA state, retains the same MLP weights,
then resets only the head to zero weight plus the unweighted **training-episode**
mean reward. Reference labels are excluded. This is a deliberate new
initialization, not checkpoint resume; default warmup preserves the saved head.
In the four-episode pilot the rewards `1,1,0,1` give prior `0.75`. Four examples
on one task do not estimate a global coding-agent prior reliably. The initial
constant prediction has no state discrimination; a lower initial MSE is not
evidence of learning. A head already at an all-identical return can legitimately
have zero gradient; this is recorded separately from an unexplained lack of
parameter updates. JustRL-II's 30 critic-only rollout batches are not equivalent
to this pilot's ten optimizer steps or the SAO paper's value preparation.

After reviewing its evidence, `dsec-swegym-train-sao --critic-adapter PATH`
can initialize a new pilot's critic from the exported directory. It verifies
head/LoRA restoration and starts a **new optimizer**; this is weight
initialization, not resuming an interrupted training run. Online critic learning
rate remains `5e-6`. These model-training facilities remain optional application
code, outside DSec's sandbox ownership boundary.

### Validate a prepared critic on independent episodes

`python -m dsec_swegym_case.critic_validate` evaluates exactly four new,
officially scored frozen-SFT episodes. Supply a manifest with a `validation`
list of their episode directories and the earlier warmup manifest as
`--prepared-from`. Paths and native response identities must be disjoint from
both warmup training and its reference, with the same interaction protocol.

```sh
PYTORCH_ALLOC_CONF=expandable_segments:True /venvs/pinned-verl/bin/python \
  -m dsec_swegym_case.critic_validate \
  --model /models/MiniCPM5-2B-SFT-controlled --adapter ./prepared-critic \
  --manifest ./validation-manifest.json --prepared-from ./warmup-manifest.json \
  --prior 0.75 --out ./evidence/critic-validation
```

Stop the inference server first. This command constructs neither Actor nor
optimizer/scheduler; the exact restored head/LoRA digest must remain unchanged.
It saves all action-token predictions at full native contexts. The primary
metric averages the four per-episode MSEs equally; pooled token error is
secondary. The prior must be fixed from training data before validation, not
chosen from these new scores. `0.75` above belongs to this four-example pilot.

The report separates workflow `status` from `gate_passed`. Its bounded gate
requires both binary reward classes and finite candidate MSE no worse than the
constant prior. A failed gate stops this experiment; no extra episodes or
warmup steps are automatically added. Passing is only a small single-task
screen, not evidence of general value-model calibration.

For the conditional training command, add `--eval-episodes 4` and
`--before-validation ./evidence/critic-validation/report.json` alongside
`--critic-adapter ./prepared-critic`. The runner rejects a failed gate or
changed checkpoint, manifest, episode evidence or protocol before starting its
trainer. It reuses all four validated scores for the before evaluation and
collects four new after evaluations. Default evaluation count remains one for
existing callers. Every update also saves `learning-signals.json`, containing
native response identities, exact Critic targets, before/after values and the
actual Actor advantages; its hash is included in the update result.

## Legacy diagnostic scaffold

The following locally implemented loop predates the OpenHands integration. Keep
it for token-boundary diagnostics; it is not the preferred coding-agent baseline.

Install the optional tokenizer/scaffold dependencies in the application venv:

```sh
python -m pip install './apps/swegym[agent,harness]'
```

Provide a separately managed SGLang native `/generate` endpoint serving the same
local Qwen3.5 tokenizer/weights. The DSec core does not start, install, unload or
update this model. The first pilot uses the existing Qwen3.5-2B checkpoint on a
16 GiB GPU; this is a model scaffold, not a framework-independent tool parser.
It follows this checkpoint's XML function-call template. Other model dialects
need their own policy/scaffold adapter.

```sh
dsec-swegym-rollout --task ./swegym-tasks/python__mypy-15413.json \
  --config case.json --model /models/Qwen3.5-2B \
  --endpoint http://127.0.0.1:31026 --policy-version CHECKPOINT_CONTENT_DIGEST \
  --max-steps 30 --episode-seconds 1200 \
  --response-length 32768 --context-length 65536 --out ./evidence/model-episode
```

The default is Thinking mode for coding: temperature 0.6, top_p 0.95, top_k 20,
min_p 0, presence_penalty 0, repetition_penalty 1. These pilot budgets are
explicit scaffold settings, not official SWE-Gym step/time requirements.
The model sees the issue statement and declared tools, never the gold or hidden
test patch. Commands and all feedback run through the ordinary DSec API. Every
model command activates the prepared `testbed` Conda environment in its fresh
shell; preflight checks its Python prefix and dependencies before sampling. Calls
in one reply execute in order after the entire batch validates. A malformed
batch executes nothing and returns format feedback; there is no silent choice
of one command from several. Commands use the explicit case budget, defaulting to 30 seconds.

Each turn saves its native request (including prompt IDs), unmodified response,
sampled IDs, logprobs and finish reason. `trajectory.json` appends actual sampled
IDs with loss mask 1 and template-rendered observations with mask 0. Prior
generated IDs are never decoded and re-tokenized into the next prompt. Context
exhaustion terminates instead of silently trimming history. A truncated reply's
partial tool calls do not execute. Full captured command output remains in the
report; feedback uses the existing bounded DSec shell-observation format with
exit status and separate capture/context truncation flags.

On finish or a step/time/token cap, capture the current repository diff, stop
the agent sandbox, and submit that diff to a fresh official verifier sandbox.
Collection preserves tracked changes, new ordinary files and symbolic links,
including filenames containing Git pathspec characters. Untracked nested
repositories (including an unfinished clone) are excluded and listed in the
`capture_candidate` output: they are not changes to the parent repository.
Ignored files remain excluded. A Git error still fails collection with evidence;
it is never silently replaced with an empty patch or a zero reward.
Its actual test score is retained even for an incomplete episode. Unknown
execution and infrastructure failures remain unscored, with evidence; they are
never replayed as a fresh action or converted into a valid zero reward.

`--policy-version` is an operator-supplied content identity for the frozen
checkpoint. Native engine weight-version metadata is retained when available.
Hot weight updates and cross-version training are not implemented here. Masked
observations alone do not implement SAO: the external trainer still needs
skip-observation GAE, the critic update schedule and DIS, then a real
before/after training evaluation.
