# MBPP + verl first-use acceptance

This report follows the published installation path for a new application user.
It records verified execution, native GPU updates and first-use issues. Selected settings:
Qwen3.5-2B, Non-Thinking, GRPO, eight responses per prompt, 4096 response tokens,
LoRA rank 8 / alpha 16 on language MLP projections.

| Observed issue | User impact | Resolution / evidence |
| --- | --- | --- |
| Application install example omitted core installation | pip may try to fetch an unpublished core version | Guide now installs the local core and application explicitly. |
| Generic Python EROFS environment had no construction command | New users could only find the TB2.1-specific builder | Extract existing shared layer/boot machinery into core `dsec-prepare-image`; TB2.1 delegates to it. Live four-layer Python build passed. |
| Guide contained reward overrides but no runnable verl recipe | A developer cannot launch native training from the guide | `dsec-mbpp-train` supplies a complete pilot recipe; native Hydra composition and four real GRPO updates passed. |
| Pinned verl single-turn loop omits presence penalty | User-selected sampling preset would silently differ | Thin native-loop extension added; live generation evidence confirmed the exact preset without changing the native token path. |
| Host interface example did not match the machine | `doctor` fails even with networkless guests | Read the default route and use its real interface; guide explains scheduler measurement. |
| Ordinary guest kernel lacks EROFS | Init mount fails, VM exits before readiness | Select the EROFS kernel explicitly; successful live rerun confirms this requirement. |
| Official PyTorch wheel download timed out through the temporary proxy | Isolated verl install stops before any training | Retry the same lock with longer HTTP timeout and four concurrent downloads; installation completed with the same locked versions. |
| Upstream uv environment does not include pip | A `python -m pip` follow-up install would fail | Guide uses `uv pip install --python .venv/bin/python` for local DSec wheels. |
| NVIDIA cuda-tile placeholder fetch received a truncated index through proxy | Official frozen install failed with `IncompleteRead` | Mac direct official index read was complete; use official 1.6.0rc5 cp312 Linux wheel and verify index SHA-256 b74c20348210d2182cd998a0ecb60c518989a79b28592d72eb8294b38ddb93d7. Exact wheel hash matched; frozen installation and CUDA availability passed. |
| Starting a venv interpreter did not put its tools on PATH | First live SGLang FlashInfer call failed to find installed Ninja | Launcher now prepends the active interpreter bin directory; all 16 failed generation requests retain evidence. Rerun passed generation, grading and token/mask linkage. |
| Small MBPP loader inherited eight worker processes | High host RAM/swap pressure; a DataLoader worker was killed during teardown | Set `data.dataloader_num_workers=0` for this small dataset; Four-update rerun exited normally with no killed DataLoader worker. No per-process training RAM peak was measured, so this does not quantify a RAM saving. |
| LoRA native FSDP checkpoint includes the base model | One checkpoint occupies 4.23 GiB despite a small adapter | Save only at the selected final pilot step and retain one checkpoint; native full-state format remains unchanged in the pilot. Upstream also supports `+actor_rollout_ref.actor.checkpoint.save_lora_only=True`; this optional save path is not yet live-validated here. |
| Guest CPU time was double-counted by the host sampler | CPU pressure and sampled utilization could be overstated | Sum only the first eight `/proc/stat` columns. Linux already includes guest time in user/nice; a counter regression and final Linux checks pass. |
| One GRPO batch had no group-wise reward variance | Completed optimizer call had zero gradient; cannot prove effective RL | Preserve all scores and use the same train split for a four-step pilot; upstream shuffle seed was null, so restarts do not preserve prompt order. |

## Validation ledger

At initial inspection the Linux host had 24 logical CPUs, 28 GiB available RAM,
114 GiB free disk and an idle 16 GiB RTX 4080. Existing trusted Firecracker/kernel,
static BusyBox and locally pinned Python/tools OCI images are reused explicitly.
The MBPP guest needs only standard-library modules; it does not require TB2.1
or network access. A separate instance avoids changes to existing services.

Prepared data and application regressions were validated on Linux. Live reward acceptance passed: correct=1; wrong, timeout and early exit=0;
a marker created in one VM was absent in subsequent VMs. All 32 synthetic
isolation samples passed at each concurrency level, using the same cohort:

| Execution concurrency | Wall seconds | Samples/second | p50 seconds | p95 seconds |
| --- | --- | --- | --- | --- |
| 8 | 4.820 | 6.639 | 1.160 | 1.358 |
| 16 | 3.504 | 9.133 | 1.649 | 1.776 |
| 32 | 2.759 | 11.600 | 2.391 | 2.724 |

This is a short execution fixture test with warm immutable-image caches, not
model accuracy, sustained throughput or a proof of maximum concurrency. Each
sample creates a fresh VM; no ready pool is enabled. The dedicated instance's
admission budget permits CPU bursting (48 reservations on 24 logical CPUs),
24576 MiB memory, 48 episode slots and a 2 GiB host free-memory floor. Those are
configured budgets, not physical CPU counts. Afterward all 101 successfully
created sandboxes were STOPPED, with no active or pending leases. One earlier
failed creation was retained as error evidence.

Experimental evidence: `mbpp-verl-preparation/live-acceptance-r2/summary.json`,
per-sample JSON receipts and `host-mbpp.json` on the Linux host. No model programs
were executed on the host. Native training validation is recorded below.

The final shared core/source export passed 183 Linux checks (182 passed, one
platform skip); the optional MBPP application passed 11 Linux checks, including
native token/mask preservation and concurrent generation-failure evidence.
TB2.1 application compatibility passed its five checks. The stopped instance occupies about 5.7 MiB of state: VM working
disks were reclaimed. Native GPU installation and complete Hydra composition passed. PyTorch
2.13.0+cu130, SGLang 0.5.20, Transformers 5.12.1 on RTX 4080; about 103 GiB
disk space remained afterward. The four-update native pilot subsequently passed; about 94 GiB remained after checkpoints and load testing.

## First native pilot

The corrected launcher generated and scored 16 train responses (two groups of
eight) and 32 validation responses. All 48 stopped normally; all TITO array
lengths matched, effective sampling included presence penalty 1.5 / min-p 0,
and every execution receipt linked its generation ID and stopped its sandbox.
Train task 890 scored 0/8; task 970 scored 8/8. Native advantages and gradient
norm were exactly zero. This is successful integration with **no demonstrated
nonzero GRPO update**. Validation passed 9/32 responses on four prompts; this
small scope is not full MBPP benchmark accuracy. No samples stopped at length.

Native `response_length/clip_ratio` reported 0.0625 because it compares length
with the dynamically padded batch width (590 here), rather than the configured
4096 limit. Use saved backend stop reasons/token lengths for truncation rates.
The four-update pilot below keeps data loading in the driver. The upstream
data shuffle seed was null in both exploratory runs; logged task IDs identify
the actual cohorts. Set an explicit native `data.seed` for a subsequent
reproducibility study; these results do not claim identical cohorts on restart.


## Four-update native GRPO acceptance

The second completed native run used the same pinned, unmodified verl source,
SGLang 0.5.20 and FSDP2. Native steps 1–4 recorded gradient norms
`0.2236328125`, `0.11474609375`, `0.21875`, `0.000026226043701171875`, with
learning rate `3e-6`, and saved the final native model/optimizer checkpoint.
Mixed-reward groups occurred in training; the native GRPO computation and
optimizer were not bypassed. KL regularization is also enabled, so gradient
norm alone does not attribute every update solely to task advantage.

There were 64 training responses over eight prompts and 32 validation
responses over four prompts. Training scored 52/64 and validation 15/32.
These small selected cohorts are integration evidence, not full MBPP accuracy
or evidence of improvement over the first run's different validation cohort.
All 96 generation records linked to their reward receipts; native token IDs,
response masks and logprob lengths matched. Stop reasons were 95 `stop` and
one `length` (4096 tokens). That incomplete response earned a labelled format
zero without creating a VM. The other 95 samples ran in real DSec microVMs,
and all 95 were stopped. Infrastructure errors were zero. The trainer exited
successfully; its GPU process group was gone after completion.

Evidence on the experiment host: `mbpp-verl-preparation/verl-pilot-r3/` contains
`launch.json`, `trainer.log`, `acceptance-summary.json`, `generation-evidence/`,
`execution-evidence/`, native rollouts/validation files and the final checkpoint.
The first failed generation attempt and the zero-gradient run are retained.

## Fixed generated-candidate concurrency

Use the first completed run's 32 validation responses (nine original positive
scores), repeated twice to produce a fixed 64-execution cohort. This cohort is
replayed unchanged at every requested concurrency level. Each candidate has a
fresh writable VM with the same four shared EROFS layers; the immutable caches
are warm and no ready VM pool is used. All 64 verdicts matched their original
scores at every level, including zeros: this is score consistency, not 100%
model accuracy. All 256 VMs stopped; no candidate timeouts, infrastructure
errors or scheduler QUEUED responses were recorded.

| Requested execution concurrency | Wall seconds | Executions/s | p50 seconds | p95 seconds |
| --- | --- | --- | --- | --- |
| 8 | 9.174 | 6.976 | 1.097 | 1.373 |
| 16 | 6.007 | 10.655 | 1.387 | 1.756 |
| 32 | 4.926 | 12.991 | 2.262 | 2.808 |
| 48 | 4.962 | 12.898 | 2.938 | 3.721 |

| Requested concurrency | Mean create/admission s | Mean execution s | Mean cleanup s | Sampled host CPU peak | Minimum host MemAvailable MiB |
| --- | --- | --- | --- | --- | --- |
| 8 | 0.781 | 0.235 | 0.110 | 20.9% | 28008 |
| 16 | 0.936 | 0.287 | 0.177 | 55.5% | 27441 |
| 32 | 1.321 | 0.474 | 0.367 | 67.5% | 26442 |
| 48 | 1.647 | 0.653 | 0.486 | 60.1% | 25440 |

CPU is the average of the entire host's 24 logical CPUs, sampled every 0.25 s,
with iowait excluded from busy time. Memory, network and disk counters also
cover the whole host, including background activity. These are sampled peaks,
not instantaneous maxima or per-VM resource attribution. At concurrency 32,
host disk busy reached 100% in one sample; at 48, disk throughput reached
152.5 MB/s. Neither observation alone proves disk is the limiting component.
The sampler reads existing proc/sys counters; its observer cost was not
benchmarked separately. Linux's guest accounting is verified in the
[kernel implementation](https://github.com/torvalds/linux/blob/master/kernel/sched/cputime.c).

The observed plateau is around 32 requested concurrent executions for this
short cohort: 48 adds latency without improving throughput. Creation/admission
is the largest measured per-execution phase; this timing includes client,
control-plane and storage work, not just Firecracker boot. This does not
establish a sustained maximum, an instantaneous 48-running-VM peak or a limit
for other workloads. Use 32 as the initial load setting for this case. After
all tests the dedicated worker had no live or queued rollout; the earlier
kernel-related failed creation remains as evidence.

Evidence: `generated-cohort-validation-r2-repeat2.jsonl` with its SHA-256 in
`generated-load-r1/summary.json`; per-execution receipts, `phase-summary.json`
and `host-concurrency-{8,16,32,48}.jsonl` under the same root. Replayed programs
are not new training samples and were never executed on the host.

## Requested generation concurrency versus effective capacity

When increasing the full-run response limit to 8192 and requested generation
concurrency to 32, SGLang's `/v1/loads` showed only six running requests and an
effective maximum of six, with requests waiting. The pinned SGLang cache
configurator caps hybrid-model concurrency by GDN state slots per request;
`max_num_seqs` alone did not increase that capacity. This is a generation-engine
resource limit, separate from DSec sandbox admission.

The launcher now exposes `--gpu-memory-utilization` and `--mamba-cache-slots`.
With a 0.6 inference memory budget and 160 state slots, the runtime interface
confirmed 32 running requests and an effective maximum of 32. The sampled GPU
allocation was 11002 MiB on the 16376-MiB RTX 4080. State precision and sampling
parameters were unchanged. This validates effective concurrency, not a completed
full-epoch accuracy comparison or worst-case training memory at 8192 tokens.
Superseded partial evaluations are retained as diagnostics and excluded from
the new before/after comparison.

Evidence: `mbpp-verl-preparation/full-grpo-r3/sglang-runtime.json`, `launch.json`,
`provenance.json`, generation/reward receipts and `trainer.log` on the host.

## Completed full-epoch GRPO and held-out evaluation

Qwen3.5-2B completed one native verl GRPO epoch over all 374 original MBPP
training tasks: eight responses per task, 2992 training responses and 187
optimizer updates. The final checkpoint's 144 optimizer parameter states all
record step 187. The run uses Non-Thinking mode, an 8192-token response limit,
32 requested/effective generation slots and the sampling/LoRA preset above.
The same 500 held-out test tasks received eight responses each before and
after training. Test scores were not used for gradients or checkpoint selection.

| Metric | Before | After | Change |
| --- | --- | --- | --- |
| Successful responses / 4000 | 1591 | 1691 | +100 |
| Mean sample success (pass@1 estimate) | 39.775% | 42.275% | +2.5 percentage points |
| Tasks with at least one success in eight samples | 324/500 (64.8%) | 342/500 (68.4%) | +18 tasks / +3.6 percentage points |
| Responses stopped by the length limit | 86 (2.15%) | 108 (2.70%) | +22 |
| Model-format zero rewards | 105 (2.625%) | 123 (3.075%) | +18 |

Of the test tasks, 41 became solved and 23 became unsolved in the sampled
cohorts. The paired-task bootstrap 95% interval for the mean sample success
change is +0.975 to +4.0 percentage points. Before/after responses are independent
draws under the same sampling preset, not matched random streams. These are
results from one epoch/run and the three original public assertions per task;
challenge tests are excluded. The result supports this integration's ability
to complete training and evaluate a held-out improvement, not a general claim
that all tasks improve or that DSec outperforms another sandbox.

The original final evaluation failed after checkpoint 187 was saved. SGLang's
input-logprob row-selection allocation requested another 1.89 GiB while only
1.35 GiB was free. With the 248320-word vocabulary, a 2048-row FP32 matrix is
about 1.89 GiB, and its selected-row copy can coexist. The recovery evaluation
bounded this workspace to 128 rows (about 121 MiB per matrix), restored the
same final checkpoint and performed no additional optimizer updates. Response
length, concurrency, state precision, agent/scoring code and sampling preset
were unchanged. Why the initial evaluation did not hit this peak is not yet
established. The failed partial final evaluation is excluded entirely.

The replacement evaluation exited zero and saved 4000 generation records plus
4000 reward receipts, with no generation, infrastructure or cleanup errors.
All 3877 execution-scored VMs stopped; the 123 format zeros created no VM.
The complete comparison validates task coverage, output hashes, reward linkage
and token/mask/logprob lengths. Native training rows omit receipt IDs, so
training linkage uses task/output-hash/score multiplicities rather than a
particular native uid for identical outputs. Because native verl dumps the
last training batch after validation, its missing 16 rows were reconstructed
from the original saved generation tokens and original reward receipts, with
that derivation explicitly marked; those samples were not rerun.

Evidence: `full-grpo-r3/comparison-recovered.json`, `completion-recovered.json`,
`step187-recovery.json` and checkpoint 187; `post-eval-r1/validation/187.jsonl`,
`trainer.log`, `provenance.json` and the complete generation/reward receipts.
The original failure record remains intact. The public launcher now supports
evaluation-only checkpoint recovery, and the comparison accepts a complete
replacement post-evaluation while checking model/data/sampling identity.

The completed-case application wheel passed all 15 Linux application regressions
after the GPU job ended. Its installed `dsec-mbpp-compare --post-run` command
recomputed the saved cohorts and reproduced the reported before/after/training
metrics, failure counts and bootstrap interval. The wheel SHA-256 is
`972971eb0d82ed98a3ec01ee82f2cb02cbbe84a0d6c12ea8ca3b92e2f2d6b6bb`;
this identifies the subsequently verified application package, not the wheel
used to generate the original training trajectories. Documentation links,
source-export regressions and local package construction also passed.
