<a id="单机-v01-安装与部署验收"></a>

# Single-host v0.1 installation and deployment acceptance

**English** · [简体中文](DSEC_V01_INSTALL_ACCEPTANCE.md)

<a id="2026-10-08模块化-r3-实机回归"></a>

## 2026-10-08: modular R3 regression on Linux

This run covers refactor baseline `775b5be` plus the persistence and busy-rejection fixes tested here. The historical r5 results below retain their original revision. Core wheel SHA-256: `63839e2000c72045ed662e4b66558b16e9ad4ea546cb496f694cf53701010f0e`. Tests imported the installed wheel in isolated Python mode from a dedicated venv; TB2.1 and MBPP were separately installed applications. The existing host and pinned external artifacts were reused. A second, freshly provisioned physical host remains untested.

| Check | Result | Raw evidence |
| --- | --- | --- |
| Linux release regressions | 323 checks: 322 passed, one skipped because the release omits an experiment-directory alias, no failures or errors | `installed-linux-busy.json` / `.log` |
| Non-TB lifecycle | Supervisor restart, same-VMM recovery, paused restart, action deduplication, score 1 and final retirement passed | `counter-recovery-fixed.json` |
| VM upgrade | One RUNNING and one PAUSED VM retained their original registry, identity, private files and action records. The new Edge adopted both leases | `live-old-to-new-fixed.json` |
| Container upgrade | The new Edge adopted a container created by the previously installed client and standalone agent. Container ID stayed unchanged, actions did not repeat and final leases reached zero | `container-old-to-new.json` |
| Container Edge restart | Real EROFS/OverlayFS with network=none. Container and lease IDs persisted, private writes survived and cleanup passed | `container-restart.json` |
| Shared SDK/worker admission | With the SDK occupying the CPU reservation, the worker waited on cpu_budget. Admission succeeded after release. No leases remained | `shared-admission-fixed.json` |
| Ready lease handoff | Same VM and node lease. CPU reservation changed from 0.05 to 1, memory estimate remained 512 MiB. Client pool hit took 45.68 ms | `ready-handoff.json` |
| Official TB2.1 verification | openssl-selfsigned-cert survived live/paused restarts and deduplicated actions. Official verifier score 1 | `tb21-openssl-final.json` |
| Prepared-state fork | Two successive episodes from one baseline each advanced the in-memory counter from 8 to 9, isolated private writes, recovered after source deletion and released CAS objects | `tb21-prepared-fork-fixed.json` |
| Fixed MBPP candidates | 64/64 expected outcomes. Correct and isolation cases scored 1, incorrect/timeout/early-exit cases scored 0. Peak actual node leases: 16 | `mbpp-execution.json` / `mbpp-receipts/` |
| Short native GRPO | Qwen3.5-2B + verl/SGLang, n=8, four steps. Observed nonzero gradient, model and optimizer checkpoints and per-sample evidence links | `rl-four-acceptance.json` |
| Real 3FS source | Two replicas ready. Published and verified a 48,201,728-byte EROFS object. Both VMMs held actual 3FS file handles. Write isolation and one pause/resume passed, with no final leases | `threefs-ready.json` / `threefs-erofs-acceptance-final.json` |
| Fixed-action cost across versions | Same host, read-only artifact cache and declared demand. Four fresh file-ext4 VMs per version completed create/execute and retired | `paired-revision.json` |

Median create wall time was 782.35 ms before and 792.02 ms after. Median fixed-action time was 47.80 ms before and 42.80 ms after. The small, fixed-order sample includes scheduling and RPC and cannot establish statistical significance or throughput gains. The 45.68 ms pool hit measures one checkout of an already running VM. It is not a cold start, fork restore, full episode or p95 under load. CPU and memory requests are scheduling reservations, distinct from utilization and measured RSS. MBPP requested concurrency 32, but the node budget admitted only 16 slots. The run therefore cannot claim 32 VMs executed simultaneously.

Short GRPO retained Non-Thinking, an 8192-token response cap, MLP LoRA rank/alpha 8/16, temperature 0.7, top_p 0.8, top_k 20, min_p 0, presence_penalty 1.5 and seed 42. Training scores per step were 8/16, 15/16, 8/16 and 0/16. Final small validation scored 8/16. Step 2 had mixed rewards and gradient norm 0.255859375. Zero or tiny gradients on other steps do not necessarily indicate task learning. Of 80 generations, 72 ran in sandboxes and eight earned explicit model-format zeros. Stops were 74 `stop` and six `length`; dynamic batch clip_ratio was not used for truncation. All executions were cleaned up. TITO, mask and logprob lengths agreed with immutable generation/receipt links. This tests native training after the refactor, not full MBPP accuracy or a measured training gain. An earlier one-step `rl-acceptance.json` had one all-1 group and one all-0 group, with zero gradient. That step alone cannot establish an effective update.

Two core defects emerged: concurrent budget publications reused one fixed `.tmp`, and transient lock contention permanently committed a busy error for the request ID. Publication now uses separate temporary inodes. A definitively unadmitted request cancels its journal intent. A new capability bounds same-ID SDK waits; UNKNOWN remains ineligible for replay. Readiness allows 120 seconds for large artifact integrity checks at startup. Execution deadlines were not relaxed to mask model or verifier issues.

Initial failures remain recorded: wrong smoke guest, admission wait in test orchestration, obsolete proxy address, insufficient readiness deadline, fork operation lock contention, and a test directory inheriting 0775 and failing permission preflight. Only the new test directory was corrected. Existing deployments still require operator review of directory modes; see [host configuration](../guides/DSEC_HOST_CONFIGURATION.md).

Evidence is at `dsec-r3-20261008/evidence/` on the host, mirrored at `.runtime/r3-20261008/remote-evidence/evidence/` in the outer workspace. Full short-training TITO and receipts are at `.runtime/r3-20261008/rl-four-evidence/`. Machine configuration, credentials, models and generated state stay outside public sources. Host Docker/Grafana did not restart. The SDK retired the dedicated test container.

R3 passed its planned functional checks. The 3FS test cloned an offline backup and left the original intact. The administrator restored the original `dsec3fs0` / `198.18.73.1/32`, Soft-RoCE and scoped mount propagation. After 123.85 seconds both cloned replicas were SERVING-UPTODATE and FUSE was ready. The old runtime image lacked jemalloc needed by the backup's metadata/storage binaries. `libjemalloc2 5.2.1-4ubuntu1` was extracted from signed Ubuntu APT packages into the test dependency directory only. All four primary programs passed dynamic-library checks, with versions/hashes archived. The backup binaries already depended on jemalloc; the refactor did not introduce it. Old container records showed no extra library path or library mount, but did not establish where jemalloc was originally installed. Production of an external service image must pin and verify its complete dynamic-library dependency closure inside the target image.

Both VMs read the same 3FS base EROFS object; the other three layers remained local. Host VMM file descriptors established the source directly. The initial sampler lacked the VMM group and could not read proc entries. Both samples succeeded under the already authorized kvm group. One VM preserved its private file over pause/resume. The next VM did not inherit it. No node leases remained. Full-hash publication warmed the object, so the test does not measure cold read volume or lazy-read speed, establish that all layers came from 3FS, or validate distributed storage behavior.

The records also retain initial issues in 3FS device identification, the previous dedicated NIC, missing libraries, multiline mount parsing and artifact-directory write permissions. These arose in external service recovery or test orchestration and did not change DSec's storage mechanisms. After acceptance the temporary storage service stopped normally. The administrator-prepared dedicated network device, private bind mount and test copy were retained for future checks.

The later r5 installation candidate passed 49 core regressions, real tool-disk integrity checks and 15 fixed-trajectory scores. All 43 final records were STOPPED. No leases, pending operations, netns, active device references or process references remained, with no host disruption. See [r5 report](DSEC_VERITY_PILOT_REPORT.en.md) for cold-resolution and cost data. Earlier functional and GRPO results below retain their original artifact identities. A subsequent independent r5 short GRPO, cross-episode fork isolation and restart test left 50 records stopped and no owned resources; see [latest training acceptance report](DSEC_V01_GRPO_ACCEPTANCE.en.md).

2026-10-06. Release entry-point acceptance completed. Full open-source delivery continues under [two-day plan](../../ROADMAP.md). This is functional acceptance; it measures neither throughput superiority nor a cost advantage over Docker.

Frozen development wheel `dsec_reproduce-0.1.0.dev0-py3-none-any.whl`, 183697 bytes, SHA-256 `53b0891380d0c57b4bca87adcdaa69eb5325773fdc6fe907abc8f1e864f2ce09`. Package-boundary checks passed for 51 Python modules, three plugin data files and release metadata. The package retains the Miles license. Core has no third-party Python runtime dependencies; model and training frameworks have separate dependencies.

<a id="验收与证据"></a>

## Acceptance and evidence

| Item | Physical result | Raw record |
| --- | --- | --- |
| Independent venv outside the source tree, source paths disabled | Installation and plugin imports passed. All 54 runtime files matched wheel bytes | `.runtime/release-v01-pilot/pilot-final-audit.json` |
| Non-TB task | Creation, action deduplication, pause/resume, score 1, stop and lease release passed | `.runtime/release-v01-pilot/acceptance-supervisor-r8.json` |
| Automatic supervision | Killing the isolated launcher triggered recovery, retaining the same worker and original VMM | Same record, `automatic_supervision` / `live_restart` |
| Both services restarted after pause | Original private file, PAUSED state and lease recovered. Grading and final release passed | Same record, `paused_restart` |
| Installed TB2.1 | openssl-selfsigned-cert, official tests/test.sh score 1 | `.runtime/release-v01-pilot/tb21-acceptance-r8.json` |
| TB2.1 restart of both services | Same VMM, committed action not replayed, official score 1 after pause/resume | `.runtime/release-v01-pilot/tb21-acceptance-restart-r7.json` |
| Official evidence | reward.txt, verifier.log, ctrf.json and manifest.json archived | `.runtime/release-v01-pilot/evidence/` |
| Frozen short GRPO | Two groups × two samples. Official scores `[0,1]` / `[0,0]`. Two training handoffs, four real snapshot restores, TITO and cleanup passed | `.runtime/release-v01-pilot/grpo-evidence-r8/installed-grpo-audit.json` |
| Installed prepared-state reuse | Two new episodes from one baseline. Official score 1, restart, recovery after source deletion and CAS reclamation passed | `.runtime/release-v01-pilot/installed-fork-r8.json` |
| Final cleanup audit | All 15 sandbox records across the two temporary instances STOPPED. No pending actions, leases or netns. Temporary services stopped | `.runtime/release-v01-pilot/post-grpo-fork-audit-r8.json` |

All 57 local regressions passed, covering task plugins, conversation, worker, sealing/fork, networking, release boundary and new deployment entry points. On Linux, isolated Python loaded the tests and imported only the installed wheel. All 31 budget, admission-rejection, queue-recovery, SDK and shared-fork regressions passed with no skips (`.runtime/release-v01-pilot/resource-tests-r8.json`). TB2.1 task revision: `7131e4375048a0e408a8fb404b5f499d726b695b`. The trajectories used fixed solution commands. They establish neither model accuracy nor a pass across all 89 tasks.

The installed TB2.1 instance used real EROFS layers, an OverlayBD + ublk private root, independent netns and the configured temporary Mac proxy. A new restricted helper launched ordinary Firecracker after checking its pinned digest. Address slots 4096–4097 were separate from existing instances. The existing ublk service remained installed as before. Temporary storage used a dedicated subdirectory under its authorized root.

<a id="本轮关闭的部署缺口"></a>

## Deployment gaps closed in this run

Task code moved into installable `dsec_adapters`. Older imports remain aliases. The worker's official verifier uses the installed package. Host configuration centralizes artifacts, scheduling and networking without relying on an experiment source directory. Services run Python in isolated mode.

The ublk service requires group traversal of its storage directory and a systemd write allowance. The control directory uses group-search-only mode 2710, and worker records stay private. chmod alone cannot bypass a service mount namespace. Existing TB2.1 guest artifacts require a virtual NIC at boot, so networkless configuration cannot deploy those artifacts. Both initial failures retain evidence and released their leases.

The old deployment pinned its verifier manifest via a separate environment variable. Omitting it from a new deployment compared against the old tool-disk digest. Daemon and worker now derive default and per-task verifier pins from the same host config, with a new regression. r4/r5/r6 records retain failures.

Start/stop checks support installed-module entry points and use the same group identity for pidfd verification. ExecStop explicitly stops the daemon. `Restart=always` handles sg child return codes. The worker uses a weak dependency to retain tasks and leases while the daemon recovers.

<a id="交付边界"></a>

## Delivery scope

This validated an independently installed instance on the experiment host, reusing existing models, images, kernel, ublk and read-only artifacts. Installation from zero on fresh Linux, all optional storage conditions and complete-backend Docker comparison remained separate gates. See the source delivery review below. Training-memory patches are confined to the explicit training-experiment package and are outside the DSec release.

<a id="冻结运行时短-grpo"></a>

## Short GRPO on the frozen runtime

Qwen3.5-4B Thinking, response cap 32768, two groups × two samples, concurrency two. It exited 0 in 474.759 seconds. All 54 runtime files byte-matched the frozen wheel. Training used its explicit experiment package, and tasks ran on the independently installed daemon and worker.

Group scores were `[0,1]` / `[0,0]`. Gradient norms were 0.256775 and 0. All 96 adapter tensors that differed between checkpoints were finite. The second group had identical rewards, providing no evidence of a meaningful second learning signal or improved model performance.

The four responses had 4603, 4967, 4449 and 4220 tokens. Logprobs and loss masks matched TITO. Each trajectory actually PAUSED after its first tool call, exited the original VMM and resumed in a new VMM at the next tool call, preserving sandbox identity and step continuity. The four private writable disks shared six EROFS source layers. This establishes independent episodes within a group and recovery within each episode. Cross-episode prepared-state fork needs its own test.

All three zero-score trajectories failed the official `test_python_verification_script` assertion in CTRF; one agent ended early due to `invalid_format`. Verifier grades were present. Both positive and negative cases retained official logs, raw replies, TITO, worker records, checkpoints and resource samples. Sampled GPU peak was 14406 MiB and training-container memory peak 20.525 GiB. Neither covers the complete DSec backend. All four records ended STOPPED with no pending operations or leases. The earlier installation cleanup audit predates GRPO.

<a id="安装版跨-episode-准备态复用"></a>

## Cross-episode reuse of installed prepared state

`tools/verify_installed_fork.py` passed using the same installed venv, shared host config, public SDK and task plugin. A two-slot instance used one sealed baseline and one fork, creating two episodes sequentially. This does not replace earlier four-fork concurrency measurements.

The baseline HTTP service held a real in-memory counter. Each fork began at 8 with new policy history. The first branch's private file and answer did not leak into the second. The first scored 1 officially. After sealing, both services restarted and could still fork. A running branch retained its VMM on restart and returned the same observation on retry of a committed action. Stopping the baseline while the second branch lived preserved the shared lower inode. Subsequent pause/resume preserved its private file and counter. All three rollouts ended STOPPED with zero leases and pending actions. Last-holder release deleted the shared object.

The final audit matched all 54 installed files to the frozen wheel. Across the two temporary instances all 15 sandbox records were stopped, instance netns empty and four temporary services down. Both original services remained healthy with empty queues. At idle the GPU sampled 35 MiB / 0%, and the root disk had about 97.6 GiB free.

<a id="mit-候选包"></a>

## MIT candidate package

The user selected MIT for project-owned code and documentation. The Miles derivative retains Apache-2.0. Wheel license expression: `MIT AND Apache-2.0`. The package includes both licenses and third-party provenance. New development candidate: 184823 bytes, SHA-256 `38fd9bb7602aad10b0bcbc816bf71e8d422624de41cb6d9659a8d0566f6f4a7e`.

License and README metadata changes left all 54 runtime files identical to the frozen functional wheel. On the experiment host a separate fresh venv verified isolated import, license metadata and all runtime files (`.runtime/release-v01-mit/acceptance.json`). The updated boundary check covered 51 modules and 62 entries and byte-checked license texts. Both runtime packages passed license-inclusion checks. Historical functional evidence retains its original wheel hash; training was not rerun for metadata changes.

<a id="从源码重建与可选应用安装验收"></a>

## Rebuilding from source and installing the optional application

Allowlisted source candidate r2 contained 96 files including manifest, 251944 bytes, SHA-256 `eba9161c2e4abfbbac7117795df66dec9dbb0ee44a1b1f3e949f4c6c57f06528`. `SOURCE_MANIFEST.json` pins every file digest. Rebuilding the archive reproduced identical bytes. It excludes task data, experiment results, images, model weights, secrets and GPU training patches. The experiment host built offline using setuptools 80.9.0 and wheel 0.45.1.

| Package | Bytes | SHA-256 |
| --- | ---: | --- |
| Core | 185063 | `dc86af4068679aca8ea5aa2abd591ee6cb21857d15e776f751e77257ad41e54a` |
| Optional TB2.1 application | 13460 | `f486c6c92e415f5033d9263eab28dd64cf90d39f5a592d3340bf87cb8dceefc4` |

A fresh venv installed only core first. The application was absent, and all 54 runtime files matched the source manifest. All 33 Linux core regressions passed with no skips. The archived C agent and pinned local image built a fresh 64 MiB non-TB guest. Execution scored 1, and supervision, action deduplication, running/paused recovery and lease release passed without reusing an old guest template.

The optional application was then explicitly installed and passed five installed-package regressions. It verified revision and hashes of all 89 tasks; this is not execution or scoring of all 89. Representative `openssl-selfsigned-cert` ran the same trajectory on the existing OverlayBD registry path and a newly converted six-layer EROFS/file-ext4 path. Both scored 1 officially and archived raw CTRF/reward/log. The new path took 1.463 s to create and 25.742 s to verify, a single functional timing. The installed OverlayBD prepared-state fork passed history, private-file and memory isolation across two episodes, restore after source deletion and final shared-object release. This run did not restart the TB service again. Earlier restart evidence belongs to the frozen build.

Starting the new artifact exposed inherited setgid on the parent. Following the recorded config-mismatch rejection, a file-ext4 sandbox directory inherited 2700 and was rejected by a launcher permitting only 700/2770. Creation now explicitly uses 700 for private directories and 2770 when OverlayBD requires it. Root helper and shared-directory permissions were not widened. This was the only runtime change relative to the frozen version (`sandbox_sdk.py`), with two new Linux permission regressions. Earlier GRPO was not rerun.

Final audit: one sandbox each for the two source instances and 19 for the shared TB instance, all STOPPED. Historical FAILED worker records persisted without pending work or leases. No instance netns remained; six temporary services were inactive, both original services active with empty queues. Root disk free was about 97.26 GiB. Original acceptance evidence, official logs and failed deployment records remain at `v01-pilot/source-release{,-r2}` on the host and `.runtime/release-source-acceptance/` locally. The delivery archive may include updated documentation and is pinned separately in its final manifest. Its core and application code must byte-match this tested source; document changes do not count as new runtime tests.

<a id="宿主隔离与权限修复候选-r3"></a>

## Host isolation and privilege fixes in candidate r3

On 2026-10-06, the new archive contained 98 files, 255752 bytes, SHA-256 `d4070cfd4cbe726f9e957b251169761dcc23a6556d393933a217cd809df21cf3`. The offline Linux core wheel SHA-256 was `ee370cb1ae15b736479eac53dc8e510f08b2e2d8ffdfc5237f0618283f1e9b6a`. A fresh venv passed all 40 core regressions and matched all 54 runtime files to the archive, then explicitly installed the original optional TB2.1 package. The guest template and existing task artifacts were reused.

Two core changes: check the root daemon's actual umask before creating OverlayBD roots and reject configurations that lose required group permissions; check host mount/net context in the network helper before touching state, locks or network. The upstream Rust protocol for creating missing runtime directories remained unchanged. Separately, the experiment launcher isolated both mount and net namespaces for private Docker. DSec services continued managing persistent guest netns handles on the host. The r1/r2/r3 failed-run entry points were retired while retaining evidence.

After administrator installation of the exact helper fix and removal of one confirmed stale empty handle, the SDK recovered and stopped the previously failed rollout. r4 tested both wrong helper contexts with the new package. A non-root client rejected the actual root ublk daemon's 0027 umask before RPC, device or runtime-directory allocation. Six Docker, six fresh DSec and three prepared-fork episodes each scored 1. Host service and original container PIDs, docker0 and Grafana health remained stable. All 33 private-instance records ended STOPPED with no lease, pending operation, owned netns, service socket or remaining device reference. All 26 installed experiment/protocol/permission regressions passed without skips.

Evidence: `.runtime/release-pair-r4/`. Complete cost report: `.runtime/release-pair-r4/report/REPORT.md`. This did not rerun short GRPO, grade all 89 tasks or cover all production failure modes. It did not establish an overall cost advantage over Docker. Full verifier-tool-disk hashing at startup brought substantial host file pages into memory. This was recorded separately from anonymous guest/VMM memory without changing accounting boundaries.

<a id="原生会话文件与流式-sdk-实机验收2026-10-10"></a>

## Native sessions, files and streaming SDK on Linux (2026-10-10)

The candidate extends `ccd61f0` with a Linux GCC path-buffer fix, deployment checks for native container socket path length and reusable acceptance entry point `tools/verify_native_sdk.py`. Both C agents compiled statically with `-Wall -Wextra -Werror`. The clean archive built a core wheel with 141 runtime modules and 149 entries. Final SHA-256 including socket validation: `f75011957988275b89923b9ffab191e2c2c2ea09c72335ee9d67e4146b3e1089`. A dedicated venv left older services, guests and user Docker networks intact.

A real offline MicroVM passed 10 checks, a four-layer EROFS Python MicroVM passed the same 10, and a layered container passed eight. Tests covered persistent cwd/env, isolated and overlapping sessions, a 160012-byte chunked binary file, private files across sandboxes, deduplication/conflict rejection, stdout/stderr streaming reconnect, precise cancellation, continuous-output limits and timeouts. MicroVMs rejected pause during execution before effects, and preserved idle-session cwd/env/files across memory snapshot restore. Containers lack memory snapshots.

Three additional recovery checks passed: idle Edge restart adopted the same VMM and shell; first native call after a paused restart resumed automatically; SIGKILL of Edge mid-execution yielded UNKNOWN, stopped the original VMM and rejected resubmission. A layered container retained its original container ID, shell cwd/env and files across Edge restart without repeating a write. A new Edge talking to an older Python guest explicitly returned UnsupportedCapability for native calls, then ran the legacy one-shot shell successfully.

Application regressions used existing one-shot commands. No trainer migration to native sessions was required.

- MBPP ran 32 fixed-code episodes. Correct, incorrect, timeout and early-exit scores matched expectations; all receipts persisted and final node leases were empty. This measures execution correctness, not model accuracy.
- TB2.1 `openssl-selfsigned-cert`, task revision `7131e4375048a0e408a8fb404b5f499d726b695b`: official `tests/test.sh` scored 1. CTRF, reward, testsh and verifier logs were retained. Verification took about 8.15 seconds. This run used a file-ext4 boot disk, the existing six EROFS layers and offline verifier disk; it did not test a new OverlayBD/3FS combination.
- Qwen3.5-2B + verl/SGLang ran four GRPO updates and post-training validation with Non-Thinking, LoRA rank 8/alpha 16, eight rollouts per prompt and an 8192-token response cap. The first two steps had equal within-group rewards and zero advantages/gradient. The next gradient norms were about 0.01263 and 0.00001287, demonstrating actual updates. All 64 training and 16 validation episodes have score receipts. Of these, 76 executed and stopped normally; four model-format zeros created no sandbox. Final mean validation reward was 0.5 on just two tasks. This neither establishes learning gains nor replaces the full MBPP evaluation.

The first TB setup reused a boot disk requiring eth0 without provisioning a NIC. PID 1 exited due to `ip: SIOCGIFFLAGS: No such device`. After preserving the failure, the existing `dsec_image.boot` produced a matching offline boot disk. Task layers and official tests were unchanged. Match every artifact's networking recipe to host configuration.

An additional container restart test exposed a 111-byte native.sock path. Capability discovery misreported the OS AF_UNIX path error as missing guest support. Opt-in native containers now validate a full path of at most 107 bytes before backend creation, with ASCII and UTF-8 boundary regressions. Legacy containers without native support are unaffected.

Final Linux regression ran 323 checks: 322 passed, one expected release-boundary skip. An earlier run found a shutdown race: SIGTERM could arrive between the stop check and blocking accept, leaving the native agent alive. The failure was preserved. Nonblocking accept with bounded poll fixed it; 12 real-process shutdown checks were added and the residual test process retired. Accepted sockets are explicitly restored to blocking mode because flag inheritance differs between macOS and Linux. All 18 macOS native regressions passed. Test processes are also cleaned up on setup failure. The corrected C agent passed the layered MicroVM, container and real crash recovery checks again. Application GRPO/TB checks predated the shutdown fix, which did not change their legacy one-shot command path. No full model benchmark was rerun. Evidence: `/home/xiaoxiaohu/dsec-native-20261010/evidence/`. Final audit found no owned VM, container or node lease. Temporary Edge/worker stopped. Existing TB2.1/MBPP services and five host containers including Docker/Grafana/Prometheus remained running. GPU use returned to about 1 MiB and disk free to 74.66 GiB. Training checkpoints and failures were retained. Single-host functional scope excludes all-task TB2.1, production multi-tenant security, concurrent performance, every storage combination and paper performance claims.

<a id="2026-10-25三条禁止联网执行路径"></a>

## 2026-10-25: three offline execution paths

This run exercised framework execution only, without SWE-Gym, inference or RL. FnCall used precreated Docker containers in QEMU/KVM. Container used Docker plus native aether/chronus in QEMU/KVM. MicroVM retained Firecracker. All public operations followed installed SDK → HTTPS API → IAM/Placement → mTLS Node gateway → private Edge. Host Docker did not substitute for QEMU execution.

Final installed wheel SHA-256: `e1b581cca785a473fb83d12063785730b8956021d9860476eddc47b7e5317e70`. Version remained `0.1.0.dev0`. Edge persistence contract increased to **2**. Automatic upgrade/rollback cannot cross contracts 1 and 2. Old ordinary Container request bytes and digests remain stable, but an old Edge cannot thereby adopt new state.

Tests used one Linux Node, dedicated single-member etcd container, separate unprivileged venv and run-specific API/gateway/Edge processes. At most two VMs ran concurrently; no GPU was used. The scope is real execution, request handling and reclamation, not repeated control-plane HA acceptance.

| Path or boundary | Observed behavior |
| --- | --- |
| FnCall | Calls reused a container ID without retaining the previous call's files or background processes. Fixed IDs deduplicated, and timeout cleanup allowed another independent call |
| Container | Workspace, shell state and binary files persisted. Real inner native agent, with no host Docker socket or KVM device exposed |
| Concurrent isolation | Two independent QEMU guests had distinct CIDs and could not see one another's workspace |
| MicroVM | Firecracker command and native session worked; files survived pause/snapshot/resume |
| Edge restart | Adopted the original QEMU/container ID. Completed writes did not repeat. Action evidence remained queryable after stop |
| Admission | Declaring only inner 512 MiB without outer 1024 MiB was rejected before VMM creation or physical lease |
| Failure evidence | After durable call intent, the run's VMM was verified by PID/start time/argv and killed. API returned UNKNOWN with command and reason, without replay. Evidence survived retirement |
| Cleanup | IAM usage, physical leases and CID counts returned to zero. Managed private QEMU disks were actually deleted; logs and request evidence remained |
| Host continuity | Five existing containers retained ID, PID, start time and network. Dedicated test service and etcd stopped |

Clean local source: **549 checks, 540 passed, nine skipped**. All **84 installed Linux checks** passed, importing from the venv. All 52 finite-model/expected-counterexample checks passed. Regressions caught three FnCall and 12 cluster negative mutations. Finite state and controlled checks do not establish automatic implementation refinement or unbounded liveness. Native concurrency traces have a separate report.

One cold FnCall/Container creation took about 4.1–4.3 seconds including template copying and guest Docker preparation. A single timing does not characterize a distribution, warm calls or a 60 ms startup. The new QEMU path used private ext4 and inner VFS. It did not establish EROFS/OverlayBD sharing. Existing MicroVM EROFS/DAX/OverlayBD/3FS mechanisms were unchanged and not all retested together.

Real tests prompted fixes for duplicate guest `/dev` mounting, default userland-proxy configuration for an offline guest, containerd versus classic Docker image-ID representation, handling a new token with an old terminal CID record, quota release after proved create failure, empty response mapping for UNKNOWN, and waiting for actual disk deletion before confirming reclamation. Between phases the acceptance tool waits read-only for Watcher capacity refresh without retrying unknown creates. One test erroneously limited loopback control traffic to 10 Mbps and correctly triggered `network_saturation`. The test budget was corrected with the resource guard intact.

An earlier pre-console `RuntimeError` lacked a recorded message. **Its root cause remains unknown.** Stop and CID release were proved. Startup error type/message and uncertain-action arguments/reason are now durable. The final run did not reproduce it; the original failure remains recorded.

Final raw host evidence: `/home/xiaoxiaohu/de25/api-r7/`. Local copy: `.runtime/execution-paths-20261025/linux-final/`, alongside regression, model and mutation reports. Thirteen stopped prototype raw-disk copies were cleaned up after allowlist and ownership checks. Digests, logs, state and two final templates remain. Full production isolation, host-user vsock authorization, egress policy, Container memory offload, QEMU shared storage, further topologies and scaling are separate roadmap gates.


<a id="2026-10-25qemu-guest-通道认证补强"></a>

## 2026-10-25: QEMU guest channel authentication

Host CID alone does not identify Edge. Container/FnCall now require a random per-VM key, random challenge and mutual HMAC bound to direction and port. Both control port 5002 and native session/stream port 5001 authenticate before execution. A private virtio-serial channel injects the key once, outside guest files and task containers. Restarted Edge uses its retained private key without reinjection or action replay. Root and Edge accounts remain trusted. Connection flooding and full production isolation were not tested.

Installed wheel SHA-256: `96c4cb96830c3abb1635c2db5c1968f3104cf3926d05393b66fea2dfa5988350`. Edge contract remains 2; QEMU catalog/descriptor now use schema 2. Old catalogs and unauthenticated guests cannot downgrade. Drain old instances using the old version, rebuild templates and enable the new catalog. Editing only the schema number is insufficient. Firecracker and independent development QEMU primitive retain their old transports.

All three real paths passed the unified HTTPS API on the same Linux Node. **22 checks** covered wrong VM keys on both ports, cross-port proof rejection, old challenge rejection, bare protocol rejection on both ports, authenticated control and streaming, absence of bootstrap devices inside tasks, and no secrets in argv/descriptors/console/VMM logs. Edge restart adopted the same VM/container and action evidence. UNKNOWN after failure was not replayed. IAM, physical leases and CID returned to zero; private disks were removed. Five host containers retained IDs/PIDs/start times/networks. Test API/gateway/Edge and dedicated single-member etcd stopped. No GPU work or additional HA/paper performance evidence.

Clean source: **561 checks, 552 passed, nine skipped**. All **86 installed Linux checks** passed. All 56 finite-model/required-counterexample checks passed. Security guard mutations increased from five to **eight** and all were detected. Three existing negative FnCall mutations remained covered. The model abstracts HMAC as an unforgeable bound proof. It does not prove cryptographic security, random quality or complete runtime refinement.

The handshake enforces a total absolute two-second deadline. Sending one byte at a time cannot extend it. A deterministic clock test verifies this boundary.

The first installed run named a nonexistent test module. Separately, inconsistent identity sampling of a sleep fixture prevented reaching the intended wrong-argv branch. Both failures were retained. The test list was corrected and the child now explicitly signals readiness before identity checks. Runtime pidfd/UID/exe/argv guards were not weakened. The earlier unidentified pre-console RuntimeError remains unresolved.

Raw host evidence: `/home/xiaoxiaohu/de25/api-auth-r2/`. Local non-sensitive reports: `.runtime/guest-auth-20261025/linux-final/`. Templates, per-instance keys and API credentials remain private on the host, outside the repository and public evidence package. Production isolation, egress, Container memory offload and QEMU shared images remain separate roadmap checks.


<a id="2026-10-25qemu-宿主隔离与无外网三路径实机验收通过"></a>

## 2026-10-25: QEMU host isolation and three offline execution paths on Linux

Opt-in `host_isolation` adds per-VMM cgroups, unprivileged exec gated by commit/claim, a per-instance AppArmor broker and kernel-state validation on restart. No in-thread `preexec_fn`, no attaching resource limits after QEMU executes, and no treating config as proof of enforcement. A 1024 MiB guest with 256 MiB VMM allowance requires 1280 MiB in Node/IAM admission and a 1280 MiB cgroup hard limit. Older preview deployments without this field explicitly report no host isolation. The production flag remains false.

A dedicated delegated user service tested three real kernel behaviors: OOM kill in a 48 MiB child group, pids-limit denial of an additional fork and CPU quota throttling a busy loop. All three groups were removed only once empty and the service exited normally. No VM, GPU, host Docker, existing service, network, module or other cgroup was touched. This alone does not prove QEMU runs under AppArmor. Policy text separately passed offline `apparmor_parser -Q -K` parsing.

Full clean-source suite: **576 checks, 567 passed, nine skipped**. All **101 installed-wheel Linux regressions** passed. All 61 finite-model/required-counterexample groups passed. Controlled regressions caught three new host-isolation source mutations and retained coverage of eight authentication/rotation guard mutations. Tested cases: cannot unload policy while a claimed process lives; retirement marks before unload and rejects late claim; missing proc details cannot imply death while pidfd lives; a real child whose commit fails cannot exec; cgroup mismatch and cleanup failure cannot return success. These are bounded checks, not a proof of unbounded state or automatic refinement of the entire runtime.

Candidate wheel SHA-256: `b3684e687bfa617cdfcdf6824a0bb87ceac7bc1d2bf7f532ac149729a38417c2`. Administrator bundle at `/home/xiaoxiaohu/dqh25/privileges-r3/` on the host pins the instance, UID, binary and launcher digest. It installs only helper, config and sudoers, without starting services. Installer SHA-256: `a7455524975f758c7ea787375ca0c6d3b018dd66104f72b41cd823aea425dcfb`. The administrator installed it and verified ownership/digests. Two physical checks initially failed; fixes are below. Loading kernel AppArmor policy requires administrator access unavailable through passwordless SSH. Once installed, ordinary users may invoke only the fixed-instance broker.

First bundle generation exposed a character-validation problem with the standard `user@UID.service` path, now covered by a configuration regression. The generator regression corrected replacement of network_required when reusing administrator preflight, avoiding unrelated network permission requirements. Failure evidence remains; the defective administrator package was not executed. Local evidence: `.runtime/qemu-host-20261025/`. Linux controller report: `/home/xiaoxiaohu/de25/host-cgroup-kernel.json`. Installation regression: `/home/xiaoxiaohu/dqh25/installed-unit-final.log`.

Both initial API runs were rejected before the first FnCall guest booted. The first `VMM privilege/seccomp state mismatch` exposed a race validating seccomp immediately after exec. Full validation now occurs after QMP/KVM readiness and before `cont`. Diagnostic kernel values are retained, but exact first-run field values cannot be inferred. The second `vmm.log` explicitly showed `Failed to create Unix socket: Permission denied`. Rule `deny unix addr="@**"` also rejected address-independent socket creation. The revised policy permits only stream creation, anonymous internal communication under the same profile and the two existing exact pathname socket permissions. Abstract bind/connect remain default-denied. The kernel must support fine-grained AF_UNIX mediation. The unnecessary `attach_disconnected` development flag was removed.

Corrected source: **576 checks, 567 passed, nine skipped**. **101 installed Linux checks passed**, plus three guard mutations and offline Linux `apparmor_parser -Q -K` parsing. Simulated updater checks passed initial replacement, idempotent rerun, rejection of active records and rejection of unknown prior versions. It backs up and replaces only the pinned helper, leaving config/sudoers intact. After the second failure the dedicated cgroup was gone and CID leases zero. Both runs preserved IDs/PIDs/start times/networks of five host containers. Failures stayed at `/home/xiaoxiaohu/dqh25/api-r1/` and `api-r2/` and did not count as successful acceptance.

Corrected wheel SHA-256: `db67bfbad21b725d5614e126e1eef9bd2a7c23b7ca7ab06c3741c1858e921fe8`. Pinned updater `/home/xiaoxiaohu/dqh25/update_profile.py` SHA-256: `b7bc26adec007e71d2571044cd7a635ea159884eb6d0988e2bcf17b972bc88b3`. After administrator update, the root helper's ownership, non-writability and digest `defa8939ba2979bbd70511864b18b7dd6e869363375f8c4e122a36ed956716fe` were verified again. The corrected wheel then passed physical acceptance.

- All **seven SDK checks** passed through HTTPS API → IAM/Placement → mTLS Node → Edge. Real QEMU/KVM FnCall cleared state on reuse, deduplicated requests and remained usable after timeout. Container preserved sessions and files, with two VMs using independent CIDs and working directories. Firecracker MicroVM passed shell/native/pause/resume.
- All **24 orchestration checks** passed: kernel-enforced profile, private cgroup limits, NoNewPrivs/seccomp and zero effective/permitted capabilities; AppArmor denied host private files outside the instance; both ports rejected wrong keys, cross-port proofs, replay and bare protocols, and accepted authenticated streaming.
- Edge restart adopted the same QEMU/inner container, skipped committed commands on retry and retained queryable evidence after stop. Killing a VM preserved UNKNOWN, the command and reason after retirement. Declaring only inner memory was rejected before VMM creation/physical lease.
- Six QEMU instances retired normally or completed fault recovery. Recalling the restricted broker for each confirmed RETIRED and no loaded profile. CID, physical leases, IAM concurrency and private writable disks reached zero. The delegated cgroup was removed without cleanup errors. The original five host containers retained ID, PID, start time and network.

The systemd service ran about **67 seconds**, including launching a test control plane, running every check and reclaiming resources. This is not single-VM startup latency. Service-level memory totals do not identify VM memory. Host reports: `/home/xiaoxiaohu/dqh25/api-r3/report.json` and `sdk-report.json`; local copies: `.runtime/qemu-host-20261025/linux/api-r3-report.json` and `api-r3-sdk-report.json`. Credentials, instance keys and templates remain outside the repository.

**This closes the opt-in offline QEMU/FnCall/Container host-execution and reclamation check.** `production` remains false. QEMU egress, connection flooding, adversarial tenancy, shared images, Container memory offload and production isolation across other backends require separate evidence.

<a id="2026-10-25qemu-独立网络完成有限内核与联网验收"></a>

## 2026-10-25: bounded kernel and external-network checks for QEMU isolation

Optional `network=isolated` adds private netns/TAP/veth, one-time FD handoff, a pinned TC eBPF object, guest IPv4 setup and retirement. QEMU stays behind its gated unprivileged launcher; there is no root VMM launcher. Control/native traffic remains on authenticated vsock. Existing offline catalogs/templates need no change. The root helper accepts only a fixed SID and launcher identity, never a command, host path, program ID or alternative BPF object. Beyond public IPv4 TCP/UDP, only pinned DNS and an optional TCP proxy exception are allowed. IPv6, ICMP, hot reload, shaping and flood resistance remain unverified.

Clean source: **594 checks: 585 passed, nine skipped**. All **119** installed-wheel Linux regressions passed. All 66 bounded model/expected counterexample groups passed, including the new network model's normal path and four failure classes. Controlled regressions caught **eight** host/network source mutations, including a child failing to inherit the lock. Killing an actual wrapper verified that its unfinished fixed subcommand retained the FD lock. Packet tests executed the same C predicate as TC, covering public/DNS/proxy, spoofed source MAC/IP, private networks and metadata, ARP, IPv6/VLAN, fragments and truncation, without testing a separate Python reimplementation.

Clang was missing on the host. Three distribution compiler packages were unpacked into the private build directory without installing host packages. The eBPF ELF compiled; AppArmor offline parsing and sudoers syntax passed. Networked FnCall/Container templates received separate pinned digests; old templates were untouched. The first build mistakenly used `python -I`, hiding the tool's sibling import. Invocation was fixed; failure logs remain and no host task container was started.

Candidate wheel SHA-256: `5c84b7b5b69fa597df65355f33eb2cbb2a7b42aca337008dd2d9c3b1f827ad94`. Dedicated qnet privilege package `/home/xiaoxiaohu/dqn25/privileges-r2/` pins six root files, UID 1000, wlo1 and two /31 slots inside `10.232.8.0/30`. Installation creates policy/privilege files and two private state directories without creating networks or restarting services. Installer SHA-256: `5d89f6036bdc3c6ffb86d3d6b400e1bca6a1aa2e9e05c4106b32a2a3dc681770`. Source, digests at both ends and the root target allowlist were checked. Local candidate/evidence: `.runtime/qemu-network-20261025/`; Linux install regressions: `/home/xiaoxiaohu/dqn25/installed-unit-r2.log`.

After administrator installation, ownership, permissions and hashes of all six root files were checked. Real Linux networking had not yet passed. Planned tests used regular HTTPS API/SDK and at most two small VMs: positive/negative packets through the attached program, FnCall/Container DNS and HTTPS, denial of host/metadata/private networks, Edge takeover, resource cleanup and Docker/Grafana continuity. Native C, offline parsing, models and compilation alone do not prove kernel verifier acceptance or production egress isolation.

The first SDK run failed before QEMU started. Real `tc -j` returned a heading and program line for one filter; the code counted two programs. Also `ip link add ... alias` had not set the host veth ownership tag, blocking strict cleanup. A second fixed SID reproduced the TC failure. Both residual networks stayed DOWN, never reached link activation and started no guest. Five original containers kept IDs/PIDs/start times/networks. Host failure report: `dqn25/api-net-r1/`.

The fix accepts exactly one program line verified by ID/tag/direct-action, rejects extra programs or foreign filters, and explicitly writes and verifies the veth tag after creation. Startup and cleanup failures are recorded separately. Interrupted creation can recover without a returned create response, and the same Edge can retry STOPPING cleanup. Unproven reclamation retains leases. Clean source **599 checks: 590 passed, nine skipped**; installed Linux **124 passed** (92 execution/isolation/auth/network, 32 native concurrency/CID). Eight host/network mutations passed. Unchanged finite models were not rerun. Wheel SHA-256: `fb03f5c27db400d4c0b861eafddf981efb105cfd5156044ae78f5244cdcca263`.

The administrator repair replaced only the network helper. It retired two residual networks after checking pinned SID, slot, namespace inode, veth ifindex/MAC, ownership, DOWN state, absence of addresses and processes. The other five root files and BPF object remained unchanged. The repair identity gate passed one valid snapshot and ten rejection branches, digest `7218e9265ae1b4d514ef72bdf9dadb4e39e035d1d436632d3bad40ddbf13a2dc`. The administrator executed it and both networks reached RETIRED. Recovery through the original API/Node/Edge passed **12 checks**: no replay or guest start, physical lease/CID/disk/IAM all released, interrupted/failure evidence retained, five host containers unchanged. Host report `dqn25/api-net-r1/recovery-r4/report.json`; non-secret local copy `.runtime/qemu-network-20261025/linux-recovery-r4/`.

Repair diagnostics revealed a second TC JSON difference: kernel program identity was nested at `options.prog` while the implementation read `options.id/tag`. A regression using the observed JSON now accepts nested or flat identity, rejects both together, missing identity or non-direct-action. Actual kernel packet diagnostics still failed at this stage; retirement alone did not validate the policy.

Latest candidate: clean source **600 checks: 591 passed, nine skipped**, installed Linux **125 passed**, eight guard mutations passed. Wheel SHA-256 `64eca4870885eced542502c9e28668397e1a6e5172a2a912f483c0d12109441a`. Single-file administrator update `dqn25/update_tc_program.py`, SHA-256 `715c750a295222cc74b22c9b427e042af96ba7bef61de7e363e14c2d30fa2208`, passed simulated replacement, idempotence and four reject cases; it changes only the pinned helper, without network actions or changes to the other five files. The administrator applied it and verified its digest. Unprivileged broker preflight then read the TC identity successfully, but the kernel rejected the gateway at `default via host_ip dev vpeer`. The same broker retired SID `b20462d1292648d2bc51ce8caa65d36e` cleanly before QEMU started. Report: `dqn25/kernel-policy-r5.json`.

A 512 MiB Firecracker MicroVM without host network devices was launched through HTTPS. Inside it, the default route returned Network unreachable while the veth was DOWN, then succeeded with the same address/gateway after link-up. Five orchestration/cleanup checks passed, physical and IAM leases released and host containers remained stable. Evidence `dqn25/api-route-r1/report.json`. Default route setup now follows link activation and precedes READY; TC and both firewalls still precede any link-up. A controlled ordering check requires routing after both peer and host link-up. **14 network regressions passed**, as did simulated administrator replacement/idempotence/four rejections. Fixed-subcommand diagnostics retain command and stderr. Installed Linux passed all **125** key checks and eight guard mutations. The wheel SHA-256 was `7e4ebd8fca64b46e4aac2f73aab0e24ff7df06d0467d7c904c76a40301a651dc`. Only affected local network and Linux key suites were rerun; full source/model suites were not.

Single-file repair `dqn25/update_network_route.py` SHA-256 `23ccc58bd1d4cca79f7934a1a175075f0bb8bf6395bcfab9a3432556f6c922e9`. The administrator applied and verified it. A separate temporary network reached READY, passed eight positive/negative packet classes on the actual attached kernel program and reached RETIRED. Host report `dqn25/kernel-policy-r6.json`. Guest route reproduction, cleanup and candidate regressions alone do not validate host egress. The real egress result follows.

The first full SDK rerun showed QEMU rejects `vnet_hdr=off` when inheriting `fd=`. Error and VMM log persisted; failed creation cleaned up. Removing the conflicting argument exposed strict argv validation in historical failed records blocking Edge startup. Compatibility now permits only that known former argument in STOPPED/FAILED records with no launcher/VMM identity and proven retired host policy/network. Active instances and any other argv difference remain rejected. No history rewrite, failure deletion or administrator file change.

Final wheel SHA-256: `8c81dc7df31355f8d59665daeb1a99a65f523d86737a1a00a2a01d6a73028fb6`. Clean source **602 checks: 593 passed, nine skipped**. Eight host/network guard mutations passed; Linux installed-wheel key suite **127 passed**. Finite models were unchanged, so TLC traversal was not repeated.

Unprivileged user, installed wheel, normal HTTPS API → IAM/Placement → mTLS Node → Edge:

- **Seven SDK checks passed.** Real QEMU/KVM FnCall cleaned state on reuse, deduplicated and remained usable after timeout. Container kept shell/files across calls with two independent guests/CIDs. Firecracker passed shell/native/pause/resume. At most two VMs ran simultaneously, without GPU.
- **29 orchestration checks passed:** actual netns/TAP/TC identity, eight packet classes through the attached program, Container DNS/public HTTPS 200 and 128 bytes read, denial of host SSH/metadata/private connections, FnCall DNS, kernel confinement proof and rejection of outside-instance files, authentication and streaming, same guest/container takeover after Edge restart without replay, and durable UNKNOWN/command/reason after VMM death.
- Physical leases, CIDs, private writable disks and IAM concurrency all reached zero with no cleanup errors. Additional kernel audit covered nine past/current QEMU instances (two old failures, seven successful creations). Policies and networks RETIRED, no corresponding interface/netns or dedicated cgroup. Five original host containers kept IDs/PIDs/start times/networks. The service ran **94.551 seconds**, encompassing far more than one VM creation.

Host reports: `dqn25/api-net-r4/report.json`, `sdk-report.json` and `kernel-retirement-audit.json`. Non-secret local copies: `.runtime/qemu-network-20261025/linux-final/`. Failed runs `api-net-r1` through `api-net-r3` remain excluded from successful samples. Keys, guest credentials and templates stayed outside the repository.

**This closes bounded opt-in IPv4 checks for QEMU/FnCall/Container on one prepared Node.** Actual policy used pinned DNS `192.168.0.1` and no proxy. Real proxy exceptions, IPv6, ICMP, hot policy changes, bandwidth shaping, floods, hostile tenants and scale remain unverified. `production` remains false.


<a id="2026-10-26qemu-erofsvirtio-pmemdax-文件共享"></a>

## 2026-10-26: sharing EROFS files via QEMU virtio-pmem/DAX

This validates optional local read-only layer sharing in QEMU Container/FnCall, without claiming host-wide memory advantage, cold startup speed, training or production scale. Normal HTTPS SDK → API/IAM/Placement → mTLS Node → Edge, independent real etcd, no GPU. Two VMs ran concurrently, followed by one FnCall. Guest RAM stayed at 1024 MiB, VMM cgroup cap at 1280 MiB and inner limit at 512 MiB; no relaxed memory or isolation constraints.

Two uncompressed EROFS layers, without inline regular-file data, aligned to 128 MiB, held 32 MiB fixed data plus an overlaid file. Published Container/FnCall templates were built with the open-source `tools/build_docker_guest.py` from pinned local Python/Docker-tools images and the same `readonly_layers`/`shared_mounts`. Host read-only shared ROM mapped the original inode. The guest mounted `ro,dax=always`, checked `STATX_ATTR_DAX` on the actual file, then composed OverlayFS with a private ext4 upper. Tasks could access neither pmem devices nor guest management directories.

Final orchestration passed **32 checks**, including the following.

- Independent VM PIDs/CIDs, actual DAX, layer identities and order.
- While all 8192 data pages were concurrently accessed, both VMM base mappings showed `r--s` with identical device/inode. Per VM: `RSS=32836 KiB`, `PSS=16418 KiB`, `Shared_Clean=32836 KiB`, `Private_Dirty=0`. Aggregate mapping PSS was **32836 KiB, about 32.07 MiB**. Summing RSS would count shared pages twice.
- Container edits copied up privately. Creating a file or deleting it via whiteout had no effect on the other VM.
- FnCall read the DAX directory; writing returned `Read-only file system`. Successive calls cleared markers in `/work` and `/tmp` while keeping the same VMM/container identity. Final stop succeeded.
- All three VMs had `memory.events` set to `oom=0, oom_kill=0`. Kernel cgroup peaks were `1072611328`, `1070272512` and `1069010944` bytes. These measure the entire VMM cgroup; the PSS above covers only the shared file mapping.
- Normal API retirement released all physical, IAM and CID leases, removed private disks/cgroups, preserved artifact digests and proved host/network retirement. Five original containers kept IDs/PIDs/start times/networks, and the original network-helper digest remained unchanged.

Core wheel SHA-256 `1beffe47aa4d97bdc2c77966866dff4f105b28a95a0e7aacb7705b46f44b6691`. Pinned launcher digest remained `80177075f4a3823175240999ba172c391bbc82a4bad912c362672e8d07b43bc3`. Clean local export **623 checks: 614 passed, nine platform skips**; targeted Linux **84 passed**. Eight existing host/network guard mutations passed locally and on Linux. Administrator update passed four simulated checks, replacing only the fixed-instance AppArmor helper/config and adding a fixed publication root. It left sudoers, network and services unchanged. Each VM writes exact layer identities to the existing policy ledger, with no wildcard directory-write grant. Publishing more layers within the root requires no helper reinstall. Models were unchanged and TLC was not rerun.

Host evidence: `dsec-pmem-20261026/api-r3/report.json`, `final-audit.json`, `service-r3.log` and raw per-instance kernel counters collected before retirement. Non-secret local copies: `.runtime/swegym-20261026/pmem-evidence/`. Two earlier script failures remain: r1 nested Python string escaping; r2 completed sharing/isolation checks but mistook a public handle for a physical directory name during aggregation. Neither counts as final success. r3 reran from the formal templates and exited normally.

Service wall time of 140.993 seconds includes control-plane startup, historical instance recovery checks, three creations and a controlled 20-second mapping hold, rather than a VM startup timing. Historical recovery repeatedly full-hashes old templates, and runtime SHA-256 of new layers also reads their content. Cold on-demand read and startup performance are not proved. Reuse existing fs-verity publication receipts for cold-read testing without weakening integrity.

Only the measured shared file's data pages avoided duplicate guest caches. **This neither reduces the whole VM to 32 MiB nor demonstrates a 50% reduction in total host memory.** At this point SWE-Gym repositories/dependencies had not migrated to this path. Earlier MBPP GRPO and SWE-Gym results retain their historical storage configurations. Compression, OCI whiteout publication, scale and cross-node loads, direct 3FS DAX, application total-memory savings and a Docker advantage require separate tests.

<a id="generality-linux-20261030"></a>

<a id="2026-10-30通用环境能力与版本绑定-linux-验收"></a>

## 2026-10-30: environment capabilities and deployed revision binding on Linux

Direct SDK creation exercised the environment capability contract without models, training or GPU. The candidate was temporarily installed in an existing test venv, using isolated Python, public HTTPS API → IAM/Placement → mTLS Node → private Edge → real guest. Independent etcd used an existing image and host networking, listening only on two fixed loopback test ports. No private dockerd, Docker bridge, new administrator rights or root helper change. The pinned AppArmor launcher digest and existing QEMU cgroup/AppArmor confinement were preserved.

Final core wheel SHA-256 `83fdb51b08aff638ab5fdb47fd6247d1f8afee7c1ea7718a0d29fe6da556a84a`. Boundary checks passed for 214 runtime modules and 225 entries.

| Check | Result |
| --- | --- |
| Physical execution | All 27 checks passed without cleanup errors. At most one workload guest ran concurrently |
| Targeted installed Linux regressions | 77 passed, zero skipped/failed/errors across environment, API, cluster, Container Edge and publication |
| MicroVM/Container basic capabilities | Created against environment-specific claims. Binary file round-trip, persistent shell, stdout/stderr/result streams and reconnect to original session/stream; effects occurred once |
| Mode and compatibility | MicroVM retained files through pause/resume. Two FnCall invocations did not share private working files. Legacy guest lacking claims returned NoPlacement with requirements, but ran with an empty requirement set |
| API admitted, Node not dispatched | File barrier paused request while test Edge's pinned revision was replaced. After release: AdmissionDenied, Edge NOT_FOUND, IAM RELEASED |
| Node checked, Edge not yet creating | Barrier immediately before actual RPC send. Replacement Edge rejected the original binding; no create intent, IAM RELEASED |
| Original proof and teardown | Original DONE MicroVM/Container create proofs remained queryable after revision replacement with no second quota charge. All Edge leases and five IAM usage counters zero |
| Host impact | Five container IDs/PIDs/start times/networks, docker0 address and default route matched baseline. Test API/Node/Edge/etcd stopped |

Fixed missing `resource_cleanup_complete=true` in `ContainerRuntime` responses for proven-stopped status, repeated stop and restored stop results. Complete reclamation requires successful backend `prove_stopped`; invalid proof returns an error. Added a real local RPC regression. All 87 local Container/QEMU/cluster checks passed, and 14 removed-guard cluster controls failed as expected. EnvironmentBinding model was unchanged and TLC was not repeated.

Host report `/home/xiaoxiaohu/dsec-generality-20261030/r5/report.json`; installed regressions `tests-r2/regressions.json` / `.log`; final archive/recovery evidence `post-cleanup.json`, `install-after.json`. Non-secret local copy `.runtime/system-generality-20261030/linux-final/`. Keys and guest credentials were not copied. Failed rounds are retained and excluded: directly modifying the old environment claim correctly hit the historical identity guard. The approach was changed to retain that environment and explicitly pin catalog history before adding a new one. Two runs corrected TIME_WAIT port misclassification and API readiness waits. The missing result field was then detected and fixed. Historical identity, capability, isolation and cleanup checks were not relaxed.

Three retired QEMU records and seven lifecycle logs were separately archived only after verifying STOPPED state, no processes/CIDs, removed private disks and reclaimed cgroups. Eight historical instances remained unchanged. The borrowed venv was restored to its original wheel and verified file by file against Python sources. Disk free was about 31 GiB. No unrelated data was cleaned.

Scope: tested native/stream primitives, four normal creations and two deterministic revision-change windows. This does not establish truth of every guest claim, session files/HTTP, cross-host HA, 3FS/ublk, pmem/DAX speed, network isolation or RL outcomes. Existing finite models, earlier confinement checks and this physical execution run remain distinct evidence.

<a id="2026-10-30task-007-正式-api-首次使用路径本地阶段通过node-阶段受阻"></a>

## 2026-10-30: TASK-007 first use through the public API (local stage passed, Node stage blocked)

The goal was a new user's first public API workload from published source artifacts. It reused the existing generator, API/IAM/Node and fixed-response Miles-compatible counter check, without adding runtime protocols or reimplementing recovery. TASK-002 and other combinations remained candidates. The local stage did not count as final Node acceptance.

The clean allowlisted export contained 521 digest-protected files plus its manifest. Archive SHA-256 `10bdeb0472723a7df4f07103c87947aa5e16fa5832914aaf0c14283617eeb9fd`, core wheel built from it SHA-256 `f522aed875efe0b6d99c3a3876634381cf68dc86ce37f6932a551dc12338618a`. An independent venv installed offline and imported from site-packages, without TB2.1/MBPP/SWE-Gym. Wheel boundary: 214 runtime modules and 225 entries. The environment could load the API verifier's help. The deployment generator ran under ordinary Python as documented; runtime and single-file verifier could use isolated mode.

A private deployment generated from public `deploy/cluster/prepare.py --plan` used pinned cached Python and etcd images. It built through the published Compose file, started three etcd members, initialized authentication/IAM and started two API replicas. No template changes bypassed TLS, authentication or dynamic membership. The formal HTTPS SDK checked both replicas. This quorum ran on one Docker host, outside independent physical HA scope. Python image `python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`; etcd `quay.io/coreos/etcd@sha256:3397341272b9e0a6f44d7e3fc7c321c6efe6cbe82ce866b9b01d0c704bfc5bf3`.

The generated configuration enabled `node_lifecycle`, but its initializer created only IAM/quota. The main deployment instructions omitted the initial Node join/activate step, previously buried in maintenance docs. The guide now shows join → activate in the main flow, and Quickstart runs the existing default non-TB fixed-response workload via `verify_rollout_api.py`. Real join returned JOINING, version 1, held 0. Create against the inactive Node returned NoPlacement as designed. Admission was not modified or misclassified as a create fault.

The installed core passed **35** local deployment/register/export regressions. All 521 digests in the updated archive were checked and the runtime byte-matched the wheel above. The following isolated-install regressions did not import runtime modules from the worktree.

| Scope | Total | Passed | Skipped |
| --- | --- | --- | --- |
| Portable core | 680 | 670 | 10 |
| Optional TB2.1 application | 5 | 5 | 0 |
| Optional MBPP application | 15 | 14 | 1 |
| Optional SWE-Gym application | 73 | 51 | 22 |
| OpenHands adapter | 54 | 26 | 28 |

Core/MBPP skips required Linux processes, permissions or guests. SWE-Gym/OpenHands also required optional upstream SDK, official harness or PyTorch/verl dependencies missing in this environment. Skips do not count as passing real guests, full upstream integration or training. The initial OpenHands test environment omitted CI's `pexpect`, giving nine import errors. Adding `pexpect==4.9.0` and `ptyprocess==0.7.0` made the same suite pass. Initial failure logs remain; product dependencies did not change.

All four optional packages built from clean sources, passed their wheel boundary checks and installed separately using `--no-index --no-deps`. Core alone installed none of them. Artifact SHA-256 values follow.

| Optional package | SHA-256 |
| --- | --- |
| dsec-tb21-case | `372c669e8d5f2e9ea9fb97d168dff069befe939ad7aaa3807f5dfa44d129d4b3` |
| dsec-mbpp-case | `1747d4e63755747bef3ebaa5bf0b8e8c5778ee4d9061ace97aa79e81b4fafb31` |
| dsec-swegym-case | `479d3a96406945aaf282837699c87e02afdda4b7f2f8e75afad682583380a12d` |
| dsec-openhands | `77b032a0cd64c65ab1e81b9f592aa9fc18a783adaf1c2451c9e9d385ad989363` |

Outside the source tree, `--help` succeeded for all 16 installed commands. This proves only that their entry points load.

Host `192.168.0.110` first timed out on SSH, then returned No route to host. The user confirmed the host unavailable and requested local work only. No further remote access, Node, VM, model or training was started. Scoring the counter after activation and proving Node cleanup remained outstanding. TASK-007 stayed blocked and could not claim a full from-zero flow or a second fresh Linux host.

Before stopping the stack, the real API showed zero usage in all five root-project counters, empty admissions, and zero held on the JOINING Node. Both APIs, three etcd containers and dedicated network stopped/retired. Private deployment config and etcd volumes were kept for resuming the same membership state. Remote services and existing host containers were untouched. Local `.runtime/release-entry-20261030/` contains `controller-startup.json`, `membership-check.json`, `pre-stop-audit.json`, `compose-services.json`, `local-regressions-r2.json`, `installed-entrypoints-r2.json` and API/regression logs. Private deployment credentials stay outside source and wheel.

<a id="2026-11-04task-008-正式-api-的无网-linux-fullvm"></a>

## 2026-11-04: TASK-008 offline Linux FullVM through the public API

An ordinary Linux account installed the separate core wheel and built a 512 MiB ext4 boot disk offline from an existing Python base image. Docker served only to export the root filesystem and run temporary loopback etcd. FullVM did not start inner Docker. Path: HTTPS SDK → API/IAM/Placement → mTLS Node → private Edge → QEMU/KVM. Reused validated Linux 6.1.186, shared CID pool, 1 vCPU/512 MiB. One VM at a time; no GPU or sudo.

- Core wheel SHA-256 `823817cc8947d299b773cbf80aca52e2060a4d103d1fa749ea83c9e4a74411d8`.
- Final template SHA-256 `b3b333d3171e4025b6db7613971972c4c8c84aed94141788fb6e83922938e0fe`.
- Four API checks: rejected wrong backend/changes to fixed resources; distinct FullVM route and create receipt; guest UID 1000 command, same-ID deduplication, reconnect, sessions and binary files; clean new disk and stop proof.
- After two sequential boots both private disks were deleted; QEMU processes, CIDs, physical leases and IAM usage returned to zero. Temporary control services and etcd stopped. Five original Docker containers retained identities/PIDs/start times.
- Local portable core suite: 681 checks, 671 passed, ten skipped. The previous physical three-path matrix was not rerun.

Private umask initially restricted exported rootfs and Python library permissions. The final builder preserved pinned base-image modes and explicitly set newly added guest file modes. Tasks still ran as non-root with `NoNewPrivs`. Failed logs remain. Final host evidence `/home/xiaoxiaohu/dvf04/f/report.json` and `api-report.json`. This was an offline headless Linux preview; desktop, OSWorld, external networking, FullVM snapshots, GPU and production tenant isolation were outside scope.

<a id="2026-11-04task-009-fullvm-2d-桌面与-osworld-单题"></a>

## 2026-11-04: TASK-009 2D FullVM desktop and one OSWorld task

An independent install used HTTPS API → mTLS Node → Edge → QEMU/KVM. Ubuntu 22.04.5/Xfce guest used Ubuntu 7.0.0-38 and matching virtio drivers, 1 vCPU, 1024 MiB RAM and 1024 MiB private disk. No network, physical GPU or 3D acceleration. Screenshots and input used private QMP, without exposed VNC or guest TCP. Initial boot argument and Ubuntu Python 3.10 type import issues were corrected; failures remain recorded.

- Runtime wheel SHA-256 `3408596769fbe8dc02bc21d8daa03f19d82616920829feafdb34a18d98a5b5d6`. Guest used Python 3.10-compatible `execution.py`; template digest `612f258c4a83b626e0753347bdfca9d6f4e76ce18621be43e44e82a7edb98f46`.
- Ubuntu/Xfce capture was 1024×768 PNG. Target and observed pointer coordinates both (140,248). After clicking, the virtio keyboard typed into xterm and produced a file. Repeating the pointer request ID retrieved original proof. A local regression verified that lost QMP replies did not replay.
- OSWorld commit `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`, task `e0df059f-28a6-4169-924f-b9623e7184cc`: rename desktop directory Jan_1 to Jan_2. An ordinary guest user recreated the same starting directory. The original verification command and `exact_match` function were retained. Fixed terminal actions typed `cd` then `mv Desktop/todo_list_Jan_1 Desktop/todo_list_Jan_2`. Score **0.0** before and **1.0** after. First run scored 0 due to a different initial terminal cwd; the screenshot showed the keys were delivered. A new instance reran the corrected fixed actions with task and scoring unchanged.
- Passing host records `~/dvd04/d/api-report.json` (desktop), `~/dvd04/e/osworld-report.json` and `report.json` (task and cleanup). The latter confirmed VM, CID, private disk, physical/IAM leases reclaimed; original host containers unchanged.

The initial run omitted the official Docker provider baseline due to disk space. ZIP64 directory metadata gave `Ubuntu.qcow2` an unpacked size of 24,460,197,888 bytes, exceeding free space. The archive itself needed 12,273,896,463 bytes plus runtime write-layer headroom. Only the last 64 KiB of ZIP was requested. The initial 1.0 covers DSec execution and the official scoring function; paired testing follows below.

<a id="2026-11-04osworld-官方-docker-provider-单题对照完成"></a>

### 2026-11-04: official OSWorld Docker provider comparison on one task

About 134 GiB was available after host cleanup. Follow-up used the same OSWorld commit, task SHA-256 `a507ec6c35284c1413efe0d8a325b41a1626522221efc14f153742a865f1c04d`, two terminal inputs and upstream `exact_match` function. Official side used upstream DockerProvider start/stop, stock Ubuntu QCOW2, official initialization and command-result getter. Launch adaptations only bound mapped ports to loopback and added a cleanup label. QEMU used KVM; no GPU.

| Provider | Before input | After input |
| --- | ---: | ---: |
| DSec FullVM | 0.0 | 1.0 |
| Official OSWorld Docker | 0.0 | 1.0 |

Official disk source: Hugging Face revision `a5d9c3eaae98eebf6e3a0beb84e7e47cf72ae133`. Downloaded ZIP matched official SHA-256; unpacked QCOW2 SHA-256 `6bf667a852b3c307f61d9f09c42559351f45e0607e428b4997becf534cf4d313`, official runtime image `happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9`. Initial run reached screenshot readiness, then exited because the test script omitted PID from its Docker process query. It cleaned up fully with no score. Final run obtained a valid score, saved before/after screenshots and removed the transient container. Host containers retained IDs/PIDs/start times.

Official raw host evidence `~/dvd04/official-run-r2/`; local comparison manifest `.runtime/fullvm-desktop-20261104/evidence-final/osworld-comparison.json` records both scores, actions and screenshot digests. Only scoring parity for this task was established. Desktop bases and allocated resources differed; no performance comparison or full OSWorld compatibility follows.

<a id="2026-11-04task-010-gui-owl-2b-单题视觉-rollout"></a>

## 2026-11-04: TASK-010 GUI-Owl 2B single-task visual rollout

Reused TASK-009's folder rename, Ubuntu 22.04.5/Xfce FullVM and official `exact_match` scorer. Model `mPLUG/GUI-Owl-1.5-2B-Instruct`, revision `528ceaec795bbfbe6103bd79e03db849feadfb24`. External harness used upstream prompt/action format. The model saw task instruction, 1024×768 screenshots and past actions, acting only through DSec desktop API. Initialization and grading remained in the harness and scores were withheld from the model.

- Official score **0.0**. In 16 steps the model alternated eight right clicks and eight Delete actions. The first right click landed within the folder icon, but the next screenshot showed no menu. The model kept issuing Delete without starting rename or entering the new name. Parsing and SDK calls succeeded. Why the menu was absent still required investigation.
- Transformers BF16 greedy decoding on RTX 4080, max 256 new tokens per call. Sixteen inferences totaled 12.33 seconds. Runner wall time 28.88 seconds included model load, VM start/stop and scoring. PyTorch peak allocation 4626 MiB, peak reservation 4778 MiB.
- The initial run passed screenshots as `file://` URIs unsupported by the current Transformers. It exited during image read after model load, without actions or score. The path was fixed and a new VM reran the same configuration.
- Final GPU returned to its 35 MiB baseline. IAM, physical leases and CID were zero, private disk gone, original host containers unchanged in identity/PID/start time.

Host `~/dvd04/owl2-r2/` holds `osworld-report.json`, `report.json`, `trace.jsonl` and per-step screenshots. Failed log `~/dvd04/owl2-r1/inference.log`. Acceptance covers one scorable trajectory and cleanup. The model failed the task; the official provider was not rerun.

<a id="2026-11-04task-011-修正鼠标点击并补齐桌面文件管理服务"></a>

## 2026-11-04: TASK-011 pointer click correction and desktop file manager

Three right-click methods were compared on the same temporary FullVM. Actual pointer position (76,322) was inside the icon. Press and release in one request produced no menu. Separate requests about 80 ms apart displayed the context menu; guest-side positive control `xdotool click 3` displayed the same menu. The harness now submits movement, press and release separately with independent IDs. Raw comparison `~/dvd04/owl-rightclick-r1/`.

| Condition | Official score | Observed behavior |
| --- | ---: | --- |
| Original harness and desktop | 0.0 | No menu after right click; model alternated right click and Delete |
| Split click, original desktop | 0.0 | Clicked Rename at step 2; Xfce reported missing file management service |
| Split click with Thunar installed | 1.0 | Renamed successfully at step 4; model continued to the limit of 16 |

The corrected run retained original core wheel, model revision, official task, screenshot size, greedy decoding and 16-step limit. Thunar `4.16.10-1` was installed in an isolated guest build image. New template SHA-256 `f08c91676caa170eb41cd8bfba178920ae0ed991e483a735a3d05c10a14f8db2`. Screenshot 5 confirmed the new name; official final check confirmed new folder present and old folder absent. Final runner took 33.20 seconds, with 13.11 seconds inference. The model did not emit `terminate`. Raw traces show continued actions after success.

Original desktop rerun and final run: `~/dvd04/owl2-r3/` and `~/dvd04/owl2-r4/`. All three runs returned VM/CID/IAM/physical leases, deleted private disks and left host containers unchanged. GPU returned to 35 MiB. Scope: single-task visual trajectory.

<a id="2026-11-04task-012-gui-owl-停止行为复测"></a>

## 2026-11-04: TASK-012 GUI-Owl stop behavior rerun

Reused TASK-011 pinned model, original upstream prompt, greedy decoding, 16-step limit, corrected pointer events and Thunar template. Four sequential fresh FullVMs each ran one trajectory. This replicated the same settings without changing sampling randomness or comparing prompts. In each run screenshot 5 showed the step-4 rename. Official verifier ran only after the entire trajectory.

| Valid trajectory | Renamed by step 4 | Voluntary stop | Final step | Official final score |
| --- | --- | --- | ---: | ---: |
| `owl-stop-r01` | Yes | No | 16 | 0.0 |
| `osrp02` | Yes | No | 16 | 1.0 |
| `osrp03` | Yes | No | 16 | 1.0 |
| `osrp04` | Yes | No | 16 | 1.0 |

All four step-5 replies mentioned the correct new name but continued acting. In the first run the model entered `todo_list_Jan_3` at step 15, confirmed at step 16, and the final screenshot verified the change. A separate attempt `owl-stop-r02` failed during VM boot without a model action; no specific cause appeared in the log. CID, VM and private disk retired. It was excluded. All four valid runs released temporary resources, returned the GPU to 35 MiB and preserved host containers. Local evidence `.runtime/fullvm-desktop-20261104/evidence-owl-stop-repeat/`; host directory names are in the table.

<a id="2026-11-10task-013-qemu-逐步差分辅助-cua-harness-修复"></a>

## 2026-11-10 TASK-013 Using stepwise QEMU differences to repair a CUA harness

We reused the TASK-011 QEMU/KVM and Thunar FullVM template, OSWorld task and official `exact_match` scorer. We replayed the same right-click on a fresh VM for each implementation. Both APIs acknowledged the action and placed the pointer at the same coordinates. The first difference was whether the XFCE menu appeared after the click. The original implementation scored 0. The reference scored 1 and passed a regression test that renamed a different directory at a different position.

Both groups used the same offline Qwen2.5-3B-Instruct, 8 CPU threads, temperature 0.6, top-p 0.9, a 96-token generation limit and up to 3 rounds. Four paired random seeds ran in AB/BA/AB/BA order. Both groups received the source, two raw events and window observations. Group B also received an automatically aligned description of the first difference. Screenshots were retained for audit. The text model read the corresponding X11 window observations. Candidates could change mouse event grouping and timing, expressed as JSON and recorded as corresponding source edits. Each candidate ran on a fresh VM under a fixed external runner that controlled scoring and the held-out regression.

| Group | Repaired within 3 rounds | First passing round | Candidate VMs | Median time per session |
| --- | ---: | --- | ---: | ---: |
| A, raw evidence | 4/4 | 2, 2, 2, 2 | 8 | 26.22 s |
| B, raw evidence and aligned report | 4/4 | 2, 2, 2, 2 | 8 | 26.56 s |

All eight sessions batched down/up in round one and split them in round two. Review of the raw results showed that B4 used a 0 ms interval in both rounds and all other sessions used 80 ms in both rounds. Every passing candidate passed the official scorer and held-out regression. Timing includes CPU inference and live evaluation for each round. Extracting the difference took about 12 microseconds. Generating the control traces shared by both groups took about 12.77 seconds. This was a single hardware trial, so small timing differences do not establish a performance result.

With one fault and a narrow edit space, the aligned report did not reduce attempts. Both groups could already see the batching difference in the raw log. The report pointed to its location. This comparison does not generalize to multi-step, long-trajectory, multi-fault or unrestricted source editing. The lab machine retains per-round reports and screenshots in `~/dvd04/cdiff1/`. Private disks for 18 VMs were reclaimed, IAM, physical and CID leases returned to zero, the proposal process exited and existing host containers retained their original state.

<a id="2026-11-10task-014-cua-差分修复-rtx-4080-复测"></a>

## 2026-11-10 TASK-014 Retesting CUA differential repair on an RTX 4080

After the NVIDIA driver was repaired, we moved inference to an RTX 4080 with the TASK-013 model, FP32, input format, four paired seeds, execution order, candidate limit and fixed VM evaluation. CUDA matmul TF32 was disabled. Peak allocation reached 11954.2 MiB. Both groups read window observations and retained the raw screenshots as evidence. CPU and CUDA sampling need not produce identical token sequences.

| Group | Successful repairs | First passing round | Median model inference per session | Median total time per session |
| --- | ---: | --- | ---: | ---: |
| A, raw evidence | 4/4 | 2, 2, 2, 2 | 1.24 s | 13.81 s |
| B, with differential report | 4/4 | 2, 2, 2, 2 | 1.26 s | 13.88 s |

All successful candidates separated down/up, kept 80 ms and passed the official scorer and held-out regression. The GPU greatly reduced model inference time. Again the differential report did not reduce the number of rounds. Session time sums inference and VM validation across rounds, excluding shared model loading and control preparation.

After one VM failed to start, the SDK confirmed cleanup and the scripted retry succeeded. Private disks were retired for 18 evaluation VMs and 1 failed start. Physical, IAM and CID leases returned to zero and host containers were unaffected. GPU usage returned to 1 MiB after the proposal process exited. Evidence is in `~/dvd04/cdiffg1/` on the lab machine.

<a id="2026-11-10task-015-两题-fullvm-calc-回放与故障定位"></a>

## 2026-11-10 TASK-015 FullVM Calc replay and fault localization on two tasks

We pinned the OSWorld commit `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`, original `compare_table` and workbook digests. Calc 7.3.7 was installed on a dedicated Ubuntu/Xfce template with image SHA-256 `92fed010ba910cbc218b661c0ca23e067d4bdb10d5f5e0e386db1219504f58db`. Workbooks were transferred and read back through the official SDK native file channel. Application launch and window probes served setup and observation only. Every task action used the public desktop input API. Replay explicitly selected the Lines Only subtype for line charts. Saving the XLSX used the virtual keyboard and GUI confirmation. The original scoring rules read the result in an isolated host scoring environment.

| Replay condition | Runs | Official score | First difference |
| --- | ---: | ---: | --- |
| BoomerangSales healthy, 24 steps | 2 | 1.0 | None |
| WeeklySales healthy, 16 steps | 2 | 1.0 | Held-out task score |
| Early release during drag | 2 | 1.0 | Step 7, selection |
| Missing Shift in key chord | 2 | 0.0 | Step 3, selection |
| Chart wizard returned stale frame | 2 | 1.0 | Step 17, wizard view |

Action sequence numbers link raw screenshots to requests and acknowledgments. At three predefined semantic checkpoints, the stepwise comparison used window names and cropped regions to avoid noise from the clock and pointer. The official scorer still gave 1.0 to both the drag and stale-frame faults. A later action reset the selection in the first case. In the second, fixed actions could continue while the actual guest state was correct. Independent checkpoints rejected both. Acceptance applies to the fixed UI and declared checkpoints.

Calc was not ready in time for the first fixed matrix. We added readiness checks for the Xfce and Calc windows and repeated the run. All private disks, CIDs, physical and IAM leases for ten FullVMs in the passing run were reclaimed. Existing host containers retained their state. The raw report is in `~/dvd04/cmat2/`. For the reusable entry point, see [OSWorld desktop regression](../../apps/osworld/README.md).

<a id="2026-11-10task-015-长轨迹修复对照"></a>

### 2026-11-10 TASK-015 Long-trajectory repair comparison

After the healthy and faulty matrix passed, six pairs of sessions ran with Qwen2.5-3B-Instruct on an RTX 4080 with fixed BF16 settings. An initial FP32 attempt ran out of GPU memory during long-input prefill and produced no candidate. It is retained in `~/dvd04/cab1/`. Peak BF16 allocation was 6559.24 MiB. Both groups used temperature 0.6, top-p 0.9, a 96-token generation limit, three rounds per session and the same paired seeds. Both saw the action sequence, acknowledgments, windows and screenshot region summaries at each checkpoint. Only B received an automatically aligned report of the first difference. A proposal could change one input or observation setting in the external harness. Acceptance required eliminating the first difference and passing the official scores for the main and held-out tasks.

| Fault | A, first passing round | B, first passing round |
| --- | --- | --- |
| Drag | 1, failed | Failed, failed |
| Key chord | Failed, 3 | 2, 3 |
| Stale frame | 1, 1 | 1, 1 |

Both groups repaired 4/6 faults within three rounds. B was earlier in one pair, later in one and tied in four. A took 12 rounds and 13 valid VM replays, with a median session time of 57.58 seconds. B took 13 rounds and 16 valid VM replays, with a median of 88.22 seconds. Session timing includes per-round inference, actual replay validation and B's differential generation, excluding the fixed matrix and shared model loading. A identified the correct step on the first round in 3/6 sessions and B in 6/6. Since B received the step number directly, we counted correct first-round code setting selection separately. A scored 3/6 and B 2/6. This small sample did not show an overall repair speedup. The result is limited to synthetic faults, predefined checkpoints and bounded configuration patches.

The complete report is in `~/dvd04/cab2/` on the lab machine. The run retired 29 private disks. Physical, IAM and CID leases returned to zero, existing host containers retained their state and GPU usage returned to 1 MiB after model exit. Replay, scoring and differential scripts are included in the source distribution. Task inputs, reference workbooks, the model and run evidence are stored separately.

<a id="2026-11-10task-016-fullvm-输入与帧来源差分"></a>

## 2026-11-10 TASK-016 FullVM input and frame provenance differences

We reused the two OSWorld tasks, pinned image, SDK and action sequences from TASK-015. `compare_trace.py --diagnose` reports separately the first submitted event difference, final button-state difference, first frame provenance difference and first visible checkpoint difference. Final button state is derived from SDK-acknowledged events and does not establish what events the guest actually received. Each screenshot record contains two capture IDs, the local monotonic return timestamp and a content digest. When the harness delivered the pre-action frame, it retained the actual post-action PNG separately. Older traces still support input and visible-state comparisons, with frame provenance marked unavailable.

Both healthy replays of each task scored 1.0 officially. Each of the three faults ran twice. The enhanced report identified the correct step and boundary in 6/6 runs.

| Fault | Step | Additional evidence |
| --- | ---: | --- |
| Early drag release | 7 | Moving to the endpoint and releasing the left button were reversed. Both frames came from after the action. |
| Shift dropped from chord | 3 | The same intended chord produced 6 versus 4 SDK-accepted events. The Shift press/release pair was missing. |
| Stale chart wizard frame | 17 | Input event order matched. The reference returned the post-action frame. The faulty implementation returned the pre-action frame and retained the post-action frame separately. |

Rechecking an incorrect drag patch from TASK-015 showed no visible difference under the old comparison. The enhanced report showed that the left button was still down at steps 7–9. Healthy and faulty matrices are in `~/dvd04/cdm1/` on the lab machine. All private disks and leases for the ten FullVMs were reclaimed.

<a id="2026-11-10task-016-增强证据-ab-复测"></a>

### 2026-11-10 TASK-016 Retesting A/B with enhanced evidence

We kept the original tasks, three fault types, Qwen2.5-3B-Instruct BF16, paired seeds, within-group execution order and three-round budget. Both groups saw raw traces with coordinates, event acknowledgments and frame capture IDs. After each live candidate validation, both received the same enhanced diagnostics. Only B received the aligned report before the first proposal. New button-state and frame provenance checks applied to both groups. Raw evidence, feedback and acceptance criteria all changed relative to TASK-015. Within-run pairs measure the additional effect of receiving the report early.

| Fault | A, first passing round | B, first passing round |
| --- | --- | --- |
| Drag | 2, failed | 2, failed |
| Key chord | 3, failed | 3, 3 |
| Stale frame | 1, failed | Failed, failed |

Both groups repaired 3/6. B was earlier in one pair, later in one and tied in four. A took 15 rounds and 13 VM replays, with a median session time of 63.31 seconds. B took 17 rounds and 14 VM replays, with a median of 85.53 seconds. Both chose the correct setting in 2/6 first rounds. Both B cases involved stale frames and resubmitted the faulty value `before` unchanged, so validation rejected them before evaluation. First-round step identification was 2/6 for A and 5/6 for B, which had already received the step number.

In one drag trajectory per group, the first proposal incorrectly changed the frame source. The validation report showed both the original input difference at step 7 and the newly introduced pre-action frame at step 17. Both corrected event order in the next round. A similar regression in the key chord case retained the original difference at step 3 and the new frame difference at step 17. The validation feedback distinguished the original fault from a new regression. With this model, these controlled faults and this small sample, the additional first-round report did not reduce rounds or wall time.

The live report is in `~/dvd04/cda1/`. All 27 candidate evaluation VMs were retired. Physical, CID and IAM leases returned to zero, existing host containers retained their state and GPU usage returned to 1 MiB.

<a id="2026-11-10task-017-自主视觉-cua-健康基线"></a>

## 2026-11-10 TASK-017 Autonomous visual CUA healthy baseline

We established a new baseline from a healthy desktop. Earlier fixed-action data served only as FullVM regressions and was excluded from autonomous CUA fault and repair comparisons. Each of eight trajectories used a fresh FullVM. The model read the task instruction and live PNG and independently chose each keyboard or mouse action. Actions ran through the HTTPS SDK. Only after the task ended did we perform the save required by the pinned OSWorld task and run the upstream `compare_table` scorer. When the target file could not be saved, scoring used the workbook actually exported and recorded the postconfig window activation state.

| Calibration | BoomerangSales | WeeklySales |
| --- | ---: | ---: |
| GUI-Owl-1.5-2B-Instruct, 48 steps | 0.0 | 0.0 |
| Qwen3.5-4B visual non-thinking, 48 steps | 0.0 | 0.0 |
| GUI-Owl-1.5-2B-Instruct, preconfigured desktop, 56 steps | 0.0 | 0.0 |
| GUI-Owl-1.5-4B-Instruct, preconfigured desktop, 56 steps | 0.0 | 0.0 |

Preconfiguration disabled Calc's first-run prompts and XLSX format confirmation, gave generic instructions to avoid repetition, save and finish, and retained the two most recent frames. These were independent calibrations. Different model/configuration results cannot establish single-variable effects. The 4B artifact was pinned to `3f061c2c562cc860c42bf32542a70e07a7ff4840`, both weight shards passed full-file SHA-256 checks and peak GPU allocation was about 9694 MiB. Both preconfigured WeeklySales trajectories changed the workbook but did not leave the requested chart in Sheet2. On the main task, 4B remained on the chart wizard's data series page without completing the range input. All eight trajectories went through official scoring, had no tool parsing failures and reached the step limit.

The healthy baseline missed the full-score entry criterion. Fault and repair A/B testing awaits a usable autonomous baseline. Evidence is in `~/dvd04/{clive1,cqv1,cop2,cop4}/` on the lab machine. All eight FullVMs, private disks, CIDs, physical and IAM leases were reclaimed, existing host containers retained their state and GPU usage returned to 1 MiB.

<a id="2026-11-10task-018-gui-owl-4b-自主扫描-osworld"></a>

## 2026-11-10 TASK-018 Autonomous OSWorld scan with GUI-Owl 4B

On an RTX 4080 and a DSec FullVM running Ubuntu 22.04/Xfce with Calc 7.3.7, we pinned GUI-Owl-1.5-4B-Instruct `3f061c2c562cc860c42bf32542a70e07a7ff4840` and used BF16, greedy generation and the two most recent frames. OSWorld was pinned to `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`. We selected 8 tasks supported by applications installed in the image. Each allowed up to 32 model decisions on a new VM. The model read the live desktop and original instructions and sent keyboard and mouse events through the official SDK. Successful tasks ran again autonomously on another fresh VM. Upstream scoring functions and rules were unchanged. The guest path became `/work`. Calc retained first-run and format-confirmation preconfiguration and the save before official scoring.

| Task ID prefix | Task | First score | Model steps | Rerun |
| --- | --- | ---: | ---: | ---: |
| `e0df059f` | Rename a desktop directory | 1.0 | 5 | 1.0 (5 steps) |
| `51b11269` | Sort amounts in ascending order | 1.0 | 3 | 1.0 (3 steps) |
| `1334ca3e` | Zoom out on a sheet | 0.0 | 7 | — |
| `4188d3a4` | Freeze the header range | 0.0 | 11 | — |
| `1273e544` | Copy the Revenue column to a new sheet | 0.0 | 10 | — |
| `1e8df695` | Add a Profit column and formula | 0.0 | 32 | — |
| `0cecd4f3` | Copy and rename a sheet | 0.0 | 24 | — |
| `5ea617a3` | Restore a poster from the trash | — | 0 | — |

All seven tasks with valid scores started at 0. The model earned full marks by right-clicking to rename, and by choosing Sort Ascending from Calc's Data menu. Four zero-score tasks stopped when the same action and frame repeated. On the Profit task, repeated clicks at the same location changed the screenshot hash and the run reached the step limit. Trash restoration exited during setup because the image lacked `gio`. Two initial launch failures were cleaned up and succeeded on retry.

The lab evidence in `~/dvd04/scan1/` includes official scores, raw responses, input events, screenshots and termination reasons. Across 10 task runs, 12 private disks were retired, including two startup retries. IAM, physical and CID leases returned to zero, existing host containers retained their state and GPU usage returned to 1 MiB. This deliberately selected small healthy sample is reported separately from the original TASK-017 tasks and fault repair results.

<a id="2026-11-11-task-019-gui-owl-4b-扫描-osworld-360-题"></a>

## 2026-11-11 TASK-019 GUI-Owl 4B scan of 360 OSWorld tasks

We ran `test_nogdrive.json` from OSWorld `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`, covering 360 unique tasks in ten application categories. As in TASK-018, we used GUI-Owl-1.5-4B-Instruct `3f061c2c562cc860c42bf32542a70e07a7ff4840`, BF16, greedy generation and the two most recent frames. Each task allowed 32 model decisions. To cover the desktop applications, we used separate QEMU/KVM VMs with the official OSWorld Ubuntu image, 1920×1080, 4 vCPU and 4 GiB RAM. These results are recorded separately from the TASK-018 DSec FullVM scores. Guests had no public network access. The host cache supplied 622 official input and scoring assets. Initialization and scoring used the original rules.

| Outcome | Tasks |
| --- | ---: |
| 1.0 on initial and fresh-VM rerun, below 1.0 on no-action control | 73 |
| 1.0 initially, below 1.0 on rerun | 18 |
| 1.0 initially, on rerun and on no-action control | 1 |
| Partial credit on first run | 6 |
| Official 0.0 on first run | 192 |
| Isolation configuration lacked a proxy or external network required by the task | 64 |
| Setup failure | 3 |
| Input channel error | 1 |
| Scoring error | 2 |

All 92 initial full-score tasks ran again in fresh VMs. Repeated full scores triggered another fresh VM for a no-action control. The 73 stable full scores covered all ten application categories, including 17 Impress tasks, 11 OS tasks, and 7 each for Thunderbird and VS Code. New examples included a Calc pivot table, centering a yellow triangle in GIMP and restoring a poster from the trash. The original Spotify trial returned 0 but lacked an online installation source. We retained the record and classified it as environment-limited. After an independent review of exceptional runs, 6 tasks still had no valid score, including 3 setup failures, 1 X11 input disconnect and 2 HTTP 500 errors during scoring postprocessing.

Raw reports, per-step screenshots and input evidence are in `~/dvd04/osw360/campaign/` on the lab machine. The per-task and full-score lists were checked against official task IDs. Cleanup was confirmed for all 471 VM run records. Private disks, QEMU processes and active episodes returned to zero, and GPU usage returned to 1 MiB.

<a id="2026-11-11-task-020-dsec-fullvm-启动-osworld-官方镜像"></a>

## 2026-11-11 TASK-020 Booting the official OSWorld image in DSec FullVM

The installed wheel had SHA-256 `242efe82f37b4e5bc6ff490123b09ec1ec211cb5a09bc99997c789692573306a`. The official OSWorld Ubuntu QCOW2 served as a read-only base. A separate incremental layer prepared the startup environment for the DSec guest agent. Each SDK create allocated a private writable QCOW2 layer and UEFI variable file. After both acceptance runs, the official base and guest agent layer still matched their respective preboot SHA-256 digests.

The official HTTPS SDK, API, Node and Edge started two fresh KVM VMs running Ubuntu GNOME at 1920×1080 with 4 vCPU, 4 GiB RAM and no external network. After authentication, the SDK executed commands as UID 1000, verified a UEFI boot, uploaded and read back official assets and ran the original `gio trash` initialization. The original guest paths and official `exact_match` scoring function came from pinned OSWorld version `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`.

| Restore poster from trash `5ea617a3` | Initial score | Final score | Model steps |
| --- | ---: | ---: | ---: |
| GUI-Owl-1.5-4B-Instruct first run | 0.0 | 1.0 | 32 |
| Fresh FullVM rerun | 0.0 | 1.0 | 32 |

In both trajectories the model inspected screenshots and sent mouse actions through the SDK. The restored poster was read back through the SDK and matched the original asset's SHA-256. Both trajectories reached the model step limit. During acceptance, systemd logs led to fixes for credentials being issued too early during firmware boot and for ownership of the native Unix socket directory. The evaluation adapter was updated for 1920×1080 and the generic `click` action.

139 Linux regressions and 17 Node preflight regressions passed. The writable layers and UEFI variable files for both acceptance VMs were removed. CID, physical and IAM leases returned to zero. Step screenshots, raw model outputs, SDK acknowledgments, official scores and cleanup evidence are in `~/dvd04/uf6/` on the lab machine.

<a id="2026-11-11-task-021-uefi-fullvm-宿主隔离"></a>

## 2026-11-11 TASK-021 Host isolation for UEFI FullVMs

The installed wheel had SHA-256 `a2208af545d19047c60f68017ed04faeb226f6f8e2ae53bccfab1bf0bd6865e9`. We reused the official Ubuntu base and guest agent layer. An administrator installed a dedicated `dsec-ufiso` broker. Edge started two new offline FullVMs within a delegated unprivileged user service. Both runs of trash restoration task `5ea617a3` via the official HTTPS SDK went from 0.0 to 1.0, reached the 32-step limit and restored a poster with the same SHA-256 as the original.

Each VMM ran under its own enforced AppArmor profile and cgroup. For each VMM the kernel recorded one denial for reading outside the instance and one for creating a screenshot outside the instance. Live file descriptors showed that OVMF firmware, the official QCOW2 base and the guest agent layer were opened read-only. The instance QCOW2 and UEFI variable files were opened read/write. Each VMM cgroup had a 4-CPU quota, 5 GiB RAM, zero swap and a limit of 256 PIDs. CPU, memory and PID controllers were delegated. AppArmor enforced file access. A separate delegated service demonstrated CPU throttling, memory OOM and PID denial. A further 141 Linux regressions passed.

After both VMs stopped, AppArmor profiles, instance cgroups, writable QCOW2 layers, UEFI variable files, CIDs, physical and IAM leases were reclaimed. The base and guest agent layer passed post-shutdown digest checks. The original acceptance script reported failure because unrelated Docker containers appeared during the run. Independent Docker event review showed no lifecycle event for the pre-existing Grafana, Prometheus, node-exporter or wandb-local containers. The temporary etcd belonging to acceptance was destroyed. The original result and kernel audit review are retained with the live evidence in `~/dvd04/ui1-before-docker-check-fix/`.

<a id="2026-11-11-task-022-修正-osworld-验收的-docker-检查"></a>

## 2026-11-11 TASK-022 Correcting the Docker check in OSWorld acceptance

Before acceptance the runner records the full IDs and start times of running host containers. At completion it checks that those containers still run with their original start times. A label unique to the run identifies the temporary etcd. After cleanup the lookup includes stopped containers. Simulated regressions cover new unrelated containers, existing containers stopping, restarting or disappearing, and leftover temporary etcd.

With the TASK-021 installed build and isolation configuration, the official HTTPS SDK ran trash restoration task `5ea617a3` on two fresh FullVMs. Both official scores went from 0.0 to 1.0. Both reached the 32-step limit and restored a poster whose digest matched the original.

The original overall report returned `passed` and the process exited with code 0. All 4 pre-existing containers remained running without a restart. The temporary etcd was removed. Writable layers, UEFI variable files, cgroups and leases for both VMs were reclaimed. IAM, physical and CID leases returned to zero. The raw live report is `~/dvd04/ui1/report.json`, SHA-256 `103084e4c5698adee02035a29bc87f77e3647a0879bf616113384629823eb546`.

<a id="2026-11-24-task-007-正式-api-首次使用实机验收"></a>

## 2026-11-24 TASK-007 First-use live acceptance through the official API

The release allowlist exported 530 files from the worktree. The archive SHA-256 was `1843a64daa863705976f77a3957c73c551d4d04259fda811055e826287c5a970`. An offline build from this archive produced the only required core wheel, SHA-256 `6f914048b6b6bed39706243e51340407aabf6d191ca757df24128f86b0b1bd70`. The Mac client, two containerized APIs and an independent Linux Node used the same wheel. We generated configuration using the public deployment guide, initialized three-member etcd, then registered and activated the Node.

Linux reused host KVM and an existing Alpine 3.20 image. We obtained Firecracker 1.17.0 and official CI kernel 6.1.188 afresh and built an offline ext4 guest using the release builder. The official HTTPS API accepted the built-in counter task. Of two fixed responses, the first wrote a counter value of 3 in the guest and the second finished the task. The verifier read the result and awarded 1.0. The rollout ID was `8f0499d019e0443ead3832678614ca7c`. The case covers API deployment and verifier execution with no model sampling.

Persistent sandbox and rollout records both reached STOPPED, the physical lease reached RELEASED and the Node retirement receipt reported RETIRED with held=0. All three Linux user services stopped. Temporary Compose containers, network, etcd volumes and the control-plane image were reclaimed. Workspace evidence in `.runtime/release-entry-20261124/` includes the score report, Node retirement receipt and cleanup log.

<a id="2026-11-24-task-002-正式-api-下的会话文件与-http"></a>

## 2026-11-24 TASK-002 Session files and HTTP through the official API

We reused the TASK-007 installed core wheel (SHA-256 prefix `6f914048`), Firecracker 1.17.0 and Linux 6.1.188. A dedicated dual-API control plane and one Linux Node published two environments. The BusyBox HTTP test service inside the guest listened only on loopback. The client requested both file and HTTP capabilities through the official HTTPS SDK.

The environment declaring only session files returned `NoPlacement` and the project admissions list was empty. The environment with both capabilities created one MicroVM. The same session wrote and read back 69,888 identical bytes. A binary POST to the guest loopback service received status 200, expected headers and the first 16 bytes marked truncated. Repeating the same request ID returned the same response. The server executed it once.

The sandbox stopped, its only physical lease was released and the Node retired with held=0. The 5 pre-existing host containers retained their running state and start times. The run's Node, Edge, Compose services, volumes and networks were reclaimed. Workspace records are in `.runtime/session-http-20261124/run2/` and the adjacent cleanup receipts. An earlier attempt copied the Node config before etcd finished initialization and missed issued credentials. Completing the public guide's steps confirmed activation with the original ID. The official admissions list supplied evidence for the denied case. Live coverage here is one offline MicroVM, loopback HTTP and bounded responses.

<a id="2026-11-24-task-003-共享-erofs-层的重复校验"></a>

## 2026-11-24 TASK-003 Repeated verification of a shared EROFS layer

On local Linux ext4, the accepted installed wheel (SHA-256 prefix `6f914048`) published two Firecracker environments. They shared a boot disk, kernel and one 48,201,728-byte EROFS layer, with one unique layer each. All three rounds used the same object store and each published to a new target catalog directory. The API rejects repeated registration of an environment name in the same catalog. The table counts only the shared layer. Logical reads sum bytes returned to verification code. Underlying reads use the executing thread's Linux `read_bytes` delta. Timing sums reading and hashing intervals. Before first publication and first Edge startup, DONTNEED was applied only to experiment copies.

| Phase | Full reads | Logical reads, MiB | Underlying reads, MiB | Shared-layer time, ms |
| --- | ---: | ---: | ---: | ---: |
| First publish of A, new object | 3 | 137.91 | 45.97 | 93.15 |
| First publish of B, reused A's object | 4 | 183.88 | 0 | 91.91 |
| Each of two further publications per environment | 4 | 183.88 | 0 | 91–93 |
| First Edge startup loading both environments | 1 | 45.97 | 45.97 | 30.22 |
| Each of two warm Edge restarts | 1 | 45.97 | 0 | 22.89–22.97 |
| Recover both existing VMs after each restart, combined | 0 | 0 | 0 | — |

On an object hit, resolving the source directory read the full source layer, then `_copy_object` hashed both the source layer and existing target object. Finally `_atomic_catalog` created a catalog and read the entire target object. All four reads happened in the publisher. Edge performed first verification for the new process's catalog at `load_configuration`. The second environment and later `RuntimeStorage.prepare` calls from `registry_store.load` shared its verification cache. After both restarts, each VM's data was read back using its original ID. Atomically replacing the cached target with a file of equal length and different content made both environments reject the old SHA-256. Both VMs stopped, and test copies and writable instance disks were reclaimed.

We prioritized reusing a verified file identity and digest during a single publication, verifying the source and target independently and rechecking on identity change. The goal was to reduce four full reads on an existing object to at most two. Cross-process credentials and fs-verity require separate designs. This general catalog used ordinary SHA-256. Measurements cover host verification and exclude guest workloads and 3FS. Raw per-file records, call stacks, takeover results and cleanup receipts are in workspace directory `.runtime/publication-read-20261124/`.

<a id="2026-11-24-task-023-本地发布校验复用"></a>

## 2026-11-24 TASK-023 Reusing verification within local publication

We implemented per-publication file verification reuse according to [design proposal](../guides/ARTIFACT_PUBLICATION.en.md#task-023-设计提案--单次发布内复用本地校验). A new wheel `59204ca6aa94288d3643b1079c48c227d3fd516342f39ec6fcaab2d7f3e0142e` was installed on the same Linux host. We reused the two TASK-003 Firecracker environments, identical input bytes, probes and page-cache treatment. Each publication ran in a separate process. The shared layer was 48,201,728 bytes.

| Per-publication metric | Original | New |
| --- | ---: | ---: |
| First creation of an object, full reads | 3 | 3 |
| Existing object, full reads of source and target | 2 + 2 | 1 + 1 |
| Existing object, logical reads of shared layer | 183.88 MiB | 91.94 MiB |
| Existing object, median shared-layer read and hash time | 91.66 ms | 45.54 ms |
| Shared-layer hit, median full environment publication time | 364.70 ms | 188.19 ms |

Medians came from five hits per version. The shared layer was in page cache on every hit, and thread physical read counts were 0 B for both versions. On first creation both versions read 45.97 MiB of the shared layer from underlying storage. Whole-publication time includes the boot disk, kernel, task-specific layers and sampling overhead. The sample used local ext4.

The two VMs wrote different markers. Both were read back through the original IDs after two Edge restarts. Each new Edge process fully read the shared layer once during loading and performed zero additional reads during takeover. Replacing a verified target with a file of the same length and different content made both environments reject the old digest. We verified that both VMs stopped and their writable disks were reclaimed, then deleted the run's input and object copies. Regressions cover independently corrupted data, replacement midway through publication, verification of the actual winner of concurrent hard-link commits, and independent verification on two successive publications. All 20 installed Linux publisher and directory tests passed. Live workspace evidence is in `trace.jsonl`, `recovery.json` and `cleanup.json` under `.runtime/publication-reuse-20261124/`. Cross-process credentials and remote 3FS costs still require separate acceptance.

<a id="2026-11-24-task-024-构建依赖的文件级增量发布"></a>

## 2026-11-24 TASK-024 File-level incremental publication of build dependencies

The lab machine used core wheel `3c749c467a09f3ee5da72155892166ee0982ad2dbb066f23f0468fd81a11d9af`, Firecracker 1.17, a 64 MiB Alpine native ext4 parent disk and Linux e2fsprogs. The client used the public SDK over certificate-verified loopback HTTPS and real etcd. Controller and NodeGateway ran in one process, with Edge and the VM separate. Only one VM ran at a time. The test covered artifact handoff and recovery. Multi-host placement was outside its scope.

1. The builder wrote executable `/opt/dsec-tools/answer` and `/tmp/reference-answer`. The executable returned 42. The manifest approved only the executable's mode, size and SHA-256. The SDK captured a 349 B delta containing only the approved file and manifest when decoded.
2. Substituting an incorrect expected parent digest caused publication to be rejected and left the existing environment directory byte-for-byte intact. The valid delta published successfully. The parent boot disk retained its pre-create SHA-256. This run materialized the entire 64 MiB child disk.
3. The first child VM ran the dependency directly and returned 42. The build temporary file was absent. After we changed the program in this VM and stopped it, a new parent VM saw neither the program nor the temporary file. A second new child VM still ran the original program and returned 42.
4. Four distinct sandbox IDs reached STOPPED, each with the resource-reclaimed flag set to true. The temporary etcd container, child object store and Edge socket were reclaimed. Three targeted installed Linux regressions passed. Raw results are in workspace directory `.runtime/pack-diff-20261124/report.json`.

The acceptance script's first startup retained a capability declaration from before the parent descriptor changed. Edge correctly rejected it. The second lacked an Edge admission budget and could not obtain a placeable Node. Removing the stale declaration and explicitly passing the dedicated instance's budget completed the checks above. Neither attempt reached VM creation.

<a id="2026-11-24-task-025-overlaybd-父层复用与子-vm-恢复"></a>

## 2026-11-24 TASK-025 Reusing OverlayBD parent layers and recovering child VMs

The same Linux host ran core wheel `5863e9e50d3db282389e150114b98c08918f0009ab6bb9f402d8a340ac49ea86`, Firecracker 1.17 and the released Rust ublk daemon (SHA-256 prefix `b78207e6`). The public SDK created four instances sequentially via loopback HTTPS. Controller and NodeGateway shared one process, with etcd, Edge and VMs separate. The initial temporary launcher put the executable in /run, mounted noexec, so systemd refused to launch it. We used root-managed /usr/local/libexec instead and verified the socket peer PID before running acceptance.

- A 64 MiB Alpine ext4 boot disk formed a two-layer OverlayBD parent image. The build instance wrote the approved executable and an unapproved temporary file. The SDK captured a 349-byte handoff package. An incorrect parent digest was rejected and the directory contents remained intact.
- After publishing the child, paths, order, inodes and SHA-256 for both lower parent layers remained identical. The object store gained only a 77,824-byte commit and a 441-byte image descriptor. Edge restarted, loaded the published directory and created new instances.
- The first child VM ran the added program and returned 42. The temporary file was absent. After overwriting the program in this instance, a new parent VM saw neither the program nor the temporary file. A second child VM still returned 42.
- All four distinct instances reached STOPPED and reported reclaimed resources. The host's set of ublk block devices returned to its pre-acceptance state. Temporary etcd and the private object store were reclaimed. Raw results are retained in workspace directory `.runtime/pack-overlaybd-20261124/report.json`.

These results cover a single-host local object store and one small added dependency. The delta size includes ext4 metadata and block format overhead and cannot be extrapolated to other workloads.

<a id="2026-11-24-task-026-两个-node-的制品交付与实例恢复"></a>

## 2026-11-24 TASK-026 Delivering artifacts across two Nodes and recovering an instance

The core wheel SHA-256 was `990d6638cad17782bdbba9dcd48cdcb472a3f93375976ec4e7e5cf1dce6d4076`. Two independent Edge processes ran on one Linux/KVM host, each with its own directories, objects and leases. Two NodeGateways shared the control-plane process and used real etcd and the loopback HTTPS SDK.

1. A previously approved delta produced an ext4 child environment containing `/opt/dsec-tools/answer`. The installed publisher exported the manifest, imported it at a fresh path and rebuilt B's deployment entry. The deployment digest changed and the artifact version matched. The source object store and temporary transfer copies were removed before Edge started.
2. A advertised an old environment of the same name and B the requested new version. A nonexistent version returned NoPlacement and no IAM admission record for that request. Only B created the requested version. Retrying with the same request ID returned the same sandbox ID. A separate deterministic placement regression checked priority when the old node was less busy.
3. The new VM ran the approved program, returned 42 and wrote a temporary guest file. The public SDK paused the instance to PAUSED and resumed it on B. The original file content was read back and the program again returned 42.
4. The instance reached STOPPED and reported completed physical cleanup. IAM admission was released and both Nodes' physical lease lists were empty. Both temporary Edge instances, their object stores and etcd were retired. The first acceptance probe accessed the Edge socket before creation. We corrected its wait condition and cleaned the temporary directory from the attempt, which had created no VM.

Installed-package final results are in workspace directory `.runtime/task026-20261124/final-report.json`. The live run used direct local ext4 root disks. Separate publisher regressions cover OverlayBD layer order, rebinding capability declarations, restoring from different paths and rejecting corruption. Two physical hosts, remote lazy 3FS configuration and cross-host migration of running instances were outside this run.
