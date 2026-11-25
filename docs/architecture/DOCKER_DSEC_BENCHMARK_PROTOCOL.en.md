<a id="docker-与-dsec-对照发布版验收口径"></a>

# Docker and DSec comparison protocol for release acceptance

**English** · [简体中文](DOCKER_DSEC_BENCHMARK_PROTOCOL.md)

> Historical record: references marked “historical source not included”
> refer to experiment files outside this repository. They are not runnable
> instructions for the current release. Use the [documentation index](../README.md) for supported entry points.

Measure the capabilities and costs of the reproduced system. Use the same tasks, task revisions, image identities, agent/model parameters, verifier, timeouts and concurrency budgets on both backends. Record task ID, trial, environment catalog digest, runtime version, cache condition and raw logs for every pair. Keep invalid verdicts and timeouts in the denominator.

| Stage | Metrics to report separately |
| --- | --- |
| Artifact preparation | Wall time for download, conversion/unpacking, publication and verification. Unique physical bytes and peak temporary space. |
| Scheduling and startup | Queue reason and wait time, time from lease to first executable command, preparation and idle cost of a warm pool. |
| Workload | Command and model API times, throughput, success rate, valid-score rate, tokens and cost. |
| Backend resources | CPU seconds, peak and time-integrated memory, physical reads and writes, network bytes and allocated disk blocks across the full scope. |
| Recovery and isolation | Pause and resume time, state continuity after failure, concurrency isolation and failure scope. |

For Docker, include the containers, daemon, image store and shared verifier services. For DSec, include the VMM, sandboxd, worker, OverlayBD/ublk, FUSE/3FS when enabled, resident guest memory, page cache, read-only layers and private writable layers. Report batch increments first for shared services. Attribute them to a backend only after isolating its scope. VMM PSS and Docker cgroup `memory.current` measure different things and cannot form a meaningful ratio. Adding ublk logical bytes to underlying physical I/O double-counts work. Record the sample interval, scope identity and missed samples for each peak. Read cgroup `io.stat` during execution and retain raw counters by device number and stage. A guest `fsync` need not have charged dirty host backing-file pages to physical `wbytes`. Complete host writeback before taking the baseline and again after the workload before collecting stable samples. Report preparation, first boot, work and cleanup deltas separately so delayed writeback is neither lost nor attributed to the next stage. Use systemd's unit-exit summary only as a diagnostic.

Start with a fixed command trace to check semantics and accounting, then run paired episodes on the same task before sweeping concurrency. The main comparison uses built artifacts and equivalent cache conditions. Report cold preparation, remote 3FS, DAX, ready pools and OverlayBD demand reads as named conditions with their preparation and resident costs. Report p50/p95, the full distribution and raw results. Do not attribute an earlier mixed host-wide peak to one backend. TB2.1 is one workload. Include at least one non-TB2 workload to test the generality of the interface.

Earlier evidence and limitations are in the TB2 comparison (`TB2_DOCKER_DSEC_COMPARISON.md`, historical source not included), resource accounting (`TB2_RESOURCE_ACCOUNTING.md`, historical source not included) and general DAX comparison (`GENERIC_DAX_DOCKER_SCOPE_REPORT.md`, historical source not included).

<a id="2026-10-04-实验机门禁"></a>

## Experiment-host prerequisites on 2026-10-04

Read-only preflight `experiments/openenv_api/benchmark_scope_preflight.py` ran on the experiment host. See `results/benchmark-preflight/host-20261004.json` for the raw record. `docker.service` served nine resident containers, including 3FS, monitoring and earlier comparison containers. The user-level cgroups of `dsec-sandboxd.service` and `dsec-elastic-rollout-worker.service` lacked `io.stat` because the I/O controller had not been delegated to the user slice. System-level `dsec-elastic-ublk.service` had its own `io.stat`. The existing services therefore supported functional and per-sandbox diagnostics but did not meet the prerequisite for paired full-backend physical I/O measurement.

The next pilot requires a separate Docker daemon (the E2 DinD script provides a starting point) and isolated system scopes with I/O accounting for the test DSec daemon and worker. Preserve production cgroups and treat unavailable user-slice readings as missing. Verify matching source images, task layers and full scopes for `regex-log`, `log-summary-date-ranges`, `install-windows-3.11` and `reshard-c4-data`, then run bursts under cold Docker, cached Docker and remote on-demand EROFS. The remote EROFS artifacts were published for all four tasks. The other three VM paths and full service attribution still needed validation. Local EROFS reads do not establish remote access.

`regex-log` passed the networkless functional smoke test using remote EROFS (`TB2_2_1_REMOTE_EROFS_PILOT.md`, historical source not included). Remote artifacts for all four tasks were subsequently published and hash-checked. All four networkless VM smoke tests passed. The official verifier, concurrency and complete resource attribution still awaited acceptance.

`experiments/openenv_api/probe_system_io_scope.sh` is a one-shot permission and accounting probe for the system scope. It uses `sudo systemd-run --system --wait --collect` to run Python, reading only its own cgroup files. The temporary unit is released on exit. Success establishes that the test process can obtain I/O counters. A later real-VMM probe in the same scope used host `fsync` and a 4 MiB calibration to establish that raw `io.stat` accounts for physical writes. Results and stage boundaries are in the isolated-scope pilot (`TB2_2_1_ISOLATED_SCOPE_PILOT.md`, historical source not included). That probe excludes separate sandboxd/worker, OverlayBD/ublk and 3FS server costs. The independent Docker daemon passed empty-store, manifest/config identity and single-task cold-pull/cached pipeline checks. See the isolated-scope pilot (`TB2_2_1_ISOLATED_SCOPE_PILOT.md`, historical source not included). Relative gains require DSec measurements at the same accounting boundary.

Docker and DSec completed matching fixed trajectories using the same images for four TB2.1 tasks. Three tasks completed official verification on both sides. The fourth lacked an offline dependency. Timings, available resource samples and accounting limits are in the four-task paired pilot (`TB2_2_1_FOUR_TASK_PAIRED_PILOT.md`, historical source not included).

<a id="2026-10-06-完整后端成本-r4"></a>

## Full backend cost, r4, 2026-10-06

The physical-host comparison passed using the frozen package, two tasks, fixed command traces and named offline dependency conditions. Docker's private dockerd/containerd had independent mount and network namespaces. DSec included separate daemon, worker, OverlayBD/ublk and VMM. Accounting covered each backend's complete cgroup subtree. Both passed the 4 MiB physical NVMe write calibration. Three fresh episodes per task per backend made 12 runs, with another three forks from a prepared DSec baseline. All 15 scored 1. Episode time includes creation, actions, official verification, writeback and cleanup. Backend and baseline preparation are reported separately. The scripts and local raw evidence are in `experiments/release_pair/` and `.runtime/release-pair-r4/`.

| openssl-selfsigned-cert condition | Median full episode wall time, last two runs |
| --- | ---: |
| Fresh Docker container, cached artifacts | 3.960 s |
| Fresh DSec sandbox, cached artifacts | 5.664 s |
| DSec prepared-state fork | 5.008 s |

At the same DSec SDK reset boundary, the medians were 1.576 s for fresh creation and 0.847 s for a prepared-state fork. Preparing the baseline took a further 5.310 s and wrote about 2.00 GiB physically. Three forks did not amortize that cost. Docker create_s includes verifier mount preparation and differs from the DSec SDK reset boundary. Neither figure supports a direct VMM startup comparison.

Backend preparation took 15.155 s for DSec and 1.677 s for Docker. DSec physically read 17,219,198,976 B in preparation. At sandboxd startup, `VerifierArtifactStore.__init__` invoked `resolve('local')`. The first resolution hashed the entire logical 22 GiB ext4 tools disk with SHA-256. Afterward, a mincore-only inspection of that file found 22,996,107,264 B (21.42 GiB) resident, without another read or cache eviction. The high usage in the raw samples was predominantly file-backed. Full hashing breaks the strictly on-demand startup condition. These results cannot establish its benefit, and the host file pages must not be attributed to VMM anonymous memory. At the time this filesystem lacked a validated fs-verity path. Any optimization must retain integrity checks. Filename or stat alone is insufficient.

Host continuity and resource cleanup passed. Failed r1/r2/r3 trials were excluded from the conclusions. This round did not exercise remote 3FS, DAX, the ready pool or model calls. The fixed order and two-task sample do not establish performance for the full suite, large concurrency or a lower overall cost for DSec.

<a id="2026-10-06-本地工具盘-r5"></a>

## Local tools disk, r5, 2026-10-06

A later test validated optional integrity verification on a separate verity-capable volume, preserving the root filesystem. Three protection checks, cold resolution, all 15 scores, full-backend accounting and cleanup passed. DSec preparation fell from 15.155 s to 0.928 s and its sampled peak in that stage fell from 22.184 GiB to 0.145 GiB. Full episodes still took longer than Docker. See the [r5 report](../reports/DSEC_VERITY_PILOT_REPORT.en.md) for one-time publication cost and comparison limits. The r4 text remains as historical evidence. The local tools-disk improvement has not been demonstrated with remote 3FS or DAX.
