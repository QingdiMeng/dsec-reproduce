# SWE-Gym first-use record

**English** · [简体中文](SWEGYM_FIRST_USE.zh-CN.md)

Date: 2026-10-25. Execution baseline: local commit `d1b4a40`.

The immediate gate is one real repository task through the public HTTPS API,
with baseline reward 0, official-patch reward 1, complete evidence and resource
retirement. No model or RL result has been measured yet.

| Input | Pin |
| --- | --- |
| Dataset | `SWE-Gym/SWE-Gym-Lite`, train, 230 rows |
| Parquet SHA-256 | `f3a7cd934e8cc523b6053298d0abb2c82fd7db2b83f9f2ccba5944545aaa4eb1` |
| Official evaluation fork | `SWE-Gym/SWE-Bench-Fork@242429c188fcfd06aad13fce9a54d450470bf0ac` |
| First task | `python__mypy-15413` |
| Official image | `xingyaoww/sweb.eval.x86_64.python_s_mypy-15413@sha256:f069dfc74592d438ad870bbc6dfb369bff1b125d21237ead49190b414f5f3456` |

The tiny patch is an execution/scoring probe. It is not evidence that a small
model can solve this task, nor a representative SWE-Gym success-rate estimate.

## Issues found during preparation

1. The dataset ID and published image name differ (`python__mypy` versus
   `python_s_mypy`). Inferring the former produced a registry 401. The correct
   public image was resolved through the official publisher's Hub metadata.
2. A 90-second image-pull timeout interrupted the client after several layers
   completed. Resuming the same immutable image reused the layers successfully;
   no daemon/proxy configuration or host service was restarted.
3. Official images assume root for pip installation, but DSec tasks run as
   UID 1000. The repository is already writable; the Conda site-packages directory
   is root-owned. Prepare a writable HOME/pip user install configuration instead
   of granting guest tasks root or recursively copying/chowning Conda layers.
4. `make_test_spec` also constructs build-time environment specifications and
   may fetch requirements. For a frozen instance image, the adapter uses the
   same official `make_eval_script_list` and `TestSpec.eval_script` directly,
   with no change to evaluation commands, expected tests or the official scorer.
5. A single verifier command currently has a 30-second limit. This entry point
   must report incomplete verification rather than silently score it as zero;
   long-running task support remains an explicit integration gate.
6. Whole-catalog digest binding prevented old instance history from being read
   after adding a new task image. Added explicit pinned catalog history for
   additive updates: the old environment, CID pool and host/network policy must
   all remain identical. Changed images/limits, unknown digests and history drift
   reject. No old instance journal is rewritten and no new root privileges are
   needed to publish an environment within the existing fixed Node scope.
7. First real API creation reached guest image import, then exhausted the old
   15-second authentication/readiness wait. It never reached task evaluation;
   the failed instance was fully retired (no CID or private disk remained).
   Added a separate operator-pinned guest startup budget (1–240 seconds), keeping
   the QMP deadline at 15 seconds and tool-command bounds unchanged. A new
   environment ID carries this budget; the prior environment/failed record is
   retained unchanged. Authentication and task readiness share one deadline.
8. The initial private orchestration script reserialized a catalog with an extra
   newline, so its deployed digest differed from the saved input's digest. Its
   next upgrade correctly rejected the history. Pin the actual deployed bytes
   and copy catalogs byte-for-byte; do not relax digest verification. A read-only
   preflight checked every historical descriptor before retrying.
9. After the startup wait was corrected, guest Docker's `vfs` importer exhausted
   the 8 GiB guest disk by copying cumulative layer contents. The host still had
   free disk. The guest builder now explicitly selects `vfs` or `overlay2`, with
   no automatic fallback; the SWE-Gym guest selects `overlay2` on its ext4 root.
   This changes the prepared guest, not the physical host Docker driver.
10. Local RPC serialized a typed `ServiceError` as the literal type
    `ServiceError`, dropping its cleanup details. Preserve the underlying kind
    and details through the existing RPC, and through committed-create replies.
    A real local-RPC regression checks that cleanup proof reaches the client.
11. The official image's Git does not implement `git apply --allow-empty`.
    Skip patch application for a zero-byte prediction, then execute the full
    official verifier; use ordinary `git apply -v` for nonempty patches. Tool
    invocation errors are invalid evidence, not negative rewards. Acceptance
    requires an official test report as well as the expected numeric score, so
    a rejected patch cannot masquerade as a successful baseline check.
12. A writable `/testbed` directory did not make its existing `mypy.egg-info`
    writable. The next pair reached official tests and scored 0/1, but logs
    showed the official editable installation failed before pytest continued.
    Prepare a derived image with the unchanged repository owned by UID 1000,
    using a stopped-container export and Docker `COPY --chown`, without executing
    task code on the physical host or changing Conda. Repeat the pair and inspect
    installation output as well as test statuses.

## Acceptance status

**Passed on Linux, 2026-10-25, final pair `api-r6`.** Both episodes used the
installed optional application and the public HTTPS API → IAM/Placement → mTLS
Node → Edge → QEMU/KVM Container path. Each received a fresh private workspace.

| Episode | Official target test | Score | Episode wall time | Official verifier | Cleanup |
| --- | --- | --- | --- | --- | --- |
| Empty prediction | `testReturnAnyLambda` failed | 0 | 35.552 s | 4.307 s | stopped |
| Official patch | `testReturnAnyLambda` passed | 1 | 39.384 s | 4.558 s | stopped |

Both logs contain a successful official editable installation of mypy before
pytest. The pinned official test script and parser/scorer were retained. Each
test run collected 11,410 items and selected the one upstream regression test;
the other 11,409 were deselected by the official command. These episode timings
include cold guest preparation, commands, verification and stop. They are not
warm-pool launch latency or a system performance comparison. The complete private
acceptance service took 132.077 seconds, including control-plane startup/shutdown.

The final prepared image is
`sha256:a015eb00230e773bdb5fdef7b873ef1c169b07ba4b5bb3362c798020a6f7b57d`;
its classic-Docker inner config ID is
`sha256:1b5708c71027eb7230ebc5f988cff2077047773036187ff649e7a022d5727589`.
The 8 GiB overlay2 guest SHA-256 is
`09bab6b22111f7309987173707699b6b3b6ebca87a371cfb1d34657fc4bd5462`.
Repository contents and Conda were retained; the derived image changes repository
ownership and writable HOME/pip settings, with no gold solution baked in.

Final API checks proved physical leases, CID reservations, private writable disks
and IAM usage were zero. A separate read-only audit checked both VMMs stopped,
the host/network brokers attested retirement, and their cgroups, veths, netns
handles and private disks were absent. All five earlier attempt namespaces also
had zero IAM usage and no pending admissions. The original five host containers
kept their IDs, PIDs, start times and networks. No GPU or new root permissions were
used; the installed network helper remained unchanged.

Evidence retains both episode reports, full official test logs, orchestration
report and retirement audit under the private experiment's `api-r6` directory;
credentials and task answers are not included in the release archive. Earlier
failed attempts are preserved separately. The source archive and optional wheel
boundary checks pass; core regressions: 608 discovered, 599 passed and 9 skipped;
installed Linux targeted regressions: 133 passed; optional application tests with
the actual pinned official harness: 11 passed. The eight host-isolation guard
mutants also passed before the final application-only correction.

The optional application does not alter the core runtime's benchmark dependencies.
This proves one task's execution/scoring integration, not model accuracy, all
230 tasks, a three-backend performance comparison, or completed SAO training.

## Training boundary

The intended target is [Single-Rollout Asynchronous Optimization](https://arxiv.org/html/2607.07508),
not asynchronous GRPO. Critic training, behavior log probabilities, policy version
tracking and observation masking belong to the external trainer/agent adapter.
DSec owns sandbox lifecycle, scheduling, execution evidence and recovery. The
existing 16 GiB GPU and available model/framework installations still need a
separate actor/critic memory feasibility check before SAO training is claimed.

References: [SWE-Gym project](https://github.com/SWE-Gym/SWE-Gym),
[official environment/scoring fork](https://github.com/SWE-Gym/SWE-Bench-Fork).

## Real model trajectory and resource correction: 2026-10-26

Added the optional `dsec-swegym-rollout` scaffold. Model serving and tokenization
remain outside the sandbox core. It uses the actual Qwen3.5 XML tool template,
accepts validated batches in order, retains full replies/Thinking, records native
SGLang IDs/logprobs and extends prior sampled token prefixes without re-tokenizing
them. Captured observations have loss mask zero. The final diff is evaluated in
a fresh API-created sandbox after the agent sandbox stops. Budget exhaustion
still permits an actual official score; infrastructure/unknown outcomes do not
become valid zero rewards.

The frozen existing Qwen3.5-2B checkpoint used Thinking coding sampling:
temperature 0.6, top_p 0.95, top_k 20, min_p 0, presence_penalty 0,
repetition_penalty 1. Response/context limits were 32,768/65,536 tokens, with
30 model turns and a 1,200-second episode budget. SGLang 0.5.20 and Transformers
5.12.1 were already installed; no checkpoint was downloaded or updated.

| Pilot | Turns / shell calls | Generated / total tokens | Patch | Official result |
| --- | --- | --- | --- | --- |
| `api-r1` | 30 / 30 | 3,955 / 14,215 | empty | valid score 0; step cap |
| `api-r2` | 30 / 30 | 3,469 / 15,153 | empty | unscored; fresh verifier VM failed startup |

Both had zero tool-format errors. Independent checks matched every saved prompt
prefix, sampled ID, logprob and policy mask against the flattened trajectory.
Both models spent their budget inspecting/searching without changing source;
the first ended in repeated Git-history queries. This is evidence about this
checkpoint/task/scaffold, not all Qwen models or the effectiveness of agent RL.
Do not aggregate these pilots into a 0/2 benchmark result: one was unscored.

Additional first-use issues and repairs:

- A manually launched model service lacked its venv's `ninja` on PATH. Added the
  existing venv bin directory; did not replace FlashInfer or install new kernels.
- Transformers could return a mapping from `apply_chat_template`; iterating it
  sent field names to SGLang. Explicitly request flat IDs, support the mapping's
  `input_ids`, and validate integer tokens before submitting a request. Retain
  native HTTP rejection details. The tokenizer-only extra also needs Jinja2.
- Fresh tool shells initially selected Conda's base Python. Activate the image's
  prepared `testbed` environment on every command and preflight its Python prefix
  and dependencies before model sampling. The corrected second pilot had no
  dependency-install workaround. Its lack of edits remains a model result.
- The second pilot's verifier failed with a connection reset, but retained a
  typed failed-create cleanup proof. Kernel logs identified **cgroup OOM**, with
  about 1,000.4 MiB anon, 268.5 MiB file and 11 MiB kernel memory under the
  unchanged 1,280 MiB limit. File memory included 177.6 MiB dirty and 90.9 MiB
  writeback pages from buffered guest-disk I/O; host available memory was still
  about 23 GiB. This was not host-wide exhaustion or a valid negative score.

### Direct-I/O acceptance

Introduced the explicit environment `disk_cache: direct` mode; existing records
remain buffered. Direct I/O bypasses the host disk-page copy with flushes enabled,
without raising guest RAM, VMM allowance or IAM demand. Retirement persists
available cgroup peak/OOM/CPU/I/O counters before kernel removal. No root helper,
launcher, host Docker, firewall policy or privilege installation was changed.

The installed repair passed a fresh baseline/oracle pair through the public API
(`api-direct-r1`), including official editable installation and complete cleanup.
Both live root-disk file descriptors were independently observed with O_DIRECT.

| Episode | Score | Episode time | VMM cgroup peak | Sampled max host dirty pages | OOM kills |
| --- | --- | --- | --- | --- | --- |
| Baseline | 0 | 37.096 s | 1,108,639,744 B | 73,728 B | 0 |
| Oracle | 1 | 43.766 s | 1,106,644,992 B | 77,824 B | 0 |

Peak/OOM values come from kernel counters retained at retirement; dirty-page
maxima are 0.5-second samples, not exact peaks. This is two bounded executions,
not long-duration or concurrent stability. Edge image-copy/hash file caching is
outside the VMM cgroup and is not claimed to disappear. This fix is not DAX,
shared guest RAM, warm-image publication or a Docker resource comparison.

Core regressions: 611 discovered, 602 passed, 9 skipped; eight controlled
host-isolation mutations passed. Optional application tests: 24 passed with the
real pinned harness. Source/wheel/docs boundaries passed. Full model and failure
evidence, token audit, scoped kernel OOM log and direct-I/O audit are retained in
the private 2026-10-26 experiment, with no keys/client credentials in release
sources. The model service stopped and GPU returned to 35 MiB idle; all private
VM disks/CIDs were retired and original host containers stayed unchanged.

### Next training gate

No SAO update has run. The installed native verl already skips observation
tokens in its GAE recurrence and provides actor/critic and off-policy correction
components; reuse and verify those instead of adding an RL algorithm to DSec.
The remaining external-trainer gate is actual critic updates, DIS correspondence,
weight-version handoff and single-GPU actor/critic feasibility, then a measured
training/evaluation loop. A candidate raised by the user is
[MiniCPM5-1B](https://huggingface.co/openbmb/MiniCPM5-1B); it has not been downloaded
or evaluated in this case. Its model-specific template and sampling must be
verified before using the scaffold; no claim of SWE-Gym success follows from its
published general tool-use results.

## Real task EROFS/pmem/DAX migration: 2026-10-26

The bounded `python__mypy-15413` case now runs through the installed core wheel,
HTTPS API, mTLS Node and QEMU Container path with three shared EROFS layers.
The official fork, base commit, task instructions, test/gold patches, verifier
script and scorer were unchanged. Gold data remains outside the published layers
and the agent workspace. No model or trainer ran in this acceptance.

The reusable publication tool exports a **stopped** pinned local image, preserves
its runtime Config and numeric file/tar metadata, and partitions `/usr`,
`/opt/miniconda3` and `/testbed` into uncompressed no-inline EROFS. Their contents
are removed from the new lean task archive; mount-point directories and remaining
files are retained. No task command runs on the physical host, and no host Docker
storage driver is changed. Cross-partition hardlinks and image-declared volumes
are rejected. This is flattened-export preparation, not raw OCI whiteout parsing.
The application recipe is in [the case guide](../../apps/swegym/README.md#move-shared-task-files-to-erofsdax).

| Artifact | Size / identity |
| --- | --- |
| Source prepared image | `sha256:a015eb00230e773bdb5fdef7b873ef1c169b07ba4b5bb3362c798020a6f7b57d` |
| Preserved runtime Config SHA-256 | `ea704a31e5df1d48bb3c37455b9c3ffe96ec2b7d9db53056f09721e5f09ef84a` |
| Shared EROFS capacity | `/usr`: 768 MiB; Conda: 1,664 MiB; repository: 128 MiB |
| Lean task archive | 173,619,200 B; SHA-256 `9ee7355b7b8025089f07f98c0e2b46d72b5d8301b79a903c4c98a8335218f00b` |
| New inner image | `sha256:5305804952f7969bc57263e9ee6a287a6992364ccca14e1ef448aa4018154916` |
| Guest template | 2 GiB private disk; SHA-256 `2b25dd08079e7ea2846e4c4b229d631ee21895dffabaf213805afecdce773279` |

The flat uncompressed export was 2,560,272,384 B. It and the lean archive are
publication representations; neither is a registry transfer-size measurement.
The shared uncompressed layers still occupy local storage. Their combined
2.5 GiB pmem window requires about 40 MiB of guest struct-page metadata. This is
not a claim of net disk savings, cross-image deduplication or smaller whole-VM RAM.
A preserved `/miniconda.sh` installer remains in the private archive; the tool
does not guess which unselected files are safe to delete.

Two probe VMs concurrently activated the actual `testbed` Conda environment,
imported pytest/typing_extensions/mypy, and mmap-read actual Git, Conda Python
and `mypy/checker.py`. Every selected file reported `STATX_ATTR_DAX=true` from
inside the task container. Each VMM mapped the same host device/inode for each
layer with `r--s`, nonzero Shared_Clean and zero Private_Dirty. These checks
exercise real application files in addition to the reserved layer probe.

After retiring those VMs, a fresh official baseline/oracle pair ran concurrently:

| Episode | Official score | Episode wall time | VMM cgroup memory peak | OOM / OOM kills |
| --- | --- | --- | --- | --- |
| Baseline | 0 | 14.333 s | 946,941,952 B | 0 / 0 |
| Oracle | 1 | 14.155 s | 949,370,880 B | 0 / 0 |

Both completed official editable installation and the selected test under the
same guest RAM (1,024 MiB), outer VMM limit (1,280 MiB), inner container limit
(512 MiB) and one-vCPU configuration. The private disk/IAM minimum is now
2,048 MiB for this prepared case; it is not a default for arbitrary SWE tasks.
Oracle edits used the private OverlayFS upper; the shared artifact hashes stayed
unchanged. Four total VMs were retired, with no OOM events, no residual writable
disks/cgroups/CIDs, zero physical/IAM concurrency leases, and no cleanup errors.
The original five host-container identities, PIDs, start times and networks were
unchanged. The QEMU launcher and network helper were unchanged by this migration.

Evidence: private `2026-10-26/shared-task-evidence` contains the 28-check report,
independent retirement audit, split descriptor, build/service logs and both full
verifier logs. Two failed **probe** reports were retained: the first omitted the
existing harness's exact Git safe-directory setup, and the second omitted the
copied statx function's constant. Neither reached scoring. Both cleaned up;
the corrected probe passed without changing file ownership, system permissions,
verifier logic or expected results. These orchestration mistakes are not valid
zero-reward model episodes.

The 152.616-second service wall time includes Edge restoration/hash work,
controller startup, two probe VMs, the deliberate mapping hold and the official
pair. It is not VM creation latency. Compared with the earlier Direct-I/O pair,
the template, imported image and prepared-cache state changed; the 14-second
results are not a controlled DAX-versus-block speedup. Runtime whole-file hashes
still touch layer data, so cold lazy-read performance remains unproven.
Only this task's environment/scoring migration is complete, not all SWE-Gym
cases, SAO updates or a model training/evaluation loop.

Regression evidence for this publication increment: 631 discovered, 622 passed,
9 platform skips in the clean source export; 18 focused layer/split tests also
passed on Linux. Source/wheel payload and documentation boundaries passed.

## Pre-training command/recovery/admission gate: 2026-10-26–27

Before model/SAO work, the installed core was exercised through HTTPS API, mTLS
Node and the real QEMU/EROFS/DAX Container environment. No GPU, model update or
new administrator privilege was used. The primary fault/cycle acceptance
`api-p0-r4` passed **56 checks across 11 VMs**, with at most two VMs at once.

| Boundary | Observed result |
| --- | --- |
| Longer shell command | 33.228 s command completed under a 45,000 ms request budget and a pinned 120,000 ms environment ceiling |
| Inspection during command | Status returned RUNNING in 0.145 s; colliding stop returned ServiceBusy before admission |
| Actual HTTPS response loss | Query by original request ID returned DONE; file side effect occurred once |
| Caller cancellation | Cancelling the waiting coroutine did not revoke/replay admitted work; original result was queryable and effect occurred once |
| Edge SIGKILL/restart | Same VMM PID and CID adopted; exact guest DONE proof reconciled the host intent; side effect occurred once |
| Timeout process tree | Partial output retained; a detached setsid child was absent afterward and the next command worked |
| Repeated concurrent lifecycle | Three two-VM waves completed in 9.627, 8.947 and 8.949 s; physical leases/CIDs/private disks were zero after every wave |
| Full Node | Extra create rejected with NoPlacement; read-only capacity waiter made no allocation and resumed after release in 2.013 s |
| Official scoring afterward | Baseline 0 in 13.035 s; oracle 1 in 13.139 s; full verifier logs retained |

The caller-side capacity waiter tests the admission/release boundary. It is not a
new durable task queue inside DSecClient; work queuing belongs to the existing
rollout scheduling layer or an external trainer. Unknown mutations are not
retried. This bounded run does not establish high concurrency, multi-host HA,
long-duration leak freedom or production readiness.

All 11 VMM cgroups reported zero OOM/OOM-kill events and were removed; kernel
memory peaks ranged from 936,292,352 to 948,977,664 B. Physical/IAM concurrency
leases, CIDs and writable disks were zero on completion. Shared layer hashes and
the original five host containers' identities/PIDs/start times/networks were
unchanged. The primary service's 372.375 s includes two complete Edge startup
passes; it is not VM creation or rollout-only latency.

Implementation boundaries:

- The operator pins the command ceiling in both prepared guest and execution
  catalog; defaults stay at 30 seconds and the supported maximum is 300 seconds.
  SWE-Gym tool and verifier budgets are independently configured and recorded.
  Model commands also fit the remaining episode budget; patch capture and fresh
  verification retain separate explicit budgets.
- Execution effects remain serially admitted, while VM/guest inspection does
  not wait on the long-command lock. A lost host response can become DONE only
  with the original VM's exact request ID/digest and durable DONE response.
  Missing, mismatched or unreachable evidence remains UNKNOWN.
- Linux helper subreaping covers orphan/setsid descendants on timeout/failure.
  The final candidate preserves successful Container background processes and
  uses container init to reap their eventual exits; FnCall still resets its
  whole container. Native session compile limit, host channel and guest proxy
  share the environment ceiling rather than retaining independent 30/40 s caps.

The `QemuCommandRecovery` finite model checked 12 distinct safe states; three
fault variants produced the required AtMostOnce, DoneRequiresExactProof and
InspectionDoesNotQueue violations. Four implementation guard mutations were
rejected by controlled regressions, alongside the three existing FnCall guard
mutations. CI now runs these checks and saves source/model/test fingerprints.
This binds reviewed boundaries; it is not an automatic whole-program refinement
or liveness proof. See CPH-020 in the architecture decision record.

First-use issues were preserved rather than counted as model scores: a generated
catalog omitted older explicit history pins; a new helper dependency was absent
from its inner-container mount; and an orchestration timer shadowed the Edge
start function. The last error interrupted the test after Edge SIGKILL. Its one
orphan VM was recovered through the normal operator SDK, its original command
proved DONE/once, and it was retired before rerunning the public API test.
Operator recovery is recorded separately and is not substituted for the later
successful public API recovery. A later compatibility check exposed Docker's
implicit init path: selecting its source binary did not prevent a shared `/usr`
mount from hiding `/sbin/docker-init` in the task. The final design mounts the
trusted binary read-only at reserved `/dsec-init`; an isolated guest publication
preflight proved PID1 there and retired its VM. Both rejected startup attempts
returned typed failed-create cleanup proof and left no CID allocation. Root
permissions and isolation guards were not relaxed to make a test pass.

Evidence is retained under private `2026-10-26/p0-evidence`: primary report,
full official logs, failed probe reports and the separate operator-retirement
proof. Application tests: 26 passed. Final candidate core regression: 639
discovered, 629 passed, 10 platform skips; Linux command/layer regressions:
54 passed without skips. The core wheel boundary remains 213 runtime modules
and 224 entries. No tasks, credentials, model weights or experiment outputs
are added to the released source archive.

### Final installed candidate confirmation

After the background-process compatibility correction and fixed root init
mount, `api-p0-final3` passed **23 targeted checks across three fresh VMs** on
2026-10-27. A successful Container command left its intended background process
alive; explicit termination was reaped by PID1 with no `/proc` zombie remaining.
A separate timeout retained partial output and removed a detached setsid child.
A native session command completed in **41.253 s** under a 45-second request
budget, then remained usable. Official baseline/oracle were again **0/1** in
12.239/12.359 s. The private disk and every resource lease were retired.

These checks supplement the earlier 56-check restart/cycle run, not a new model
benchmark. The independent audit checked all **14 VMs across those two runs**:
no OOM events/kills, no remaining cgroups/private disks/CIDs, and unchanged fixed
launcher/AppArmor-helper/config identities. Both runs preserved the original
host-container identities and networks. The final service wall time was
241.748 s including Edge initialization and the deliberate 41-second command;
it is not a creation-latency measurement.

Final core wheel SHA-256: `eb8ee429322b8b9df59e3f335f652f0706b8374db09e9d17a315a1fab13638bd`. Final guest SHA-256:
`6b896ce92b45e312f44affbda0249be2d19ec9ddebee1583e5316b9f6353da76`. The wheel payload matches the final runtime sources; the
source archive also includes the operator builder, regressions, model and CI
guards. Detailed reports, full test logs and independent audit are retained in
private `2026-10-26/p0-evidence/final` and `independent-audit.json`.

## Real model sampling after the P0 gate: 2026-10-27

After the host reboot, the existing frozen Qwen3.5-2B checkpoint was rechecked
against its content manifest and served with the previously recorded SGLang
configuration. No model download or system privilege update was needed. The
installed core and `swegym-mypy-p0-final3` EROFS/pmem/DAX environment were reused
through HTTPS API and mTLS Node. Sampling retained Thinking coding settings,
32,768 response tokens, 65,536 context tokens, 30 turns and a 1,200-second budget.
Tool and verifier budgets were 45,000 and 120,000 ms respectively.

The first new episode (`api-model-r1`) exposed an application collection bug:
the model left an unfinished nested Git clone, and broad `git add -N -- .`
failed before patch extraction. This episode remains **unscored**, with its
original trace and cleanup evidence; it is not a valid zero-reward sample.
Collection now lists untracked paths using NUL delimiters, adds ordinary files
and symlinks with literal pathspecs, and records excluded untracked directories.
Tracked changes remain in `git diff --binary HEAD`. Ignored files stay excluded;
other Git errors still fail explicitly. A partial clone cannot block collection
of the parent repository's actual changes. No verifier, task or expected test
was changed. Five Git integration regressions cover complete/incomplete nested
repositories, literal filenames, symlinks, ignored files and actual Git failure.
All **31 application tests passed on Linux**, including the pinned real harness;
the three source-release packaging checks and documentation-link checks passed.

A fresh independently sampled episode (`api-model-r2`) then completed:

| Measurement | Result |
| --- | --- |
| Model turns / shell calls | 30 / 30 |
| Termination | Step limit |
| Format errors / truncated responses / shell timeouts | 0 / 0 / 0 |
| Nonzero shell exits | 8; retained in feedback, chiefly unsuccessful searches |
| Sampled / total trajectory tokens | 3,220 / 13,946 |
| Candidate patch | Empty; no source edits |
| Official fresh verifier | Valid score **0**; editable installation completed and `testReturnAnyLambda` failed |
| Generation / model shell wall time | 31.135 / 10.557 s, summed sequential request intervals |
| Agent episode / fresh verifier / total | 49.055 / 15.403 / 64.996 s |
| Agent / verifier VMM cgroup memory peaks | 940,781,568 / 947,785,728 B |
| OOM events or kills | 0 in both VMM cgroups |
| Retirement | Both cgroups/private disks removed; physical/IAM/CID leases zero |

Independent audit matched every saved prompt prefix, response ID sequence,
sampled-token logprob and flattened policy-token mask. Observation tokens had
mask zero and no behavior logprob. The original host containers' identities,
PIDs, start times and networks stayed unchanged. The temporary model service was
stopped afterward; GPU use returned to 35 MiB. Evidence lives under private
`2026-10-27/swegym/evidence`, including both episodes, full official logs,
independent audits and the Linux regression log. The failed first episode is
kept separate from the scored second episode.

The temporary model supervisor used systemd's default control-group stop signal.
After scoring, its planned stop also signalled the SGLang scheduler; SGLang
reported that exit and killed its own process tree, leaving a signal-failed unit
marker. No model process/cgroup remained. The status and full model journal were
saved before clearing the marker. This was a post-evaluation supervisor shutdown
issue, not a failed generation or sandbox OOM; parent-first model termination
remains a launcher follow-up outside DSec.

The second trace found relevant source but continued searching, including
repeated queries for a nonexistent diagnostic identifier, instead of editing
and testing a fix. This is one frozen-policy pilot, not a benchmark success-rate
estimate. It establishes a real sampling-to-scoring path on the repaired storage
environment, not model competence or completed RL. Edge startup still validates
historical artifacts; that control-plane setup time precedes the episode and is
excluded from the times above.

The application wheel used in this run has SHA-256
`47cdd22ae2f35f4f0c1863836337e67cea393ac0e8db56e9e38f4cae35fd1382`.

### External SAO trainer component check

CPU-only probes of the existing installed verl compared masked GAE against an
action-only reference, including extreme observation values. They also matched
native bypass REINFORCE/token-IcePop gradients against detached direct-ratio
weights away from clipping endpoints. Excluded and observation tokens had zero
gradient. Source hashes and exact settings are retained in
`sao-component-probe.json`. These probes ran no model update or critic update.

The [SAO paper](https://arxiv.org/html/2607.07508) specifies an open DIS interval;
native IcePop accepts the interval endpoints. That boundary difference still
needs explicit alignment. Actor/critic memory feasibility, frozen-attention
critic training, the two-to-one critic/actor update schedule, asynchronous
weight-version handoff and held-out before/after evaluation remain unaccepted.
Those are external trainer/application responsibilities; this increment adds
no training algorithm or model management to the DSec runtime.

## 2026-10-27: upstream OpenHands integration

The optional `integrations/openhands` package implements the real upstream
Runtime boundary; `apps/swegym` owns the separate task/patch/official-verifier
runner. CodeActAgent, its prompts, function-call parser, controller and history
remain from SWE-Gym's recommended fork at
`e644a2ca45c3623b27a7e6c169e3d479f0a87fbc` (CodeActAgent 2.2).
SAO identifies OpenHands but not an exact commit, so this pin is a documented
choice rather than a claim of the paper authors' exact environment.

First-use preparation exposed these distinct issues:

| Issue | Resolution and boundary |
| --- | --- |
| Original LiteLLM 1.52.5 wheel unavailable from PyPI | Build unmodified official tag commit `33ceb7ca1ffab1401decca773253b58d5fc461a2`; retain wheel hash and original upstream constraints. Do not silently substitute a newer dependency stack. |
| Prepared task Python is 3.11; coding tools require 3.12 | Publish a separate portable interpreter/dependency layer; scrub tool Python variables from task shell initialization. No task interpreter replacement. |
| Legacy model allowlist predates Qwen3.5 | Explicit endpoint native-function-calling capability in the LLM wrapper; no changes to the upstream agent, parser or tool schemas. |
| Added environment omitted the last catalog pin | Add its explicit historical digest; keep historical environment definitions and integrity checks unchanged. Failure happened before sandbox creation. |
| Tool archive was relative to its intended mountpoint | Publish `opt/dsec-openhands/...` in a guest-root filesystem layer. All four EROFS/DAX mounts succeeded, then the guest refused the missing composed mount source; no OOM and failed-instance cleanup was proved. A publication regression now checks this layout. |
| Private test-launch wrapper lost quotes during remote generation | Transfer the locally compiled wrapper as a file. This was test orchestration, before VM creation; preserve the failed attempt separately. |
| Conda activation decorates PS1 | Restore the PTY framing after environment activation; a controlled regression checks that prompt decoration is not command feedback. |
| A background launcher passes ignored SIGINT to its PTY child | Reset SIGINT/SIGQUIT in the PTY child before exec. The inherited-signal regression failed deterministically before the fix and passed afterward; real API Ctrl+C and hard timeout subsequently passed. |
| Native HTTP/session-file extension guard called `RemoteNode.health()`, but only `probe()` existed | Implement the same live health interface over the existing read-only `node_probe` RPC. Add controller → RemoteNode → NodeRPC regression for supported/unsupported HTTP and session files, plus lost-probe no-dispatch/no-retry. All 31 related control/transport regressions passed on Mac and Linux. The candidate core wheel differs from the prior deployed wheel in exactly `dsec/host/node_transport.py`. |
| Missing upstream Git initialization invoked a pager in `git show` | Reuse upstream `INIT_COMMANDS` unchanged, including Git identity/no-pager alias, and preserve the upstream shell umask. Add a real Git-inspection conformance check. The affected model run was interrupted and retained separately. |
| Cancelling a model run detached an awaiter while native HTTP still ran | Core correctly returned `ServiceBusy` before admitting stop. Recover that instance by the original stop ID and save separate recovery proof. The adapter now fences submissions, drains tracked SDK futures, then stops. Controlled tests cover that order and sticky UNKNOWN; real API cancellation subsequently completed its admitted command before successful retirement. |

The initial tool-health loop hid the RemoteNode API error behind a generic readiness
failure. Startup evidence now retains preflight/launch results and the last HTTP
response/error; readiness uses a wall-clock limit, and protocol/capability errors
fail immediately. The guest companion and shell were alive in the failed VM;
this was not a guest OOM, service-launch failure or missing external network.

The coding tools use guest loopback through the existing native HTTP operation;
they add no host listening port, new root helper, model manager or agent-specific
API Server/Edge branch. Tool dependencies are prepared before episodes and can
be shared across tasks. The reusable real-API conformance test is independent of
SWE-Gym and exercises terminal/Python state, stdin/poll/interrupt, timeout
settlement, upstream editor/undo, native file ranges and retirement.

The installed Linux optional application regressions passed all 34 tests,
including the pinned real harness and upstream loop classification. Adapter portable regressions passed 14;
the real-API conformance test is separately opt-in. Wheel/source boundaries and
documentation checks are separate from functional scoring. Frozen OpenHands
completion logs are not accepted as SAO training token/logprob tensors.

The final real-API conformance run passed all 16 assertions and proved its sandbox
retired. In the same published environment, separate fresh official baseline
and oracle runs scored **0** and **1**, respectively, with both sandboxes stopped.
These are execution/scoring controls, not a model solve rate.

The first OpenHands model attempt reached 43 sampled calls and the native
controller's repeated-action/observation detector. Its initial harness treated
all agent ERROR states as unscored, so no candidate was collected. Preserve that
attempt as unscored; do not reconstruct a patch after its sandbox is gone.
The harness now recognizes this exact pinned loop message only when the original
StuckDetector confirms the saved history, then captures the actual candidate and
uses fresh official grading. Other ERROR states remain unscored. The new rule
was checked against all 87 saved events of that actual attempt as well as
controlled positive/negative cases. A later pager-affected attempt was safely
interrupted and recovered; neither is counted as a scored comparison.

A third attempt produced the undeclared name `str_replace_bash`. The upstream
parser correctly rejected it without executing the call, but the initial
harness did not yet collect/grade such model-format failures. Preserve that
attempt as unscored too. The harness now matches parser errors against the
actual completed reply and declared tool list before allowing candidate capture;
it never aliases an invented tool name or repairs malformed arguments.
OpenHands' `num_retries=0` did not disable LiteLLM's independent OpenAI client
retry layer. The final run explicitly supplied `max_retries=0`, and used a
600-second per-model-request deadline for the 32,768-token cap.

### Final upstream-agent model pilot

Qwen3.5-2B Thinking, temperature 0.6, top-p 0.95, top-k 20, min-p 0,
presence penalty 0 and repetition penalty 1 ran through the unchanged upstream
CodeActAgent/controller/parser with the completed DSec runtime profile:

| Measurement | Result |
| --- | --- |
| Model completions / tool calls | 24 / 24 |
| Termination | Native `agent_stuck` detection after repeated searches for `current_function` |
| Response truncations | 0 |
| Actual captured candidate | Empty, 0 bytes; no repository fix |
| Fresh official verifier | Valid score **0** |
| Agent phase / total with fresh verification | 69.817 / 82.893 seconds |
| Agent / verifier outer VMM cgroup memory peaks | 965,877,760 / 962,879,488 bytes |
| Retirement | Both sandboxes/cgroups/private disks removed; final Node/IAM/CID checks performed separately |

The independent audit verified the agent's source hash against the pinned
upstream file, every model/tool history pair and tool-call identity, all four
EROFS/DAX layers, and all five final conformance/control/model/verification
VMM scopes. Outer cgroup OOM/oom_kill counters were zero and guest logs contained
no OOM report. The baseline/oracle controls scored 0/1. Earlier failed or
interrupted attempts remain separate, not averaged into this result.
This is a functioning agent-to-sandbox-to-official-score integration. It does
not establish a positive model solve, a SWE-Gym success rate, or completed SAO RL.

Final teardown proved physical Node, IAM and CID leases zero, no private VM disks,
and unchanged IDs/PIDs/start times/networks for all five original host containers,
including Docker-hosted Grafana and Prometheus. The isolated API/Node/Edge stack
and temporary etcd stopped normally. The external model supervisor also stopped
successfully (`MainPID=0`, `ExecMainStatus=0`); GPU use returned to 35 MiB.
The credential-filtered evidence archive contains 467 files, 910,051 bytes, with
SHA-256 `a97a5083ac3f8091e2e73713b954395f224397cb1257648193ad58dddc5b4f29`.
All 466 manifest entries were independently verified after retrieval. Private
API credentials, route keys, guest channel keys and TLS private keys are excluded.

Completion evidence records OpenHands wrapper inputs and normalized LiteLLM
ModelResponse objects; it is not a raw provider-wire or native-token training
capture. No sampled-token/logprob reconstruction or SAO update is claimed.

### Trajectory attribution review (2026-10-27)

The final scored run failed to produce a fix, rather than producing a fix that
the runtime lost. Calls 6 and 11 read the relevant `warn_return_any` condition
and the existing `is_lambda` variable; call 15 used the real upstream editor to
view lines 4257–4280. Calls 16–24 searched for the nonexistent
`current_function`, with the last six commands identical. There were 23 shell
calls and one editor **view**, no source edit, reproduction/test command, or
finish call. Candidate capture therefore correctly returned an empty patch.
After-the-fact inspection of the withheld gold patch shows one added predicate,
`and not is_lambda`; this patch was not part of the model's public task input.
The fresh official test failed with the original unexpected return-Any warning;
the independent baseline/oracle controls scored 0/1 on that same test.

All 24 saved requests retain the identical complete public issue and all prior
assistant/tool pairs. Every one of the 23 observations with a subsequent request
appears there verbatim, including shell exit codes. The final prompt used 7,817
tokens of the configured 65,536-token context. There were no length-finished
responses, runtime timeouts, or observed OOM events. These checks exclude those
specific explanations for this run; they are not proof of complete adapter
equivalence to upstream Docker.

The review also found an unresolved **Thinking-history compatibility gap**.
The installed LiteLLM 1.52.5 response converter reconstructs messages without
`reasoning_content`. Independently, this pinned OpenHands CodeActAgent rebuilds
assistant history from `content` and `tool_calls` alone. A read-only synthetic
marker test in the actual installed environment proved both losses, including
the second loss when the field was explicitly injected after LiteLLM conversion.
The actual local Qwen template (SHA-256
`273d8e0e683b885071fb17e08d71e5f2a5ddfb5309756181681de4f5a1822d80`)
renders supplied reasoning for assistant messages in the current user's tool
chain, and renders empty thinking blocks when it is omitted. This concerns
successive tool steps of the **same** user request, distinct from removing
thinking from completed earlier user turns. See the
[official Qwen chat template](https://huggingface.co/Qwen/Qwen3.5-2B/blob/main/chat_template.jinja).

The run enabled Thinking and its usage records reported reasoning tokens, but
all normalized assistant contents are empty/whitespace and no reasoning field
reaches later requests. Original provider response bodies were not saved, so
the discarded reasoning text cannot be recovered or used to explain the
model's intent. The synthetic test proves a lossy compatibility boundary; it
does **not** prove that preserving reasoning would make this task pass.

Attribution remains mixed: repeated unsuccessful searches are an observed
policy failure, while the model/agent history path still has a demonstrated
compatibility gap. Do not label this a clean Qwen capability comparison yet.
The next discriminating experiment should preserve raw provider evidence and
the template-appropriate reasoning history, verify the whole round trip with
a marker regression, then rerun the same public task and sampling configuration.
Only after that should stronger-model or upstream-Docker controls be used to
separate model ability from residual runtime differences. This review started
no model service, sandbox, or training job; the original frozen evidence archive
and its manifest are unchanged.

### Thinking-history repair and same-configuration rerun (2026-10-27)

The optional application now captures provider request/response bodies without
HTTP headers, restores the separate reasoning field after the old LiteLLM
converter, and restores current-user tool-chain reasoning by exact tool-call
identity before the next completion. The upstream CodeActAgent source hash,
task/system prompts, tool schemas/parser and stuck detector remain unchanged.
Old completed user turns are excluded according to the Qwen template. An
installed-stack mock-HTTP regression exercises both formerly lossy boundaries
and verifies the field in the actual outgoing request, with API keys excluded
from evidence. Six model-boundary/history tests passed on Linux; the existing
34 application tests also passed.

The first rerun with reasoning restored reached 58 completions and attempted
editing/tests, but exposed another execution defect. A heredoc followed by a
second top-level command produced multiple interactive PS1 frames. The companion
accepted the first as completion of the whole script; later calls consumed stale
output. Patch capture consequently received an earlier grep result, and native
read correctly reported that `/work/solution.patch` did not exist. Its report
remains **error/unscored**, with the actual sandbox stopped; no candidate or
score is reconstructed after retirement. Its later repeated searches are not
a clean model-failure comparison because feedback had shifted.

Two deterministic regression cases failed on the old implementation: a
multiline script returned the first line's exit code, and heredoc-plus-cat
returned empty output while shifting the cat output into the next action.
Fresh bash actions now submit `eval -- <shell-quoted complete script>` inside
the same persistent shell. This preserves cwd/env/aliases while producing one
completion and the final exit code. Foreground stdin remains raw. Portable
adapter checks passed 16 with one real-API test skipped; the separate Linux
run passed nine guest tests and the real-API method with **19 capability
assertions**, including multiline/heredoc/output identity and retirement.

After that fix, a fresh model episode and fresh official grading completed:

| Measurement | Original scored pilot | Final repaired rerun |
| --- | --- | --- |
| Model / sampling / iteration and response limits | Qwen3.5-2B Thinking, original configuration | Identical |
| Completions | 24 | 38 |
| Provider responses with nonempty captured reasoning | Not available in original normalized records | 38 / 38 |
| Exact prior-reasoning restorations in outgoing provider requests | None | 703, checked against raw responses |
| Maximum prompt tokens | 7,817 | 17,212 |
| Response truncations | 0 | 0 |
| Terminal loop | Repeated grep for `current_function` | Four identical `sed -n '530,545p' /testbed/mypy/checker.py` calls |
| Candidate patch | Empty | Empty |
| Fresh official score | 0 | 0 |
| Agent / total elapsed seconds | 69.817 / 82.893 | 95.169 / 127.783 |

All final-rerun calls were shell inspection commands; none edited code or ran
tests. At step 15 its captured reasoning recognized that lambdas returning Any
should not trigger this warning, but it then pursued whether the lambda had an
explicit annotation. Steps 35–38 repeat both that reasoning and the same source
read despite receiving identical complete feedback. Every prior observation
was verified verbatim in later requests with its correct exit code. The final
official test failed with the original unexpected return-Any warning. This
shows repetition persists with reasoning retained; a single stochastic rerun
does not measure a solve-rate improvement or establish global model inability.

All four new VM scopes (the unscored first rerun, capability probe, final model
episode and final verifier) have retired cgroups/private writable disks. Final
physical Node/IAM/CID leases were zero, original host container identities and
networks were unchanged, the private stack/model stopped normally, and GPU use
returned to 35 MiB. The separate credential-filtered archive contains 442 files,
2,387,112 bytes, SHA-256
`f276ece8614cddeaac4448dae26257674f22a8310650e48dc8992e95e7dfa11f`.
All 441 manifest entries were verified locally after retrieval. The earlier
archive is unchanged. Native sampled token IDs/logprobs are still not captured
by this OpenHands evaluation runner; these records are not accepted as SAO TITO.

### DeepSeek Flash same-stack comparison (2026-10-27)

At the user's request, the next run used `openai/deepseek-flash` with the official
API, Thinking enabled and reasoning effort high. No Pro generation was run.
The API's returned model identifier was `deepseek-flash`. The first request's
complete system/task messages and tool schemas were checked equal to those of
the final Qwen rerun. The exact upstream agent source hash, published environment,
DSec runtime (including the multiline completion fix), official verifier and
100-iteration/1,200-second/32,768-output-token limits remained unchanged.

This is not identical sampling across model families: the
[DeepSeek API](https://api-docs.deepseek.com/guides/thinking_mode/) ignores
temperature in Thinking mode, and Qwen-only parameters were not forwarded.
DeepSeek tools require reasoning from all prior assistant replies, including
plain replies and earlier user turns. The model-boundary bridge now supports
that separate contract, validates response order/content, and converts the old
agent's assistant text-part arrays to the API's string form without changing
text or tool arguments. Both Qwen and DeepSeek installed-stack mock-HTTP
regressions passed (eight tests); local application checks passed 32 with seven
optional-dependency skips. Credentials stayed in the remote controller process,
not in task instructions, guest environment, argv, request-body evidence or
the credential-filtered result archive.

| Measurement | Qwen3.5-2B Thinking, final rerun | DeepSeek Flash Thinking |
| --- | --- | --- |
| Model replies | 38 | 18 |
| Agent iterations | 38 | 23 |
| Tools | 38 shell inspections | 19 shell calls, one editor change, one finish |
| Maximum consecutive identical tools | 4 | 1 |
| Candidate | Empty | 550-byte diff: add `and not is_lambda` in `mypy/checker.py` |
| Native controller termination | `agent_stuck` | `finished` |
| Fresh official score | 0 | 1 (`testReturnAnyLambda` passed) |
| Agent / total elapsed seconds | 95.169 / 127.783 | 74.906 / 95.567 |

All 153 prior-assistant reasoning restorations in the Flash run matched the
original provider responses. Every prior tool output was verified verbatim with
its correct exit code in subsequent provider requests. The maximum prompt used
12,286 tokens; no response was truncated. Aggregate API usage was 149,095 input
and 3,398 output tokens (input includes repeated/cached history); these counts
are not billing-cost estimates or native RL training tensors.

**Data-quality limitation: the task repository exposes future repair commits.**
At replies 6–8, Flash searched all Git history, then read `d61383eef` and
`9e7bc383e`, whose patches contain the exact repair for this task. It applied
that repair at reply 10, ran reproduction/regression commands, and finished.
The actual candidate only changed the one source predicate; existing tests
were unchanged. We did not provide a gold patch in the public instruction, but
it was accessible through `.git` inside the published task environment. This
is an answer leak in the current case preparation, not a clean independent
solve or evidence of a benchmark success rate. Keep the scored result as an
execution/scoring positive control with this limitation explicitly attached.

The comparison establishes that this OpenHands/DSec execution and official
scoring path can complete successfully, and that Flash's agent behavior in this
run differs from Qwen's repeated inspection. It does not isolate intrinsic model
reasoning ability from retrieval of the future repair, nor prove all framework
paths correct. Before using this case for clean capability evaluation or RL,
the application preparation should publish a baseline-only Git repository with
future refs/objects removed, while retaining the original source tree and
recording its identity. Both models must receive the same cleaned environment
for that additional comparison; silently changing only Flash's input would not
be a valid control. That additional experiment was pending at this point; its
results follow below.

Both Flash VM scopes/cgroups/private writable disks were retired. Physical
Node/IAM/CID leases returned to zero; original Docker-hosted container identities,
PIDs/start times and networks were unchanged. The temporary stack stopped normally;
no local GPU model was started and GPU use remained 35 MiB. The separately
credential-filtered archive contains 124 files, 317,951 bytes, SHA-256
`7ade93b1b518506dbe70352e7802f16ae561549fd3fc7fbedcc56cb19a8b797a`.
All 123 manifest entries were independently checked after local retrieval.

### Baseline-only Git, Qwen vs Flash rerun (2026-10-27)

**Superseded evaluation condition:** this preparation also removed ancestor
history, exceeding the requested future-object cleanup. It changed `git show`
and `git blame` semantics and contributed to Qwen's history-search loop. Preserve
the measurements as a single-commit shallow-repository experiment, not a clean
attribution of model capability under the intended history-preserving condition.
The corrected publication keeps every baseline ancestor; its rerun is below.

Both models were rerun on `python__mypy-15413` after rebuilding the actual
guest-visible `/testbed` EROFS layer. A fresh Git database contains only the
original baseline commit `e7b917ec7532206b996542570f4b68a33c3ff771` and its
trees/blobs: one shallow commit, 1,497 objects, one branch, no remotes, tags,
reflogs or alternates. This also omits ancestor history, rather than preserving
the original repository's full pre-baseline history. Explicit-object packing
disables delta reuse, so future objects cannot survive as hidden pack bases.
The known future repair objects are absent, including from the complete
packed/loose object inventory. The object-set SHA-256 is
`13b85fcd891b10761b121d5646af0f7a54885257de9ceb78e232fa76350f091a`.

All 1,426 tracked files match the original baseline blobs and executable modes.
The non-Git tar inventory, contents, numeric ownership and permissions match
the original stopped image. Its lean boot image contains no old `/testbed`
files. The old task layer is replaced, not hidden beneath an OverlayFS whiteout.
Original images and earlier contaminated-run evidence remain unchanged outside
the new environment. Layer SHA-256:
`d1e984f448148dccbe3b7447228d520773a2721b8aeff86e8a25c83f69af6780`;
new template SHA-256:
`9e1618e4f9ead5f21324baa4be042ab7fbc0db5f098f918d6334eadcd471d293`.

The optional SWE-Gym application now provides `dsec-swegym-git-baseline` for
offline publication. Both the model runner and fresh verifier validate the
pinned baseline/tree/object-set inventory before task exposure or patch
application. All six scored-control/model/verifier sandboxes passed the same
inventory check. Empty-patch and oracle controls scored **0 and 1** respectively.

The official script's `git show` expands the complete tree for a shallow root,
exceeding the previous 8 MiB log transfer budget. The first control therefore
remained unscored and its failure evidence was retained. Log capture now falls
back to a complete gzip transfer (8 MiB compressed, 128 MiB expanded limit),
retaining both files for the unchanged official parser. The verifier script,
test patch and test selection are unchanged. Both controls were rerun after this
fix. Application tests passed 35, with seven optional-dependency skips; the
29 QEMU Container regressions passed. An additive catalog-history capacity
increase from 16 to 32 allowed startup with existing retired-instance evidence;
all digest/policy checks remain in force, and no administrator permission changed.

| Measurement | Qwen3.5-2B Thinking | DeepSeek Flash Thinking |
| --- | --- | --- |
| Official score | **0** | **1** |
| Model replies / agent iterations | 51 / 51 | 26 / 26 |
| Tools | 51 shell calls | 23 shell calls, one finish |
| Candidate | Empty | 546-byte diff, one added `and not is_lambda` predicate |
| Termination | `agent_stuck` | `finished` |
| Maximum consecutive identical tools | 4 | 1 |
| Maximum prompt tokens | 24,509 | 13,228 |
| Response truncations | 0 | 0 |
| Agent elapsed seconds, including sandbox setup/cleanup | 124.039 | 112.793 |
| Total elapsed seconds, including fresh verification | 180.739 | 166.988 |
| Sum of model-call wall time, seconds | 50.373 | 55.976 |

The exact initial system/task messages, tool schemas, agent source, environment,
100-iteration/1,200-second budget and 32,768-token response limit were checked
equal. Both use fresh conversations. Sampling follows the earlier separate
Qwen and DeepSeek Thinking contracts; different model families, serving
hardware and API behavior do not constitute identical sampling or an inference
throughput benchmark. All 1,275 Qwen and 325 Flash prior-assistant reasoning
restorations matched saved raw replies. All 1,275 and 316 tool-history
occurrences respectively retained their original feedback and shell exit codes.

Qwen inspected the diagnostic and lambda inference, then repeatedly searched
for unavailable history, ending with four identical `git log` calls and no edit.
Flash inspected the baseline diagnostic, lambda handling and tests. It attempted
one GitHub `curl`, but that returned only `Terminated` with exit 143 and no source
content. Its reasoning then explicitly acknowledged no network, considered
lambda return-type inference, and recalled a likely existing implementation.
It applied the source predicate and ran local tests/reproductions; a fresh
official verifier passed `testReturnAnyLambda`. No future Git repair or successful
external-source retrieval appears in the recorded tools. This removes the
observed environment-history leak; it cannot exclude memorization from model
pretraining or establish a solve rate from one task and one sample per model.
It supports a successful OpenHands/DSec execution/scoring path on this case and
shows Qwen's repeated inspection persists with complete feedback and reasoning.
These records still are not native token-ID/logprob SAO training evidence.

All seven VM scopes, including the unscored log-capture control, retired their
cgroups and private writable disks. Each mounted four DAX layers and had no
outer-cgroup or guest-console OOM evidence. Physical Node/IAM/CID leases returned
to zero. The temporary stack/model stopped normally, GPU use returned to 35 MiB,
and original host container IDs, PIDs, start times and networks were unchanged.
The credential-filtered archive contains 423 files, 24,745,721 bytes, SHA-256
`beb6f9733ed45271579b6d255267b03084796b566c4954e1ceeb1c5d0fcfdbd5`.
All 422 manifest entries were verified after local retrieval, and the request,
reasoning and feedback comparison audit passed independently on the local copy.

### Corrected cleanup preserving baseline ancestry (2026-10-27)

**Subsequent termination audit:** Git-history preparation is corrected, but
replay later identified a defect in the pinned upstream StuckDetector that
prematurely stopped Qwen at reply 37. The table below records the actual patch
score at that stop, not an uncontaminated full-budget capability comparison.
See the diagnostic below; the original evidence and measurements are unchanged.

The earlier cleanup was too broad: the user asked to remove future repair
commits/objects, while the implementation also discarded legitimate ancestors.
The resulting shallow `blame/show` view contributed to the observed Qwen loop.
That run remains an explicitly superseded evaluation condition, not evidence
for attributing that history-search behavior to the model alone.

Publication schema v2 now retains the exact baseline and **all** ancestors,
including every merge parent, plus their complete tree/blob closure. It rejects
shallow/incomplete sources and grafts, disables replacement objects, rebuilds
packs with deltas restricted to the explicit allowed set, and verifies every
packed/loose object. Future/unrelated objects and extra refs remain excluded.
The corrected environment has 11,242 commits and 86,255 objects, no shallow
boundary; object-set SHA-256
`70d26ac822e02c3f697f1c96eae4d5b2fbf07aa4a4b9597c25e3b99c6f7baae5`,
commit-set SHA-256
`706e39c064b046852914b2a05c834645edfef65a291b195c5264a9c089b896f2`.
Known future repair objects remain unreadable. All non-Git file data/metadata
and the 1,426 tracked baseline files still match the original image.

Full baseline `git log --format=raw`, `git show --full-index` and
`git blame --line-porcelain` on `mypy/checkexpr.py` were compared byte-for-byte
with the original complete repository and matched. Source/base IDs are unchanged.
New EROFS layer SHA-256:
`4bb474c5bfdeee0ce704739096dbaa2be27e69f8490991fb1cb7c63cc6c3c91c`;
template SHA-256:
`1808ce062043fc6bc0507c124ea33f65771d891d7dab61dea235badf5ac39321`.
All six fresh scored-control/model/verifier sandboxes checked the same complete
object/ancestor inventory before task exposure or candidate application.
Empty-patch/oracle scores were again 0/1. Application regression checks passed
36, with seven optional-dependency skips, including a merge-history preservation
test and negative tests for future packed/loose/tagged/unreachable objects.

Only `repository.py` differs between the preceding and corrected application
wheels. Model sampling, time/step/output limits, runtime and verifier settings
were compared equal; the upstream agent source hash is unchanged. Initial
system/task messages and tools were again equal between models.

| Measurement | Qwen3.5-2B Thinking | DeepSeek Flash Thinking |
| --- | --- | --- |
| Official score | **0** | **1** |
| Model replies / agent iterations | 37 / 37 | 16 / 22 |
| Tools | 27 shell calls, 10 editor views | 16 shell calls, two editor changes, one finish |
| Candidate | Empty | One source guard plus one added test case; 1,106-byte diff |
| Termination | `agent_stuck` | `finished` |
| Maximum prompt tokens | 15,309 | 9,524 |
| Response truncations | 0 | 0 |
| Agent elapsed seconds, including setup/cleanup | 91.371 | 64.377 |
| Total seconds, including fresh verification | 122.590 | 85.043 |

Qwen's failure path changed: it inspected the diagnostic and function/lambda
scope, but did not edit. Near the end, recursive search returned
`mypy/checkexpr.py:4436` for `visit_lambda_expr`; it returned to searching
`mypy/checker.py`, received exit 1/no matches, and triggered the upstream loop
detector. This was not the earlier attempt to retrieve nonexistent ancestors.
Flash added `and not is_lambda` in the diagnostic guard and a new
`testNoReturnAnyInLambda` case in the existing warnings file. Existing cases were
not rewritten. The added case lacks an explicit Callable context and therefore
is not itself strong evidence that the original regression was exercised.
The original issue reproduction passed, targeted local groups passed 3, 20 and
69 tests, and the independent official `testReturnAnyLambda` passed. Saved tools
contain no future repair lookup or external-source retrieval in this run.

All 666 Qwen and 120 Flash retained-reasoning occurrences matched original raw
replies under their respective contracts; 666/178 tool-history occurrences
matched actual observations and shell exit codes. This is one stochastic
sample per model, not a solve-rate estimate or proof against pretraining
memorization. Corrected results do not excuse the preceding preparation error
or establish that history restoration alone caused a model-behavior change.

All six VM cgroups/private disks were retired, four DAX layers were observed per
VM, no outer/guest OOM was found, and Node/IAM/CID leases returned to zero. The
temporary stack and model stopped normally, GPU use returned to 35 MiB, and
original host container identities/PIDs/start times/networks were unchanged.
The credential-filtered archive has 324 files, 828,359 bytes, SHA-256
`7b5180ebf884c7690fbca79f90044f967995713219df18dfb76aa99ec4a6348d`.
All 323 manifest entries and the independent comparison audit passed locally
after retrieval. Earlier images and experiment evidence remain preserved.

#### Qwen source-search and premature-stop diagnosis

The model found the warning guard at replies 3–4, but at reply 5 assumed that
`is_lambda` detection was broken without executing a reproduction or observing
its value. After seeing the local boolean definition, it pursued an unverified
theory that lambda scopes were not pushed. Reading `push_function`'s append/pop
implementation did not establish that callers omitted lambdas, yet at reply 24
it treated this as the cause. The actual warning condition did not use the
already-computed `is_lambda` flag. All ten editor calls were views, not edits;
there was no test execution or candidate change.

At reply 35, recursive search returned `checkexpr.py:4436` as the implementation
of `visit_lambda_expr`. The next two actual provider requests retained this
exact observation and exit code, but the model searched `checker.py` again.
This supports an observed failure to use a returned file location, not missing
tool feedback. The maximum prompt was 15,309 tokens and no reply was truncated.

There is also a concrete harness defect. The last four chronological shell
actions/observations are A/B/A/A, where A searches `checker.py` and gets exit 1,
and B searches the repository and gets exit 0 with the correct implementation.
The pinned SWE-Gym OpenHands fork's
`_is_stuck_repeating_action_observation` compares reverse positions 0/1/3 in its
four-event stopping condition, but omits position 2 (B). Consequently it
incorrectly reports `Action, Observation loop detected` for this sequence.

The installed detector source SHA-256 is
`0c0014ccfaee33cc0b271e3e158ccca56b750750cd1d2d41e202fbc2cceb6c40`.
Deserializing the saved real history (SHA-256
`85c7fd85e5efc28ea90fc42362d8aa9af5a6c394e936927a1a2b766279199fb4`)
and replaying it through the installed class returned true. A candidate method
that additionally checks position 2 returned false on that same history, while
both methods returned true on a four-identical-call positive control. This was
a deterministic, memory-only experiment: installed code, model and recorded
episodes were not modified or rerun. Diagnostics were retrieved separately
from the immutable experiment archive.

The empty patch's official score 0 remains its actual test result, but the
37-reply cutoff cannot be attributed solely to policy failure or described as
four consecutive identical calls. A corrected detector needs acceptance and a
fresh matched evaluation before claiming an uncontaminated full-budget model
comparison. The replay does not establish that Qwen would solve the task if
allowed to continue.


### 2026-10-27: OpenHands SDK v1.58.2 integration

The current optional adapter pins `openhands-sdk==1.58.2` and
`openhands-tools==1.58.2`. The default `dsec-swegym-openhands` entry uses the
upstream `Agent`, asynchronous `Conversation.arun()`, Terminal and FileEditor
schemas/executors. The old SWE-Gym fork remains a separate `[legacy]` install
and `dsec-swegym-openhands-legacy` command. They must use separate environments.
The core package still has no OpenHands, model, benchmark or trainer dependency.

Actual task tools execute through the public HTTPS API and mTLS Node/Edge in
the existing confined QEMU Container environment. The controller keeps model
credentials and an empty private HOME; its workspace holds metadata only and
rejects host command/file/Git execution. Task files remain in the sandbox.
Only the tool EROFS layer and its guest template were replaced. The task's
corrected baseline-and-all-ancestors layer remains SHA-256
`4bb474c5bfdeee0ce704739096dbaa2be27e69f8490991fb1cb7c63cc6c3c91c`.
The SDK tool layer is `23601713f395fb198c975d79058c3f1b13e80aa98347a50654af9e86e4002d6c`.
Each fresh sandbox checks the complete Git inventory before model execution
or official grading. Gold and test patches are never put in model context.

Installed Agent, StuckDetector, TerminalExecutor and SubprocessTerminal sources
were hashed against the v1.58.2 release archive and matched byte-for-byte. The
new detector SHA-256 is
`3bf25f2d722404398e4744ae188f575f628463732b3606a8da6049fb8ff0039e`.
A regression with real SDK action/observation event types rejects A/B/A/A as a
four-identical-action streak and retains the all-identical positive control.
No upstream installed source was patched.

Integration issues found and resolved:

- Official Terminal rejects bare newline-separated commands. Its native error
  remains visible; valid multi-line scripts are executed as one explicit shell
  command. The adapter does not repair/rewrite model commands silently.
- Harness identity/JSON checks and candidate capture use the non-PTY DSec API
  in that same sandbox. They do not parse terminal control sequences as JSON.
- Interrupt requires a second native session as well as a separate guest HTTP
  route, avoiding both the per-session SDK lock and the guest action lock. The
  subprocess backend's shell-process-group signal did not promptly stop its
  foreground job. The adapter uses the official PTY `C-c` key interface instead.
  Early interruption is tested with a guest admission barrier, nonzero exit
  status and bounded completion; accepting an interrupt HTTP response alone
  was an insufficient test and is explicitly superseded.
- A tool worker must not call controller `pause()` while an upstream agent step
  can hold its state lock and await the worker. Genuine transport failure now
  fences workers and aborts once at the event callback boundary. A controlled
  test using the actual upstream asynchronous loop proves that no second model
  call or tool replay happens and that error callbacks can still persist.
- Unsupported timeout values and mutually exclusive reset/input arguments are
  rejected before execution with recoverable tool feedback. They cannot reset
  the shell or become an infrastructure failure. Genuine uncertain effects
  remain unscored. The failed locked Flash attempt was retired using the normal
  API before its exact controller processes were stopped; recovery is recorded
  separately rather than fabricating a score or rewriting its original trace.
- `context_length` is a joint window, not input space plus another full output
  reservation. The profile reserves 32,768 output and 1,024 safety tokens within
  65,536, leaving 31,744 input tokens. It uses the official summarizing condenser;
  Qwen loads the existing model's tokenizer without loading weights. Original
  events remain on disk. Qwen's preceding context-overflow attempt is invalid,
  not a zero-reward policy episode.
- SDK `arun()` consumes cancellation after transitioning to PAUSED. Deadline
  recording checks `asyncio.timeout.expired()`; provider timeouts are not
  relabeled as episode limits. Recognized step/deadline limits still capture
  and grade actual candidates. CLI stop signals are forwarded to the controller
  and its cleanup is joined.
- Resolve the SDK and official harness together using `sdk-constraints.txt`.
  Separate unconstrained installs had selected conflicting Click/Hub/datasets
  versions. The accepted Linux environment passed `pip check` and retained the
  original official SWE-Gym script/parser/scorer source hashes.

Acceptance results and evidence are recorded below. These are single-task
integration checks, not a solve-rate estimate, a performance comparison against
Docker, or a completed SAO/RL training run.


#### Accepted outcomes and recovery evidence

| Final profile on `python__mypy-15413` | Model replies | Tool calls | Official score | Completion |
| --- | ---: | ---: | ---: | --- |
| DeepSeek Flash Thinking | 10 | 12 | 1 | Finished; agent + fresh verification 64.82 s |
| Qwen3.5-2B Thinking | 100 | 97 | 0 | Iteration limit; agent/preparation 281.63 s; verifier-only recovery below |

Model reply counts include any condenser calls; the SDK's iteration budget is
not a count of shell commands. Both profiles retain the 32,768 response ceiling,
65,536 total context window, 100-iteration/1,200-second budget, full Git baseline
and task/resource configuration. Qwen uses the official local tokenizer. This
single case does not establish a general model capability or solve-rate ranking.

The final tool acceptance passed **20 checks**, including persistent cwd/env,
script output/exit-code alignment, soft timeout/poll, actual foreground stdin,
editor undo, bounded early interrupt, recoverable timeout/reset-input rejection,
state preservation and retirement. Its interrupt completed in about 0.13 s in
the preceding full acceptance. The independent official `arun()` cancellation
acceptance completed in about 0.16 s after a guest-side admission barrier,
confirmed no native requests remained pending, and retired its sandbox.

The temporary acceptance stack's one-hour lifetime expired while the final
Qwen verifier was returning its log. The original model/candidate/report were
preserved; that first verification has no score. Restoring the **same etcd and
Node state**, rather than resetting quota records, exposed a QEMU adoption
edge case: `pidfd_open` returning ESRCH for a previously recorded, already-dead
VMM aborted Edge startup. The fix handles only `ProcessLookupError` as proof of
absence and also checks an attached pidfd's exit state before live attestation.
It does not accept an identity mismatch, signal a replacement PID, relaunch the
old VMM or replay a command. Local QEMU/Container tests (57, two Linux-only
skips), Linux installed QEMU tests (28), eight host-isolation guard mutants and
four command-recovery guard mutants passed. The deployed recovery wheel was
built from the actually installed distribution and changed only
`dsec/runtime/backends/qemu.py` plus its wheel RECORD; the other package files
were preserved. The resulting module SHA-256 is
`309919b64c4bba595e029668a0e90e5ba5264c44217e5515e55e9bba41934619`.
These are bounded regression/guard checks, not a whole-program formal proof.

With this fix, Edge retired the old verifier's physical resources, Node
reconciled the prior IAM admission, and a fresh official verifier scored the
**exact saved candidate** 0. No model response was resampled. The separate
`recovered-verification.json` binds that score to the original report and patch
SHA-256; the original interrupted report stays unchanged. Node/CID/IAM leases
and private writable VM disks returned to zero. The temporary stack and model
stopped; GPU use returned to 35 MiB. Original Docker/Grafana-related container
IDs, PIDs, start times and network settings matched their pre-run baseline.

The acceptance evidence archive contains 1,293 files, 9,210,296 bytes, SHA-256
`d1df06b30a1854cc24f20d53e63d8ecd0909feaa32bf243ff25ff21b53126dc4`.
Export checked for the actual model/API credentials before copying; archive
hash and all 1,292 manifest entries were independently verified locally. It
includes valid and invalid attempts, recovery proofs, raw SDK completion/event
records, candidate patches, official logs and installed dependency/source
identities. The optional SDK integration, case and package-source regressions
passed, and both installed CLI entry points were checked outside the source
checkout. Installation, dependency constraints and CI were updated together.


### 2026-10-27: Preserve evidence in modern SDK context summaries

The preceding Qwen run reached the iteration limit with only two scratch files,
without modifying the checker. It reproduced the warning and read the relevant
condition, but continued source/history searches. Its three generated context
summaries also invented source edits and branch/commit state. This prevents
attributing that single result solely to model capability.

The optional adapter now extends the pinned summarizing condenser at its input
serialization boundary. Native event JSON preserves actual action/editor
arguments, structured reasoning, complete available observations, tool-call IDs
and error/exit states. Upstream display strings had omitted command parameters
and reasoning, and truncated observations after 500 characters. Both synchronous
and asynchronous summary generation use complete inputs; oversized records are
fragmented with SHA-256 and character offsets, summarized in bounded batches and
merged. The hard-reset display limit cannot silently truncate these records.
The release's default trigger thresholds, event boundaries and summary placement
remain unchanged. All original events and summary requests remain evidence.

Summary instructions distinguish hypotheses, reading historical diffs and
actually applying edits, and require evidence for changes/tests. This repairs
the input information loss; it does not guarantee that a model-generated summary
is factually perfect. The full issue remains in the retained context. No model
weights, sampling settings, task instructions, guest image, tool schemas or
official verifier are changed.

Local SDK/case/summary regressions passed 24 checks; the eight targeted summary
checks also passed against the installed Linux SDK, including a controlled real
upstream asynchronous agent loop that triggers a summary, receives it in the
next model request, and retains the current tool feedback. Replaying the preceding
trace round-tripped all 194 action/observation/error records, including 97
reasoning records and 95 observations, through full structured serialization.
The controlled follow-up uses Qwen3.5-2B Thinking with the same 32,768 response
ceiling, 65,536 context window, 100 iterations and 1,200-second episode budget.

Offline replay with the original Qwen tokenizer measured the three preceding
full summary inputs at 24,579, 22,768 and 32,705 tokens. The third exceeds the
31,744 input budget and is delivered in two complete batches (23,268 and 10,145
tokens) before merging, rather than cutting output or reducing the response
ceiling. This replay makes no model requests.

The first structured-input rerun (`summary-fix/qwen-r5`) was scored 0 after
97 tool calls and the 100-iteration limit (274.91 s agent phase, 290.94 s total).
It produced scratch tests without modifying the checker. Inspection revealed a
second input omission: the summarizer receives the forgotten range only, while
the original issue stays in the retained initial window. The model's summaries
reversed the task objective and eventually claimed that no fix was needed. The
original issue was still present in the main agent context; it was absent from
the summarizer's own input. This diagnostic run remains separate evidence.

`EvidenceSummarizingCondenser.v2` therefore includes the exact unchanged public
task instruction as immutable context in every chunk and merge request, including
its token cost. It neither reveals the official hidden tests nor supplies a fix.
This is a single-task evaluation profile; the generic SDK factory still accepts
a caller-provided condenser. Nine targeted checks passed locally and on Linux,
including preservation of this task context across over-budget batching/merging;
the seven application SDK checks also passed after wiring the instruction.
With this context, replay inputs are 25,028, 23,217 and 33,154 tokens; the final
one is split into 23,717 and 10,594 token batches, all below the same input
budget. The second rerun retains all original model/task/resource parameters.

Final anchored-input rerun (`summary-fix-v2/qwen-r6`): official score **0**,
47 model requests (46 tool calls and one summary), termination `agent_stuck`,
72.48 s agent/preparation and 94.10 s including fresh verification. The candidate
patch is empty and the official `testReturnAnyLambda` still fails. Shorter
runtime reflects early termination, not improved solving performance.

Real-request audit verified the exact retained instruction and all 44 native
records in the summary input (17,422 prompt tokens), with zero response-token
cutoffs. The summary correctly describes false warnings on lambdas; the preceding
task-direction reversal is absent in this run. Source searches still failed to
converge, including repeated searches for nonexistent `AllowReturnAny` symbols.
The model endpoint also returned unsupported `command`, `find` and `code` tool
names despite explicit error feedback. Available tools remained `terminal`,
`file_editor`, `finish`, `think`. No command aliases or host fallback were added.

The installed upstream v1.58.2 stop-policy behavior was reproduced from the
last four real events: unsupported `code` calls have four different raw argument
strings but all have `action=None`. `_event_eq` compares source/thought/action/
tool name without their raw tool arguments; the repeated tool-not-found errors
therefore trigger stopping. Installed detector source remains unpatched, SHA-256
`3bf25f2d722404398e4744ae188f575f628463732b3606a8da6049fb8ff0039e`.
The observation is retained in `stuck-detector-audit.json`.

Interpretation corrected after review: differing arguments do NOT by themselves
prove an erroneous stop. An unavailable tool remains unavailable regardless of
the command arguments, so terminating repeated calls to that tool is a reasonable
anti-loop policy. The source comments/nudge describe identical arguments while
the implementation groups these unsupported calls, but this wording mismatch
does not establish a functional defect or justify changing the policy. The
previous claim of a confirmed upstream bug/false positive is withdrawn. This
run is a scored policy episode ending under the unchanged upstream policy, not
proof of a sandbox or detector failure. Its empty candidate genuinely scores 0;
one stochastic episode still cannot establish a general model capability.
The earlier legacy A/B/A/A omission is a separate case.

Agent and verifier were retired; Node/CID/IAM usage and writable VM disks
returned to zero. Temporary API/Edge/model services stopped, GPU use returned
to 35 MiB, and all original monitored Docker container IDs/PIDs/start times and
network settings matched the before-run baseline. Four stopped etcd containers
created solely by these attempts were removed after evidence export.

Final evidence archive: 178 files, 803,684 bytes, SHA-256
`b0bc54467d1ed596ef49050dbfef735fed556bbc112e7943e6c2120ca75916f6`;
all 177 manifest entries were verified locally. Diagnostic structured-input v1
archive: 337 files, 2,311,497 bytes, SHA-256
`bc92d185f1006aa422e798b44a2041ef545de8230ae18db90b9c372f46f1c5d5`;
all 336 manifest entries were verified locally. Neither export contains the
actual temporary control credentials. Prior model traces remain unchanged.


#### Raw model-output versus tool-parser investigation

The preceding unsupported `code` calls are observed API tool-call names, not
proof of their pre-parser origin. The historical service had `log_requests=False`
and no output/debug dumps; original completion records have no raw generation
IDs or token logprobs. Its four pre-parser outputs cannot be recovered.

A bounded diagnostic replay submitted each of those four recorded contexts with
the same model, tools and sampling settings, adding only `return_token_ids=True`
and `return_meta_info=True` to the non-streaming request. No sandbox commands
were executed. Each response contains both parsed fields and `response_token_ids`
from that SAME generation; the installed serving code takes those IDs directly
from `ret_item["output_ids"]`, not by reconstructing parsed tool-call JSON.
Decoded raw text retains special tokens and the Thinking boundary. These are
NEW stochastic generations and cannot reconstruct the old samples.

| Replay of prior `code` context | Raw final function | Parsed API function |
| --- | --- | --- |
| 1 | terminal | terminal |
| 2 | file_editor | file_editor |
| 3 | file_editor | file_editor |
| 4 | terminal | terminal |

`code` was not reproduced in the four bounded replays. The actual installed
SGLang 0.5.20 parser also reproduced these four parses offline. Source review
shows Qwen3CoderDetector extracts the name from `<function=...>` and the serving
layer returns that name unchanged. Explicitly synthetic controls preserve both
`terminal` and unsupported `code`; they are not model generations. Thus no
terminal-to-code renaming has been demonstrated, but the missing historical raw
outputs prevent a definitive model-versus-parser attribution for the old four
calls, including any unusual Thinking-boundary behavior. Do not report the new
replays or synthetic controls as recovery/proof of the old model output.

Raw token IDs, decoded texts, paired responses, requests, source snapshots/hashes
and the historical capture audit are retained. The model stopped, and no VM or
task tool was started. Evidence archive: 22 files, 236,561 bytes, SHA-256
`e2f944b378c26a54be68b2e8aee121a31bfd58da9f1de8f0a0c6de9bf9c48f3c`;
all 21 manifest entries verified locally. The investigation made no installed
SDK/parser source changes and does not change the upstream stopping policy.
Repeated unsupported-tool termination has not been established as a functional
bug; see the corrected interpretation above.

#### Full episode with native pre-parser evidence

The requested full same-configuration rerun is complete (`full-raw-r1/qwen-r7`).
The task, Qwen3.5-2B, Thinking/history policy, installed SDK/condenser, guest,
repository baseline, 32,768 response budget and 100-iteration policy were retained.
Only the fresh private API path and SGLang observability flags changed. Native
level-3 JSON file logging preserves pre-reasoning/pre-tool-parser text, original
generation token IDs and resolved sampling; level 2 would truncate fields at
2,048. A separate short capability check first verified matching raw IDs from
one response and is excluded from benchmark counts.

All **102** episode model responses are paired with native finished-request logs
by response ID: zero missing raw records. Original token IDs were independently
decoded, and the actual installed reasoning/tool parsers reproduced every saved
API response, including reasoning, content, tool name and arguments. This proves
online/offline consistency, not that malformed input was interpreted correctly.
No source patches or tool aliases were applied, and no stop policy changed.

The episode made 97 tool calls (88 terminal, six file_editor, two think and one
unsupported thought) plus five summary requests. It reached the original
iteration limit, produced an empty candidate patch and scored **0** in a fresh
official verifier: `testReturnAnyLambda` failed. Agent/preparation took 307.62 s;
total including verification took 321.46 s. No response hit its token ceiling.
**`code` was not reproduced.** The old four pre-parser samples remain unavailable,
so this rerun cannot establish their model-versus-parser origin.

The last response does expose a real malformed-framing/parsing interaction:

```text
...reasoning...
</think>
<tool_call>
<function=thought>
...more reasoning; neither function nor tool_call is closed...
</think>
<tool_call>
<function=terminal>
<parameter=command>
sed -n '4195,4240p' /testbed/mypy/checker.py
</parameter>
</function>
</tool_call>
```

These are actual model-generated tags, confirmed in original token IDs. The
first `</think>` occurs at character 4,303 and the second at 8,487; function
starts are `thought` at 4,325 and `terminal` at 8,509. The reasoning parser splits
at the FIRST end marker. The tool parser's non-streaming regex matches across
the unclosed earlier block, taking `thought` as its name and the LATER terminal
block's `command` as its arguments. The API consequently returns
`thought({"command":"sed -n '4195,4240p' /testbed/mypy/checker.py"})`.
OpenHands correctly rejects that unavailable name. Its later episode termination
is the iteration limit, not repeated-tool stopping.

An offline diagnostic parsing only the unchanged final complete tool block
returns `terminal` with those same arguments. This is a suffix diagnostic, not
a new model generation, an executed command or an applied repair. The complete
raw response remains unchanged. `boundary-audit.json` records the exact spans,
installed regex reproduction and diagnostic. Thus the new evidence demonstrates
malformed model framing AND permissive cross-block parser matching; merely
observing a name in the raw text is insufficient to attribute an entire parsed
call to that block. It does not prove that the old `code` samples had this cause.

This capture also corrects an earlier configuration assumption: configured
temperature/top_p are 0.6/0.95, but all 102 actual server requests resolve to
**1.0/1.0**. The SDK model alias has `supports_sampling_params=None` and
`supports_reasoning_effort=True`; its upstream chat option selection therefore
omits temperature/top_p. Extra-body top_k=20, min_p=0, presence_penalty=0 and
repetition_penalty=1 still reach SGLang. All 47 previous `qwen-r6` SDK completion
records also omit temperature/top_p; that server's resolved values were not
logged. Earlier references to those configured sampling values must not be read
as proof they took effect. The requested rerun retained this existing path;
correcting the explicit endpoint capability declaration belongs in a separate
follow-up, rather than silently altering this comparison.

Node/CID/IAM usage and writable VM disks returned to zero. Private API/Edge/model
services stopped, GPU use returned to 35 MiB, and original Docker container
IDs/PIDs/start times/networks matched their baseline. The one stopped etcd
container belonging to this run was removed after evidence export. No host
service or privileged configuration was changed.

Evidence archive: 869 files, 9,183,294 bytes, SHA-256
`a35eeeea190142dfd03a9e718d293086db0c4d1c92b960b092395ae64f553e45`;
all 868 manifest entries independently verified locally after excluding actual
temporary control credentials. It includes native request logs, per-response
raw text/tokens/parsed JSON, full SDK events and inputs, source snapshots,
sampling audit, boundary audit, patch, official logs and cleanup evidence.

#### MiniCPM5-1B same-task model trial

Tested the official `openbmb/MiniCPM5-1B` release, revision
`87179e5c1f455ef22e6223592d2d61351b525bfc`, with the same mypy issue, prepared
repository, pinned OpenHands SDK, EvidenceSummarizingCondenser.v2, DSec API/QEMU
Container environment, resource demand, verifier and iteration/time/token limits.
The model's native tokenizer/template and SGLang `minicpm5` XML tool parser
replace the Qwen-specific ones; the common `<think>` reasoning parser is retained.
Thinking uses the model card's recommended temperature 0.9/top_p 0.95. Other
explicit sampling extras remain top_k=20, min_p=0, presence_penalty=0 and
repetition_penalty=1. This is a model trial with its recommended profile, not
a weights-only or statistically controlled performance comparison.

The optional SWE-Gym SDK application now passes configured `capability_overrides`
through to upstream `EndpointLLM`. This trial explicitly enables sampling support
and disables `reasoning_effort`, since Thinking is controlled by the native chat
template option. The installed wheel was checked against the preceding installed
application: the only changed module differs by that one constructor argument.
Both Mac and Linux passed all eight SDK case checks, including a real upstream
request-option regression proving temperature/top_p survive normalization. This
does not change upstream SDK sources or silently rewrite existing case profiles.

The fixed official weight file is 2,161,290,912 bytes, SHA-256
`7ab8fd86563125929be78aeec8cb3969c7ed2ead3be1ab9d3ec0a9fa69c8660d`.
It and all required inference files were verified after download and again on
the experiment machine. No remote model code or new inference-framework version
was needed. This trial retains the same native level-3 raw capture.

Result (`minicpm5-r1/minicpm-r1`): **official score 0**, one model response,
**zero tool calls**, normal `finished` termination and an empty candidate patch.
Agent/preparation took 24.28 s; total including verification took 37.46 s.
The response has 1,053 output tokens, `finish_reason=stop`, one `</think>` and
no function-call tags. It is not a response-token or iteration cutoff. No
summary was needed. The model discusses the warning as legitimate, proposes
comments/example invocations instead of editing the checker, and claims
verification without running commands. A fresh official verifier still fails
`testReturnAnyLambda`.

Input audit rules out omitted tool definitions or an accidentally missing issue:
all 6,540 actual model input token IDs match reconstruction using the installed
SGLang content normalization and the official native template. The exact public
instruction and `terminal`, `file_editor`, `finish`, `think` definitions are
present. Native server sampling is verified at 0.9/0.95. Original generation IDs
decode to the saved pre-parser output; the installed MiniCPM parser reproduces
the saved response and also finds no tool call. This episode therefore provides
no observed tool-parser loss and no evidence of improved task solving. Because
the model made no tool calls, it also cannot demonstrate more stable tool-call
formatting or establish general agent capability from this one sample.

Both agent and verifier sandboxes retired normally. Node/CID/IAM usage and
writable VM disks returned to zero; private API/Edge/model services stopped and
original host container identities/start times/networks matched their baseline.
The stopped etcd container created for this trial was removed after export;
weights and all experiment evidence are retained.

Evidence archive: 72 files, 300,975 bytes, SHA-256
`43f254695c692d983b037caf44616a22877b97dc53061998258476402e089d6c`;
all 71 manifest entries independently verified locally. Includes the actual
rendered model prompt, raw token IDs/text, parsed response, exact SDK input,
source snapshots, official verifier output, installation/preparation checks and
cleanup proof. Actual temporary control credentials were excluded before export.

#### MiniCPM5-2B same-task model trial (2026-10-28)

The official model card, deployment guide, pinned repository tree and available
Hugging Face discussion titles were inspected before this trial. They document
native tool parsing and recommended sampling, but the inspected materials do
not provide the agent, prompt or configuration used for the advertised SWE-bench
score. This is therefore an OpenHands + DSec model trial, not a reproduction of
that official evaluation. Official reference:
[MiniCPM5-2B model card](https://huggingface.co/openbmb/MiniCPM5-2B).

Model `openbmb/MiniCPM5-2B` is pinned to revision
`f97400052a43d642bbc6e9975e2397e3ae6a6b52`. Its 5,033,557,096-byte weight file
has SHA-256
`14fb8e7f0a18d53d1f239773758bf581cee7e456a4523a54622c3a245b64402c`;
all eight inference files were checked locally and on the experiment machine.
The model uses its native template and SGLang `minicpm5` tool parser, BF16 and
Thinking enabled. Temperature 1.0/top_p 0.95 follow the model card; extras are
top_k=20, min_p=0, presence_penalty=0 and repetition_penalty=1. Explicit endpoint
capabilities preserve those sampling options through the SDK. SGLang 0.5.20,
OpenHands SDK/tools 1.58.2, condenser v2, the public task, complete baseline
ancestry, DSec API/QEMU Container with EROFS + pmem/DAX, resource demand, official
verifier and iteration/time/token limits remain unchanged from the 1B trial.
There is no additional production-code change for this trial.

Result (`minicpm5-2b-r2/minicpm2b-r2`): **official score 1**, normal `finished`
termination, 37 model requests (36 agent requests and one summary) and 47 tool
calls: 44 terminal, one file_editor, one think and one finish. Agent/preparation
took 253.85 s; total including verification took 266.78 s. These times exclude
one-time weight download/transfer and model-server initialization.

The candidate changes only `mypy/checker.py`, adding `and not is_lambda` to the
`warn_return_any` guard. A fresh verifier sandbox applied that candidate and the
official test patch, then passed
`mypy/test/testcheck.py::TypeCheckSuite::check-flags.test::testReturnAnyLambda`:
one passed, 11,409 deselected. This is the task's official target-test result,
not a claim that the full mypy suite passed. Existing tests were not part of the
candidate patch.

All 37 native generations are paired with their exact raw token IDs/text and
parsed responses. Installed reasoning/tool parsers reproduce every saved
response; all tool names are available, with no `code` call or response-length
cutoff. The first actual 6,540-token input exactly matches the official native
template after SGLang normalization and contains the original instruction and
all four tools. Every request's effective sampling matches the configured
profile. The run demonstrates an end-to-end edit/test/grade path on DSec with
this 2B model. One successful episode does not establish a benchmark-wide model
ranking or explain the 1B failure solely by model capability versus prompt fit.

The preceding `minicpm5-2b-r1` attempt timed out waiting for model-file preparation;
no model server or benchmark episode started and it has no score. Its original
failure report is retained, including an attempted stop of a never-created model
unit. Independent stack checks passed with zero physical/CID/IAM usage and no
writable VM disks. The private r2 launcher tracks whether the model unit started;
this orchestration fix does not alter model behavior or production code.

Both episode sandboxes and temporary API/Edge/model services retired normally.
Physical/CID/IAM usage and writable VM disks returned to zero, GPU use returned
to 35 MiB, and original host container IDs/PIDs/start times/networks matched
their baseline. After evidence export, only the two stopped etcd containers
created for these r1/r2 attempts were removed following exact identity/image
checks. Model files and experiment evidence remain available.

Evidence archive: 382 files, 4,607,198 bytes, SHA-256
`342e650023e821d938f6762096473079c4dd25909d4878a2e7b386e60a3631bf`;
all 381 manifest entries independently verified locally. It contains raw and
parsed generations, exact SDK inputs, source snapshots, model/preparation/input/
sampling/parser audits, candidate patch, official verifier logs and cleanup
proof. Actual temporary control credentials were excluded and scanned before
export. Private local evidence is under
`.runtime/openhands-upstream-20261027/minicpm5-2b-r2/evidence/`; it is not a
published artifact or a training update.

##### Trajectory comparison: MiniCPM5-2B, Qwen3.5-2B and MiniCPM5-1B

This comparison uses executed actions and observations from 2B `minicpm2b-r2`,
Qwen `full-raw-r1/qwen-r7` and 1B `minicpm5-r1/minicpm-r1`. Agent turns below
exclude summarizer requests; action numbers count individual tool calls.

MiniCPM5-2B located the warning guard in `checker.py` by turn 4, reproduced the
issue with the public example in turn 5 (action 9), inspected existing lambda
detection and warning tests in turns 10–15, and edited the guard in turn 16
(action 25). Turn 17 re-ran the unchanged reproducer successfully; turn 18
confirmed a regular `def f() -> int: return g()` still warns when `g` returns
`Any`. Thus the fix suppresses the lambda case while preserving the flag's
ordinary function behavior. Its history searches only saw the previously
verified baseline/ancestor object set, not future solution commits.

Verification was imperfect but adaptive. In turn 19, direct pytest execution
of the `.test` data file collected zero tests. The model switched to the Python
test runner with `-k warning`, observing 27 passes, and later selected
`check-warnings.test` through that runner, observing 19 passes. Two larger test
runs waited 45 seconds each and were interrupted with real terminal `C-c`
actions. Neither completed, so neither establishes full-suite correctness.
Some extra ad hoc tests incorrectly label an inferred `int` expression as
`Any`; these examples are weak evidence and are unnecessary to the official
target-test result. The run has redundant post-fix checks, but they do not undo
its valid candidate. The one condensation occurs after the edit and successful
reproducer check; afterward the candidate remains intact and the agent finishes.

Qwen also read the warning guard by turn 8, the existing `is_lambda` definition
by turn 9, and both together in turn 10. Its problem was therefore not failure
to deliver the relevant source. It subsequently explored unrelated/nonexistent
names such as `Checker`, `self.s`, `mypy/ast.py`, and repeatedly searched for
`top_function` in `scope.py` and `nodes.py`. Turn 29 reproduced the same warning,
but this did not lead to a candidate. Its only `str_replace` attempt (turn 59)
supplied identical old/new text; the editor explicitly rejected it without
changing a file. It resumed searching and reached the iteration limit with an
empty patch. A malformed final tool block also interacted with the parser's
permissive matching, as documented above, but the lack of a real edit predates
that final event. Three condensations occurred; merely counting them does not
establish that condensation caused the loop.

MiniCPM5-1B instead ended after one response and zero tool calls, discussing the
warning as intended behavior and suggesting comments/invocations instead of a
repository fix. The full task and tools were present in its input and no raw
call was lost in parsing. Its lack of execution prevents any test-feedback
iteration. The episode demonstrates a failure to follow the repair workflow;
it does not isolate general model ability from prompt compatibility.

The observed differentiator is conversion of source evidence into a small
executed edit and checking that edit against actual feedback. This does not
prove a parameter-count law or a model-wide ranking. Sampling also differs:
MiniCPM5-2B actually used 1.0/0.95, 1B 0.9/0.95, while the recorded Qwen run
actually used 1.0/1.0 due to the earlier endpoint capability declaration issue.
That confound must be disclosed; it is not evidence that sampling caused Qwen's
loop. No additional rerun or training update was performed for this analysis.

#### MiniCPM5-2B-SFT versus final checkpoint (2026-10-28)

User-requested single-episode comparison on the same `python__mypy-15413` task.
SFT checkpoint `openbmb/MiniCPM5-2B-SFT` is pinned to
`3e0690c57f6ac772180498467c82e56170cc00ac`; its 5,033,557,128-byte
`model.safetensors` has official SHA-256
`caafb462c2b0f0cdcf282896771a48df173f16a2ade6d077399cb7269ec43e88`.
Both local and remote full-file verification passed. All 381 tensor names,
shapes and dtypes match the recorded final checkpoint.

This is explicitly a weight-only comparison. The SFT repository's standalone
chat template differs from the final release (9,060 versus 5,435 bytes); the
other five configuration/tokenizer files match byte for byte.
The controlled SFT model directory therefore uses the final release's exact
`config.json`, `generation_config.json`, `tokenizer.json`,
`tokenizer_config.json`, `special_tokens_map.json` and `chat_template.jinja`,
with the verified SFT weight file hardlinked into that directory. The original
SFT files remain preserved. The final release's weight index is not copied.
The unchanged served alias is an API compatibility name, not the identity of
the loaded weights; model argv and the artifact manifest record that identity.

The same installed SGLang, OpenHands SDK/tools 1.58.2, condenser v2, application,
DSec API/QEMU Container, EROFS + pmem/DAX, complete repository ancestry, public
instruction, official verifier, resource limits, Thinking and sampling profile
are retained. Actual requests use temperature=1.0, top_p=0.95, top_k=20,
min_p=0, presence_penalty=0 and repetition_penalty=1, with response=32,768,
context=65,536, 100 iterations and a 1,200-second episode budget. Source hashes
for all 15 captured implementation modules and installed package versions match
the final run. Exact initial input reconstruction passes at 6,540 tokens; the
only initial prompt text difference is OpenHands' current date/time line.
No product, SDK or model-server source was changed for the trial.

| Observed metric | SFT `minicpm2b-sft-r1` | Final `minicpm2b-r2` |
| --- | ---: | ---: |
| Official reward | 1 | 1 |
| Termination | finished | finished |
| Model requests, including summaries | 81 | 37 |
| Tool calls | 78 | 47 |
| Condensations | 2 | 1 |
| Successful edit request, agent turn | 15 | 16 |
| Successful edit request, seconds after conversation creation | 29.32 | 62.02 |
| Tool observations marked error | 6 | 0 |
| Terminal 45-second pauses | 3 | 2 |
| Agent/preparation time, seconds | 312.43 | 253.85 |
| Total including official verification, seconds | 325.14 | 266.78 |
| Generated native tokens, including summaries | 11,069 | 11,420 |
| Sum of SDK completion latency, seconds | 119.77 | 118.00 |
| Sum of recorded terminal/editor operation durations, seconds | 168.12 | 110.63 |

Native request counts comprise 79 agent requests plus two summaries for SFT,
and 36 agent requests plus one summary for final. SFT's 78 tools are 62 terminal,
15 file_editor and one think; it finishes with a normal assistant message.
Final's 47 tools are 44 terminal, one file_editor, one think and one finish.
Timing excludes one-time model preparation, model-server startup and private
API/Edge startup. Tool-operation sums include transport and command waiting,
not just guest CPU execution; SDK latency sums are recorded client durations,
not an inference-engine kernel measurement.

SFT reproduces the warning in turn 2, reads the warning guard in turn 7 and the
existing lambda detection in turns 8–9. Two `str_replace` attempts in turns 11
and 13 fail because their indentation does not match the file. It inspects
whitespace with `cat -A`, switches to a smaller Python text replacement in turn
15, and re-runs the reproducer successfully in turn 17. This recovers from the
editor errors without human intervention. Its final patch is byte-identical to
the final model's patch, SHA-256
`b3af1a1ebb799a9615bc91a5d9393ecbe1ab106730f3f33c456ec70161f7ae9d`:
one added `and not is_lambda` in `mypy/checker.py`.

Most excess interaction occurs after the fix: SFT repeatedly tries to execute
`.test` data directly, searches for test entry points and rechecks examples.
Three broad test commands hit the 45-second terminal pause. Twice it submits a
new command before the prior one finishes; the terminal refuses those commands
and it later sends `C-c`. Twice it requests timeout=180; the unchanged DSec
45-second per-call limit rejects those requests with polling instructions.
These four refusals plus the two editor mismatches account for six error
observations; they are explicit feedback, not silent command loss. Eventually
turns 72–73 select existing warning tests correctly and observe 19 and 14
passes. The original lambda and a regular function are also checked: the former
no longer warns and the latter still warns. The separate fresh official
verifier passes `testReturnAnyLambda` (one passed, 11,409 deselected). Full-suite
completion is not established.

The wall-clock difference is 58.35 seconds, approximately 21.9% longer for SFT,
despite 2.19 times as many model requests. Total generated tokens and summed
completion latencies are similar; most extra measured time is in terminal/editor
operations (57.50 seconds more), including one extra 45-second pause. Thus raw
request count alone would substantially exaggerate the runtime difference.

Both checkpoints solve this instance, and SFT actually reaches a correct edit
earlier in this episode. The final checkpoint is more economical in subsequent
verification and finishes with fewer operations. This single stochastic sample
does not establish a success-rate gain from RL/OPD, distinguish RL from OPD, or
justify attributing the 1B failure to missing post-training. It supports using
the SFT checkpoint as a model that already has tool and repair ability when
designing later training experiments; no training update occurred here.

All 81 generations were retained with original IDs/text and parsed responses;
installed parsers reproduce all responses, no unavailable tool name or token
cutoff appears, and effective sampling matches on every request. All agent and
verifier sandboxes, writable disks and CID/IAM/physical leases retired. Original
host container identities and networks matched their baseline. Temporary
API/Edge/model services stopped, the exact stopped etcd container for this run
was removed after export, GPU use returned to 35 MiB and disk free space was
29.25 GiB. Model files and evidence are retained.

Evidence archive: 715 files, 8,467,369 bytes, SHA-256
`c1a25d1a829c90f1724a9c89337bb148735707e001bae4dbd3e8643371ef15cb`;
all 714 manifest entries independently verified locally. Includes original
model metadata and file comparisons, actual weight/config provenance, exact
SDK/native requests and responses, parser/input/sampling audits, the comparison
audit and initial-prompt diff, source snapshots, patch and official logs.
Temporary control credentials were excluded and scanned before export. Private
local evidence is under
`.runtime/openhands-upstream-20261027/minicpm5-2b-sft-r1/evidence/`;
the post-export container retirement record is saved beside the archive.

## 2026-10-28: MiniCPM5-2B-SFT SAO mechanism pilot

**Result: the real training lifecycle passed; learning improvement was not
demonstrated. This is our implementation of paper mechanisms on native verl,
not an official SAO implementation or reproduction of the paper's results.**
The [paper](https://arxiv.org/html/2607.07508) uses a different model, scale,
critic preparation and coding evaluation. The application README records the
alignment/deviations table. Both actor and critic use MLP LoRA here (rank 8,
alpha 16), while critic attention remains frozen and its scalar value head is
also trained. The SFT backbone is the same controlled checkpoint as the preceding
comparison; the value head is newly initialized, without value pretraining or
the paper's ten-step critic warmup. These omissions matter to learning quality.

The optional application uses native verl FSDP2 engines at the existing pinned
revision `8718ca30a3f002f93b7c4fd99b9b2506718681bc`. It supplies skip-observation
GAE, two critic updates per actor update, and strict coding DIS `(0.2, 4)` using
the actual rollout logprobs. Actor/critic learning rates are `1e-6`/`5e-6`.
Unlike the paper's batch of 128 with group size one, this feasibility pilot
updates from one completed episode at a time. It is not GRPO.

The unchanged OpenHands SDK 1.58.2 agent and guest tools use DSec HTTPS API →
mTLS Node/Edge → QEMU Container with EROFS + pmem/DAX. No model or algorithm
management was added to DSec. The optional local inference gateway retains the
native prompt IDs, sampled IDs, behavior logprobs and policy version for each
SDK completion. Every segment trains on its own native context; history is not
flattened and re-tokenized. Auxiliary summaries are excluded as policy targets.
This run did not reach condensation, so that exclusion is covered by regression
tests rather than by this particular real trajectory.

All four episodes use `python__mypy-15413`, independently clean baseline
repositories with full permitted ancestry, the same sampling settings, a 65,536
context and 32,768 response limit. The local 20-iteration ceiling is shorter
than the previous 100-iteration evaluation and the paper's 300-turn budget.
Do not compare these zero scores directly with the earlier successful sample.

| Episode | Behavior version | Sampled action tokens | Official reward | Termination |
| --- | --- | ---: | ---: | --- |
| Before evaluation | 0 | 1,983 | 0 | Iteration limit |
| Training 1 | 0 | 2,164 | 0 | Iteration limit |
| Training 2 | 1 | 2,493 | 0 | Iteration limit |
| After evaluation | 2 | 2,057 | 0 | Iteration limit |

Each episode contains 20 original generations. Independent audit re-matched
all 80 SDK response IDs to native IDs/logprobs. Native SGLang request records
confirm `policy-v1` and `policy-v2` for the corresponding sampled episodes;
this is not merely a requested version label. Fresh official grading produced
valid binary zero rewards, not synthetic zeros for an infrastructure failure.

Both policy steps performed two real critic optimizer updates and one real
actor update, with finite gradients. Actor gradient norms were 0.52734375 and
0.6953125; DIS retained all 2,164 and 2,493 tokens. Adapter identities changed
in a continuous chain:

```text
v0 d8cc896c79587c250aa834f9fc6b43d061a310d1440e63ff3194ab71d3dbe96c
v1 750aa3560b559cfd7dbead4343306e42bd766c899d223047ee3cf4ee35b96c78
v2 00d61e924b3ec187bf4f25c588699ffec66358087def07d30a70df7943219735
```

A zero reward can still produce a nonzero actor gradient through the learned
value baseline. With an unprepared critic, observing that gradient is proof of
execution, not evidence that the update improves the policy. There is no held-out
evaluation here and no claim of SAO effectiveness, stable convergence or a
success-rate increase.

The single GPU explicitly alternates between SGLang generation and native verl
updates. The GPU lease was exclusive throughout. The two update intervals took
319.64 and 281.34 seconds, including memory handoff and adapter publication;
generation spent 316.58 seconds waiting for this lease. The second sandbox was
alive when the first rollout was admitted, but its first generation waited for
the first update and then used version 1. No individual trajectory spans mixed
policy versions. Thus neither policy-lag correction nor simultaneous GPU
training/inference throughput has been demonstrated. Peak native trainer
allocation across the updates was 9,373.93 MiB, not a whole-GPU peak metric.
The native allocator logged one recoverable allocation retry during critic
training; the update continued successfully. This short-trajectory acceptance
does not establish memory headroom for 32k-token responses or longer contexts.

During feasibility diagnosis, the native fused CE received a single-rank
DTensor LM-head weight whose wrapper `data_ptr()` was zero; the local storage
pointer was valid. Plain/local tensor calls passed while the wrapper failed.
The optional trainer converts that frozen single-rank head to its local tensor
before calling the unchanged native Triton autograd operation. It rejects a
trainable head or multi-rank mesh. FlashAttention/native kernels remain enabled;
native verl sources were not edited. This process-local compatibility bridge
is an additional integration adaptation, not part of the SAO algorithm.

Final cleanup proved zero physical/CID/IAM leases and retired private writable
disks. Original host container IDs, PIDs, start times and networks were unchanged.
Temporary model/gateway/trainer/API/Edge services stopped; GPU use returned to
35 MiB. The exact stopped private etcd container was removed after its image and
status were checked. No new root permissions were installed. An owned 10.91 GB
failed-probe crash dump was identified by its exact command line and removed,
retaining the textual diagnostics. Future trainer launches disable core dumps.

Private evidence is retained under `.runtime/sao-20261028/evidence/`; all 415
downloaded pilot files (264,528,115 bytes) were independently checked against
remote SHA-256 values. It includes exact generations, official verifier logs,
candidate patches, GPU lease timing, LoRA adapters and optimizer states. The
independent audit, update reports and host-baseline reports are retained beside
the pilot. Private API key/certificate/token configuration directories were not
downloaded. Checkpoint export passed; checkpoint resume has not been validated.

### Why the earlier SFT reward was one but this pilot scored zero

The pilot shortened `max_iterations` from 100 to 20 to reduce lifecycle-test
cost. This is a changed evaluation condition, not evidence of a model regression.
Thinking, temperature/top-p and additional sampling controls, response/context
limits, tool definitions and the public task are unchanged. Comparison of the
initial SDK messages finds only the automatic current-datetime text differs.
The inference route additionally enables LoRA and native training capture;
their effects on sampling have not been isolated by a controlled replay.

The earlier successful SFT sample made the correct `and not is_lambda` edit at
generation 15 and ultimately finished after 81 requests, including two summary
requests. It did **not** need all 81 requests to create the passing patch.
In the new untrained baseline, generation 14 creates `test_issue.py`, generation
15 reproduces the warning, and generations 16–20 search existing tests. At the
cap it has not edited `mypy/checker.py`. Its candidate contains only the new
reproduction file. The after-training sample likewise creates that file and
spends generations 12–20 repeatedly inspecting the relevant checker condition,
without applying a fix. Both fresh official verifiers therefore run the
unfixed checker and fail `testReturnAnyLambda`.

Training sample 1 also has only the reproduction file. Training sample 2 tries
the correct conceptual edit at generation 20, but its `old_str` indentation
does not match the source; the upstream editor explicitly reports no
replacement. The iteration cap then prevents a corrective next action, leaving
an empty patch. No edit is silently dropped or converted into a synthetic zero.

The observed causes are different action trajectories and termination before a
valid source fix is present. Restoring 100 iterations is necessary for matching
the earlier evaluation budget, but does not guarantee that a new stochastic
trajectory will succeed. These observations do not isolate why the trajectories
diverged, and neither the budget difference nor two updates establishes learning
degradation or improvement. A fair effectiveness comparison must first restore
the common evaluation settings and repeat before/after sampling.

## 2026-10-28: restoring the 100-iteration budget

The CLI default is restored to 100 iterations. Evaluation preserves the original
case deadlines (1,200 seconds per episode, 600 seconds per model request),
Thinking and the original sampling controls, 65,536 context and 32,768 response
limit. Training episodes separately allow 7,200 seconds to include the shared
GPU queue; those waits remain visible rather than extending evaluation deadlines.

Before launching fresh RL, a capacity probe used the longest three agent
segments from the earlier successful SFT episode, with native prompt/response
lengths `(26533, 62)`, `(26244, 80)` and `(25001, 57)`. Historical behavior
logprobs were not recorded. Consequently these records are **not** treated as
SAO training samples and no logprobs are reconstructed or invented. The probe
uses diagnostic NLL/value losses, performs two rounds totaling four critic and
two actor optimizer steps, then discards every update. Fresh RL initializes from
the original SFT backbone with fresh adapters/value head.

The probe passed with resident SGLang using the same release/resume profile as
the live application. Peak native trainer allocation was 12,068.38 MiB, reserved
memory 13,302 MiB, allocator retries zero; sampled whole-GPU peak was 15,133 MiB.
It took 296.97 seconds including initialization/handoff. These measurements
cover a maximum 26,595-token segment, not arbitrary 65k contexts. Residual whole
GPU headroom was under 1 GiB, so longer newly collected segments still require
monitoring. All probe/model processes stopped normally. The twelve downloaded
probe files were independently verified against remote SHA-256 values under
`.runtime/sao-20261028/memory-evidence/`.

The new live run is `closedloop-r2`, with unchanged published environment and
official verifier. Its **before-training evaluation scored one** at the
iteration limit. The 108 captured requests comprise 97 agent generations and
11 auxiliary summary generations; SDK iteration counts are not identical to
generation counts. The candidate adds `and not is_lambda` to the relevant
checker condition and the official lambda regression test passes. It also
contains additional model-authored reproduction/test files, so this is not a
byte-identical replay of the earlier one-line patch. The longest agent segment
is 29,678 tokens, exceeding the historical probe's maximum; no claim is made
that the initial probe exhaustively established capacity for every new segment.
The training attempt was subsequently stopped before any update to repair the
summary recovery boundary described below; its before score remains diagnostic
evidence, not part of the corrected run's before/after comparison.

### Recovery boundary found during the 100-iteration attempt

Training episode 1 reached token-pressure condensation after 35 agent replies.
Two summary requests generated 32,768 tokens entirely in Thinking, with empty
final content and `finish_reason=length`. The native capture retained both;
this was not CUDA OOM. Upstream hard reset then reduced its per-event preview
length, but our version-2 evidence serializer ignored that display-preview cap
to avoid dropping data. Consequently the fallback did not obtain its intended
smaller request budget. It generated further chunks and discarded the batch
when an earlier empty response was validated only after all chunks returned.
An intermediate merge response was initially mistaken for completed recovery;
later logs showing another failed reset corrected that interpretation.

The attempt was explicitly interrupted at policy version zero. No actor or
critic update was admitted. All sandbox/physical/CID/IAM leases and private
writable disks retired, original host containers/networks were unchanged, and
GPU use returned to 35 MiB. The exact stopped private etcd container was removed
after checking its image identity. Completed raw requests, Thinking, outputs,
events and the valid before-evaluation patch/score are retained under
`.runtime/sao-20261028/attempt-r2/`. The interrupted training episode remains
unscored; it is not converted into a zero reward.

Version 3 restores the recovery contract: measure the token budget of upstream's
bounded previews, then fit complete records/fragments under that budget. The
previews never become model evidence. Each empty chunk now fails immediately,
letting the unchanged upstream reset loop choose its next smaller limit. Eleven
tests passed on the installed SDK 1.58.2, including smaller recovery requests,
byte-for-byte reconstruction of every record at two fallback sizes, unchanged
sampling/default triggers, synchronous/asynchronous equivalence and the actual
SDK asynchronous conversation loop. No native SDK source was changed in this
repair. The consumer's initial rollout wait was also increased from one to three
hours to accommodate API initialization, original before-evaluation and a
bounded two-hour training episode.

`closedloop-r3` starts from SFT again with version 3 used consistently for its
before-evaluation, training and after-evaluation; the model, Thinking/sampling
controls, 100-iteration cap and response/context limits remain unchanged. Its
before-evaluation finished voluntarily with reward one (98 total model
requests). Training episode one also finished with reward one: 56 policy
generations, 7,074 sampled action tokens, maximum native segment 25,581 tokens.
It excludes the episode's auxiliary summaries, including three length-capped
Thinking-only generations. Both critic updates completed, but actor backward
failed on segment 33 before its optimizer step or any policy publication.

The failed 270 MiB allocation occurred with 10.55 GiB allocated, 3.20 GiB
reserved but unallocated, and only 33.12 MiB free device memory. Actor allocations
after segments 27–32 were identical at 4,848.59 MiB. That pattern supports
allocator fragmentation from changing sequence shapes, rather than retention of
every earlier segment's activation graph. The earlier three-longest-segment
probe had not exercised the complete shape history; its success was insufficient
to establish end-to-end capacity. No actor update was committed. The second
episode interrupted by this infrastructure failure remains unscored.

The next candidate enables `PYTORCH_ALLOC_CONF=expandable_segments:True` only
for the learner subprocess before CUDA initialization. SGLang retains its own
memory-saver allocator/configuration. The
[PyTorch 2.12 documentation](https://docs.pytorch.org/docs/2.12/notes/cuda.html#optimizing-memory-usage-with-pytorch-cuda-alloc-conf)
describes expandable segments as a way to reduce unusable allocation slices
when shapes vary. This is an external training-recipe setting, not a DSec sandbox
mechanism. No SGLang reservation, response/context budget or sampling parameter
is reduced. A regression verifies that the setting does not leak into the model
server's environment.

`closedloop-r4` reuses the valid version-3 before-evaluation and the fully scored
first rollout, preserving its native IDs and actual behavior logprobs. Because
the failed attempt never published an actor update, that rollout still has the
same frozen SFT behavior policy. Fresh actor/critic engines and optimizer states
replay the entire 56-segment update under the allocator candidate. This is not
optimizer-checkpoint resume and does not reconstruct historical probabilities.
The source paths/report digests are explicitly recorded as reused evidence.
Only after the full replay passes and publishes policy version one does it
start a fresh second training episode; after-evaluation will also run fresh.
The complete first replay passed: all 56 segments completed both critic
updates and the actor update; 7,074 of 7,074 policy tokens were retained by DIS.
The expandable-segment flag was observed in all three update passes, the actor
state changed, and policy version one was published successfully. Peak native
trainer allocation through this update was 11,773.86 MiB. This establishes the
allocator repair for the complete recorded trajectory, beyond the earlier
three-segment probe. A fresh second episode now uses policy version one; its
update and fresh after-evaluation remain pending.


The fresh second episode reached the 100-iteration cap and still received an
official score of one. Its 97 agent segments contain 16,376 sampled tokens and a
maximum native segment length of 31,741 tokens. Both critic passes and the actor
update completed; 16,375 policy tokens passed DIS. The published actor hash
changed again and policy version two was loaded successfully. Native peak CUDA
allocation reached 13,451.43 MiB; sampled whole-card use peaked at 15,929 MiB.
These are different accounting boundaries. This validates both recorded updates,
not arbitrary longer contexts or every possible trajectory shape.

The first after-evaluation failed before its first action could be delivered to
the SDK: SGLang had rotated its hourly native request log. The capture reader
tracked positions by pathname, so the new current file inherited the old file's
large offset and its actual completed request was missed. The model returned
HTTP 200, but capture correctly refused to invent token evidence and returned
502; this episode is unscored, not reward zero. The learner report and both
published checkpoints remain valid. The original pilot/root summary omitted
update counts because it populated them only after successful evaluation;
reporting now persists completed updates before evaluation starts. The original
failed reports are retained unchanged.

The capture reader now tracks offsets by the opened file's device/inode, detects
size shrinkage, and tolerates a rename between directory enumeration and open.
The raw HTTP response is preserved before waiting for the native record, even
when capture subsequently fails. All five targeted capture-contract tests passed
on both Mac and the installed Linux SDK. The fixed reader also recovered the
actual missing response ID from the real rotated logs. A new evaluation-only run
loads the verified second adapter checkpoint with the same inference settings,
100-iteration cap and original evaluation deadlines; it does not repeat training.
The recovery completed with an official score of zero after four model replies
and three executed tool calls. No context/response/iteration/episode cap was hit.
Only `test_issue.py` was added; `mypy/checker.py` was not modified, and the official
`testReturnAnyLambda` case failed. The fourth reply expressed an intention to
continue searching, but generated XML for `execute_bash` although the advertised
tools were `terminal`, `file_editor`, `finish`, and `think`. SGLang retained that
unknown function as ordinary content and returned no structured `tool_calls`;
the SDK's resulting `finished` status does not establish that the model believed
the code repair complete. Replaying the actual installed detector produced zero
calls; changing only the function name to `terminal` in an in-memory diagnostic
produced one valid call. Neither diagnostic executed a command or changed the
official score. No alias or automatic repair was added to the scored agent.

A local backup of all 1,137 original pilot files and the separate recovery
pilot has been independently checked against remote SHA-256 digests. An offline
audit rechecked every selected native sampled ID/logprob record, exclusion of
summary targets, behavior versions 0/1, use of version two for after-evaluation,
identical sampling/token/evaluation budgets, nonoverlapping GPU leases and both
exported actor-state hashes directly from safetensors. Both native updates and
recovery passed their execution contracts. The original failed after-evaluation
remains unchanged and unscored; the independent audit explicitly links the
separate recovery instead of replacing the failure with a success claim.

The result is before=1, training rewards=1/1, after=0. This validates the bounded
SAO-adaptation training/publication/evaluation lifecycle; it does not demonstrate
a learning improvement or establish degradation causally from one stochastic
before/after sample. Critic pretraining and paper-scale asynchronous mixed-policy
training remain outside this pilot. Both API stacks retired normally with zero
physical/CID/IAM leases and no private writable disks; original host containers
and networks were unchanged. The two exact stopped private etcd containers were
removed after image/status verification, and GPU use returned to 35 MiB.


### Bounded native Stop recovery and a new frozen baseline

The MiniCPM profile can now opt into the pinned SDK's Stop hook. The integration
observes the original assistant MessageEvent; unparsed function markup receives
an environment feedback message through upstream's existing loop. It never
turns response text into a command or aliases `execute_bash` to `terminal`.
The original assistant text remains in history and the next native request.
Recovery is bounded to three feedbacks by default and does not restart the
conversation or extend the 100-iteration/episode budget. Plain final answers,
fenced examples and valid structured calls retain their ordinary behavior.
The profile is explicit and disabled by default for other providers/formats.

Eight deterministic recovery checks passed against the installed Linux SDK,
including the captured fourth reply from policy version two, real sync/async
Stop execution, exact assistant text plus feedback in the next request, no
sandbox dispatch for the invalid call, and both recovery and iteration caps.
The broader installed SDK/tool/Thinking/condenser/capture suite passed all 33
tests. A failed hook is infrastructure failure; a policy that exhausts recovery
still submits its actual candidate for official scoring. Its termination is
labeled `unparsed_tool_call` rather than treating SDK `finished` as repair proof.
The fixed helper command reads controller metadata only; model-generated
commands never enter that host subprocess.

`baseline-r5` is a fresh frozen MiniCPM5-2B-SFT episode with this profile enabled.
It preserves the task/environment, Thinking, sampling, response/context budgets
and original 1,200/600-second episode/model-request limits. It loads no trained
adapter and performs no optimizer updates. The original before=1/after=0 pair
remains historical evidence under the previous interaction rules; this new
baseline must not be compared to an old after-evaluation as a training effect.
The new baseline completed with official score one at the 100-iteration cap.
It generated 110 model requests (96 agent generations and 14 auxiliary summary
requests) and 101 native ActionEvents; requests, tool calls and SDK iterations
are different counts, and a generation can contain multiple calls. Agent elapsed
time was 904.87 seconds; the full case including fresh verification took 918.99
seconds, within the original policy budget. The submitted patch adds
`and not is_lambda` in `mypy/checker.py`, and the official `testReturnAnyLambda`
case passed. This does not claim the entire upstream test suite passed.

One real reply used the unavailable name `str_replace_editor`. The native Stop
hook supplied feedback; the very next agent request retained that assistant
reply verbatim plus the environment feedback, and the model then generated a
structured `file_editor` call. This was a new model response, not automatic
aliasing of the rejected call. No further recovery attempt was recorded.
The installed Linux SDK suite passed 33 tests; a subsequent nine-test targeted
run also verified the real SDK abort path for hook execution failure. A separate
regression confirms baseline, training and after configurations preserve the
same recovery profile and budget. No native OpenHands source or DSec core
execution mechanism was modified.

An independent offline audit checked all 455 pilot files against remote
SHA-256, native token/logprob records and summary exclusion, absence of a loaded
adapter (policy version zero throughout), the actual recovery transition and
unchanged sampling/token/deadline settings except for the explicitly enabled
recovery profile. The API and physical/CID/IAM usage retired normally, private
writable disks were removed, and host containers/networks were unchanged. The
exact stopped private etcd container was removed without force after normal
retirement.

This frozen result establishes the updated interaction baseline, not a learning
improvement. The old trained policy's zero score remains evidence under the old
harness and is not compared to this new frozen run as a training effect. No
additional optimizer updates or long training run were started in this stage.

## 2026-10-29: saved-critic diagnosis before value-only warmup

Read-only evaluation restored the final critic from the two-update pilot,
including the TOKEN_CLS scalar head and MLP adapters. Its adapter state digest
was `60e19e89996afcdaeb8dd188ffd4ad76d38c3145a7ff39ffa4c81ccf3fb7224e`
before and after evaluation. No actor was loaded and no optimizer step ran.

On the current-profile frozen SFT reference (official reward one), all 96
agent generations / 11,376 sampled tokens yielded mean value -0.1372,
MSE 12.0898, RMSE 3.4770 and MAE 2.6660. Predictions ranged from -11.375
to 17.25; 86.29% lay outside the binary reward interval. The original two
in-sample positive episodes also had RMSE 3.3768 and 3.2222. Historical negative
episodes had different interaction/budget settings and were diagnostic only.
Each episode has constant Monte Carlo return, so its explained variance is
undefined. These results show an unprepared value scale, not a missing-checkpoint
evaluation or a justified claim that all critic optimizer updates were absent.

The fixed follow-up collects four fresh frozen-policy episodes with the current
Stop-hook profile, original 100-iteration/1,200-second budget and unchanged
sampling. Every official score is retained. The prior frozen reference is
excluded from optimizer batches. The critic-only application provides ten
bounded updates, full native contexts, separate value-head/MLP rates and saved
before/after token predictions. Neither the all-positive historical corpus nor
old-harness zero scores are used to manufacture a current mixed-reward set.
Collection and warmup outcomes are recorded below after completion.

Collection completed with official rewards `1,1,0,1`. All four episodes used the
same current interaction protocol as the reference, loaded no policy adapter,
and retained exact native sampled IDs/logprobs. There were 294 agent generations
and 39,812 action tokens (auxiliary summaries excluded). The longest complete
native context was 32,019 tokens. No incomplete capture was excluded. Agent and
fresh-verifier sandboxes retired; API physical/CID/IAM usage returned to zero,
the private writable disks were removed, and original host container/network
identity checks passed. The exact stopped private etcd container was removed
without force. Collection's 1,338 archived files matched remote SHA-256.

The saved-head warmup performed ten critic-only optimizer steps, eight full
native generations per step, using MLP/head rates `5e-6`/`1e-4`. Both actor
construction and updates remained absent. The full reference's pre-update
values matched the original diagnosis token-for-token. Reference RMSE fell from
3.4770 to 2.9554 (MSE 12.0898 to 8.7344); fixed training-check RMSE fell from
3.3007 to 2.8325. However, 83.69% of reference predictions still fell outside
`[0,1]`, with range `[-10.6875,17]`. The report's limited relative-improvement
gate passed; **it does not certify calibration or authorize Actor updates**.
Peak native CUDA allocation was 12,726.58 MiB. Original Actor and Critic adapter
file hashes stayed unchanged. An independent audit verified 1,350 evidence files,
the exact native corpus, reference exclusion, per-token MSE and exported adapter
digest. Ten steps reduced error but did not repair the value scale sufficiently.

The user's reference to
[JustRL-II](https://github.com/merak0514/JustRL-II/tree/75814318f243af7e39af337c1173fc3927c9635b)
identified a more direct initialization fix. Its scalar-head constructor zeroes
the projection and seeds bias with an expected reward prior; the released math
configuration uses `0.52`. Its Megatron load path re-seeds after loading a policy
checkpoint and synchronizes optimizer master parameters, while preserving real
critic warm-start/resume heads. Our HF TOKEN_CLS path has distinct head keys;
there is no evidence here of that Megatron LM-row overwrite. We adopt the
general initialization and checkpoint-lifecycle principle, not that loader patch
or its PPO training recipe. Our initialization runs after base/PEFT loading but
before FSDP/optimizer construction, so no stale optimizer copy exists then.

A separately recorded comparison keeps the original MLP LoRA, backbone, all
four training episodes, reference, minibatch response IDs, rates and ten-step
schedule unchanged. Only the scalar head is explicitly reset: weight zero,
bias `0.75`, the unweighted mean of the four training-episode rewards. The
reference score does not enter that estimate. The default restore path still
preserves trained head weights; reset requires a separate explicit option.
Four examples do not provide a reliable global coding-task prior. The initial
constant prediction fixes random projection scale but has no state discrimination.
JustRL-II's 30 critic-only rollout batches are not ten optimizer updates on this
small corpus, and are not a replacement for SAO value-model preparation.

The initialization comparison completed all ten updates with no Actor loaded
or updated. Exact native output confirmed the initial constant `0.75` on the
fixed training check and all reference tokens. Results (RMSE):

| Initial value head | Fixed training check, before → after | Full untouched positive reference, before → after |
| --- | --- | --- |
| Saved head from the earlier pilot | 3.3007 → 2.8325 | 3.4770 → 2.9554 |
| Zero weight + training-episode mean bias | 0.5207 → 0.3878 | 0.2500 → 0.4191 |

The seeded head's fixed-check MSE fell from 0.27110 to 0.15037, with pooled
explained variance rising from zero to 0.4025. This is within training
trajectories, not held-out-task discrimination. Its reference MSE **rose** from
0.06250 to 0.17562, so its relative-reference-improvement gate did not pass.
The reference contains only one successful episode and has undefined explained
variance. That result does not establish value generalization; a low starting
error from a constant prior is not learned prediction. The useful established
result is removal of the initial projection noise: after warmup, reference
out-of-range predictions were 1.46%, versus 83.69% with the saved head. First
gradient norm was 17.75 versus 109.5. These are measured values for this pilot,
not JustRL-II's reported ablation numbers.

An independent comparison verified identical native response IDs, return labels,
token counts, corpus digest and optimizer schedule at every step. It rebuilt the
initial seeded adapter digest from the old checkpoint bytes by replacing only
the scalar head, confirming that MLP state did not change at initialization.
Both exported adapter digests and all per-token error summaries were recomputed.
All 1,367 archived files matched remote SHA-256. Original Actor/Critic checkpoint
file hashes remained unchanged. All three user units exited successfully and
the GPU returned to 35 MiB; no additional model sampling or Actor update ran.
Local application checks passed 54 tests with 11 optional-dependency skips;
two real PEFT/native-verl CPU tests passed on Linux, including exact checkpoint
restore, reset guard, constant output and nonzero first head gradient.

Engineering choice: zero-weight/prior initialization is available for a fresh
critic (neutral prior `0.5` unless explicitly supplied), while restoring a trained
critic preserves its value head. This experiment's `0.75` remains a small-corpus
case-specific choice. Keep the new checkpoint as a candidate, without automatic
Actor promotion. Independently collected positive and negative validation
episodes are needed before treating it as a reliable SAO value baseline.
Private raw evidence and comparison are retained in
`.runtime/sao-20261029/evidence/` in the research workspace.


## 2026-10-29: independent critic gate before a bounded SAO follow-up

The fixed next stage collects exactly four fresh frozen-SFT episodes on the same
issue, with the same 100-iteration interaction profile, sampling and official
verifier. These are independent of all prior critic optimizer episodes and the
old reference, both by episode path and native generation identity. All four
scores remain in the result, including formatting failures and bounded failures.

The candidate is the zero-head/prior warmup checkpoint, fixed before sampling
(adapter file SHA-256
`511f7a2fb24b5456edee182d1f4c108935fa893efc28792e0e60caa0ea3e951d`).
Read-only validation restores that exact head and LoRA with no optimizer,
scheduler or Actor. It evaluates every complete agent generation at its full
native context, excluding auxiliary summaries, and saves every action-token
value. The primary criterion is the **arithmetic mean of each episode's MSE**,
so a long failed trajectory cannot outweigh several shorter successes merely
by generating more tokens. Pooled token metrics are secondary.

The predeclared gate requires both actual reward classes, finite predictions,
an unchanged candidate and primary MSE no worse than constant value `0.75`.
The constant comes from the earlier training corpus and is not re-estimated on
these validation episodes. A failed gate or missing class stops Actor training;
it does not trigger automatic resampling or more critic warmup.

Only if the gate passes, reuse these exact four episodes as the before evaluation
and run two Actor updates, each preceded by two Critic updates, then four fresh
after evaluations. The learner preserves the prepared Critic head, starts the
Actor from the controlled SFT checkpoint, and stores response IDs, token counts,
behavior versions, before/after values, exact Critic targets and actual Actor
advantages before the corresponding optimization stages. Hash checks tie reused
evaluations to the same manifest, checkpoint and official evidence. This is a
single-issue integration experiment; four scores cannot establish general
coding improvement or reproduction of SAO's reported results.


The four fixed independent episodes completed with official rewards `0,1,1,0`
(before success rate 50%). The first stopped after bounded unparsed-tool
recovery; the fourth reached its 1,200-second episode deadline. All results
were retained and all sandbox/API leases and private disks retired normally.
The exact stopped etcd container was removed without force. Native capture
checks found 143 complete agent generations / 19,455 action tokens, maximum
agent context 26,203 tokens, no excluded incomplete records and no response-ID
or episode overlap with the earlier critic corpus/reference.

The fourth episode's latency must not be mistaken for long useful agent
reasoning. Its 41 completed agent generations produced 5,666 tokens in 78.75
seconds, whereas 12 auxiliary summaries produced 61,210 tokens in 801.32
seconds, including a 32,768-token summary. The third likewise used two
summaries totaling 33,817 tokens / 420.39 seconds. These completed-call durations
are from the native gateway record, not the entire episode wall time. Summaries
are excluded from all policy/value targets. The current before/after comparison
keeps the same condenser and sampling; changing summary limits requires a
separate harness comparison.

Read-only validation restored the candidate exactly, constructed no optimizer
or scheduler, made zero parameter updates, and verified the unchanged final
adapter digest. Full action-token MSE by episode:

| Episode reward | Candidate Critic | Fixed value 0.75 |
| --- | --- | --- |
| 0 | 0.449191 | 0.562500 |
| 1 | 0.146587 | 0.062500 |
| 1 | 0.174669 | 0.062500 |
| 0 | 0.414022 | 0.562500 |
| Equal episode mean | **0.296117** | **0.312500** |

Independent double-precision recomputation from every exported value confirmed
the gate. The MSE improvement is only 0.016383 (5.24%). Failure-group MSE fell
from 0.562500 to 0.431606 while success-group MSE rose from 0.062500 to 0.160628.
This is a narrow pass of the fixed small-pilot rule, not established state
ranking or general calibration; a neutral constant 0.5 would have MSE 0.25 on
these equally weighted binary episodes. That post-hoc comparison does not
replace the predeclared prior or change the run's gate. Peak native validation
CUDA allocation was 1,749.73 MiB, with the GPU returning to 35 MiB afterward.
The two-update/four-after-evaluation follow-up was then started, reusing these
exact four before episodes and preserving the candidate head.


The follow-up's model/trainer were ready before its private API/Edge stack.
Time from trainer process start to the first rollout was 661.62 seconds. An
Edge process sample during startup showed the 22-environment execution catalog
under validation, 921.44 GB cumulative logical reads and 424.91 GB actual disk
reads; its open image included the shared `openhands-sdk-1582/tools.erofs`.
These are process I/O counters, not additional disk capacity consumed or VM
creation latency. The startup is recorded separately from agent execution and
learner updates. No validation or storage behavior was changed mid-comparison.


The first new training episode reached the iteration limit with official reward
one: 97 agent generations / 9,318 action tokens. The limit is not itself a
failure label; the submitted patch passed the independent verifier. Its exact
native contexts total 1,637,796 tokens per complete learner pass, maximum
28,233, explaining why a small number of target tokens still requires substantial
single-GPU training time. A second sandbox episode began while the first was
being verified, and its initial eight agent generations used policy zero.

The first two Critic updates and one Actor update completed successfully. On
that training trajectory, Critic MSE changed from 0.169363 to 0.157223. Actual
Actor advantage mean was 0.258572, range `[-0.421793,0.812163]`. Actor gradient
norm was 0.0178223, all 9,318 tokens passed the strict DIS interval, and peak
native CUDA allocation was 12,482.47 MiB. Both Critic gradient norms were finite
(31.625 and 30.75, before clipping). These are in-sample diagnostics, not value
validation or coding performance gains.

The updated Actor adapter state digest was
`30b589b792fe3cb916f696ebd4a4272ff936d92f6e75a32cedf3363048750b46`.
Independent CPU reconstruction from that checkpoint, zeroing only its B matrices,
matched the recorded initial adapter digest exactly. The first step has zero A
gradient with zero B and zero weight decay, so this verifies the fresh SFT-equivalent
LoRA start without depending only on process arguments. SGLang loaded `policy-v1`
and resumed the pending episode without restarting its conversation or sandbox.
Native records showed eight base-policy agent generations followed by actual
`policy-v1` generations in the same episode.

The second episode also reached the iteration limit with official reward one.
All 97 agent generations / 12,215 action tokens were admitted, carrying behavior
versions `[0,1]`. Native context processing totals 1,734,587 tokens per learner
pass, maximum 29,665. The final two Critic updates and one Actor update also
completed successfully: Critic MSE changed from 0.183422 to 0.171491, Actor
gradient norm was 0.0142822, and all 12,215 action tokens passed the DIS interval.
Peak native CUDA allocation was 12,896.73 MiB. The final Actor state digest is
`79575aba202d7aea7aadb0cad3858b6e347af01abb4eace67c9f20d2b6fa5e5d`.

The original outer acceptance wrapper reached its three-hour work deadline
after both updates and three scored after evaluations. It interrupted the
fourth after episode, leaving it explicitly unscored. The native trainer
passed; the outer wrapper and pilot reports remain failed and were not rewritten.
Normal controller/stack retirement passed without cleanup errors.

On 2026-10-30, one fresh attempt replaced only that infrastructure-interrupted
fourth evaluation. It loaded the saved `policy-v2` adapter, started no trainer,
performed zero parameter updates and preserved the original task, Git baseline,
100-iteration/1,200-second episode limits, Thinking, sampling and condenser.
The retry's outer work budget separately allowed 5,400 seconds for startup,
evaluation and retirement. It scored one at the episode deadline: the current
patch passed the independent official verifier. Case elapsed time was 1,232.37
seconds, including setup/retirement/verification outside the agent time budget.

| Same-issue evaluation | Official rewards | Successes |
| --- | --- | --- |
| Frozen SFT before | `0,1,1,0` | 2/4 |
| Frozen policy-v2 after | `1,1,1,1` | 4/4 |

The first two after episodes reached the iteration limit; the third and retry
reached the episode deadline. Passing the verifier establishes patch correctness
for this issue, not efficient agent termination. These four repeated attempts
on the training issue are descriptive integration results, not held-out evidence
of general improvement or reproduction of the paper's reported performance.

The retry also distinguishes two latency sources: its 36 complete agent calls
generated 39,311 tokens in 496.84 seconds, while nine summaries generated 45,331
tokens in 602.99 seconds. One agent reply and one summary each exhausted the
32,768-token output budget, taking 410.98 and 419.52 seconds respectively. Long
agent generation and auxiliary summarization therefore require separate metrics;
not every long model call is a summary. No profile was changed during this retry.

The completion audit independently verified both optimizer-update groups, saved
targets/advantages and Actor weight chain, native token records and actual
policy-v2 LoRA selection, identical evaluation settings, and unchanged original
reports/checkpoint. `completed-evaluation.json` records the resumed result while
`training-independent-audit.json` explicitly preserves the original failed outer
run. The base model's full SHA256 was unchanged. All evaluation sandboxes and
API leases retired normally; the exact stopped retry etcd container and the
experiment's temporary user-service watchdog were removed. Docker monitoring
containers remained running. Raw evidence, including the interrupted attempt,
is retained with a per-file SHA256 archive manifest.
