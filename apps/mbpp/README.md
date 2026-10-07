# MBPP with DSec and verl (preparation candidate)

This optional application prepares the original MBPP dataset and implements
verl's native async `custom_reward_function`. DSec executes generated Python
and the three official assertions in a fresh scheduled microVM for each sample.
verl owns generation, token IDs/logprobs, GRPO and model updates. No OpenEnv,
AgentENV or SandboxFusion service is required by this adapter.

**Status:** data conversion and reward/lifecycle regressions are implemented.
Live microVM acceptance and a real verl parameter update are still pending.
This is a single-turn code-generation application, not a validated multi-turn
verl Agent Loop or a claim of production isolation against malicious graders.

## Prepare explicit dependencies

Install the DSec core and this application separately:

```sh
python -m pip install ./apps/mbpp
# Only needed for verl's Parquet input:
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

Provide an independently published generic Python 3 runtime as
`DSEC_MBPP_ENVIRONMENT_ID`. It must be a microVM `erofs_layers` catalog entry
with local storage, support 512 MiB guests, Python 3 and the dataset's required
modules. All tasks share this immutable environment; each sample has its own
writable state and rollout ID. It does not require TB2.1 task directories.
Host-side scoring never executes model code.

## Native verl integration

Interface inspection is pinned to
[verl revision 8718ca3](https://github.com/verl-project/verl/commit/8718ca30a3f002f93b7c4fd99b9b2506718681bc).
This pin has not yet passed a live GPU training acceptance. Install verl and
its supported model/inference backend separately; do not change the running
Miles environment to install it.

```sh
export DSEC_ROLLOUT_WORKER_SOCKET=/srv/dsec/mbpp/worker/worker.sock
export DSEC_MBPP_ENVIRONMENT_ID=python-mbpp
export DSEC_MBPP_EVIDENCE_DIR=/data/mbpp-run/execution-evidence
```

Add the following overrides to a separately validated verl launch command:

```text
algorithm.adv_estimator=grpo
actor_rollout_ref.rollout.n=4
data.train_files=/data/mbpp-prepared/train.parquet
data.val_files=/data/mbpp-prepared/validation.parquet
reward.custom_reward_function.path=/absolute/site-packages/dsec_mbpp_case/reward.py
reward.custom_reward_function.name=compute_score
```

Resolve the actual installed module path with
`python -c 'import dsec_mbpp_case.reward as r; print(r.__file__)'`. These are
integration overrides, **not a complete portable Qwen3.5-4B training recipe**.
Its 16 GiB fit and supported verl LoRA/inference combination remain an
independent acceptance gate. Keep Qwen3.5 Thinking settings explicit in that
recipe, with a distinct model-response limit and episode/test deadline.

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
bounded model evaluation; independent four-sample groups; one observed real
GRPO update with intact token/mask accounting; held-out test evaluation.
Record zero-advantage groups honestly rather than forcing or replacing scores.
After this execution-reward path passes, add the stateful multi-turn Agent Loop
and run a cross-framework TB2.1 case; neither is claimed by this first adapter.
