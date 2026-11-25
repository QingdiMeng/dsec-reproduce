<a id="本地工具盘按需完整性与-dockerdsec-配对验收"></a>

# On-demand integrity for a local tool disk and paired Docker/DSec acceptance

**English** · [简体中文](DSEC_VERITY_PILOT_REPORT.md)

The installed r5 candidate passed on physical hardware on 2026-10-06. Startup no longer read the entire 22 GiB verifier tool disk to resolve its identity, and integrity checks remained enforced. The Docker/DSec timing gap persisted for complete episodes. This test did not publish the candidate or replace existing services.

<a id="验收对象和方法"></a>

## Configuration and method

The core package was `0.1.0.dev0`, wheel SHA-256 `11b2df99f8e305c332da1dd8d2b7adce6a3a25a1d2866adea6875c296e7eef5b`. An independent source rebuild, 49 core tests in a fresh virtual environment, checksum checks on 55 runtime files and 27 preparation-stage regressions passed. The guest fixture and optional TB2.1 package were reused. This run did not rebuild the guest or rerun short GRPO training.

The tool disk retained digest `a487ff4cbdac2c2193ff4c451c0e33e3683bcce1b9ef1909a831b9e7a33da490` and logical size 23,622,320,128 B (22 GiB). The administrator sealed the same bytes, confirmed the original SHA-256 and published a protected receipt. At runtime the system checks that receipt against the kernel digest. The kernel verifies data pages as they are read. A filename, stat result or cache hit cannot substitute for integrity verification.

To establish the cold condition, fadvise was applied only to this experiment's new file and its loop backing file. Root verified that no data pages were resident. This was a measurement condition, not a runtime cache-reclamation feature.

<a id="完整性和一次性准备"></a>

## Integrity checks and one-time preparation

All physical checks passed. Writes to the sealed file were rejected. An unsealed copy with identical bytes failed resolution. After data on an independent, unmounted test volume was corrupted, a read after remount returned EIO. The corruption test preserved the real tool disk and all previous artifacts.

Copying the data, building the tree and confirming the full digest took **43.556 s**. The separate volume used **17,461,133,312 B (16.26 GiB)** of physical space for a logical volume size of 26 GiB. Retaining old artifacts for independent acceptance consumed extra disk space. A production deployment need not retain two copies for this reason. Root filesystem features were preserved. After success the new volume was mounted read-only, with no fstab entry or service installation.

With cold input, a fresh non-root process resolved integrity in **0.516 ms**. The process reported `read_bytes=0` and **0** tool-disk data pages remained resident. The interval measures integrity resolution only. It does not measure VM startup.

<a id="完整后端成本"></a>

## Complete backend costs

Both backends used the same fixed trajectories, offline initialization and read-only tool disk. Each ran two tasks three times, and DSec additionally ran three prepared-state forks. All 15 scores were 1. Accounting included helper services throughout the cgroup subtree. Sampling at 250 ms reported no errors, and a calibration check confirmed physical NVMe write accounting.

The table compares the same DSec stage across revisions. r4 read the full digest. r5 used a new protected volume, which also changed the storage layout. This comparison does not isolate a single variable.

| DSec backend preparation | r4 | r5 |
| --- | ---: | ---: |
| Wall time | 15.155 s | 0.928 s |
| CPU time | 12.989 s | 0.497 s |
| Sampled peak memory | 22.184 GiB | 0.145 GiB |
| Physical NVMe reads | 17,219,198,976 B | 87,904,256 B |

The kernel-reported memory peak for the entire DSec batch fell from 24.402 GiB to 2.749 GiB. DSec additionally prepared a baseline and executed three forks, so its batch peak cannot be paired with Docker's as an equal-workload measurement. For equal numbers of fresh episodes, sampled peaks were **0.681 GiB** for Docker and **0.771 GiB** for DSec. DSec peaked at **2.737 GiB** during the prepared-fork stage, including the retained baseline. The test did not attribute the baseline cache to individual files.

For `openssl-selfsigned-cert`, the following table reports median wall time over the last two complete episodes.

| Condition | Complete episode | Median SDK reset |
| --- | ---: | ---: |
| New Docker container, cached image | 3.932 s | Different timing boundary from DSec reset |
| New DSec sandbox, cached artifacts | 5.624 s | 1.511 s |
| Fork from prepared DSec state | 4.968 s | 0.850 s |

Reset medians also use the last two runs. Preparing the baseline separately took **5.305 s** and incurred **2,149,896,192 B** of physical writes. Three reuses did not amortize that preparation. The data do not establish superior overall throughput or resource cost against Docker. Removing full-file verification primarily reduced backend preparation cost. Warm-episode wall time remained close to r4.

<a id="宿主边界与回收"></a>

## Host boundaries and cleanup

The real host correctly rejected mount/network helpers invoked in the wrong context and a root ublk service with the wrong umask. Host Docker/containerd PIDs, original container PIDs, the docker0 address, MAC and ports, and Grafana health remained unchanged. Grafana remained reachable from the Mac.

All 43 sandbox records were STOPPED, with zero leases and pending requests. No network namespaces, acceptance sockets or active device/process references remained. Two historical FAILED worker records retained no resources. Old network_slot strings were historical labels; the namespaces no longer existed. About **79.84 GiB** of disk space remained. The test instance services were stopped. The persistent services were not upgraded to the candidate.

<a id="证据与发布边界"></a>

## Evidence and release scope

Raw evidence is in `.runtime/release-pair-r5/evidence/`. `verity-pilot-r1/result.json` records sealing and cold resolution. `pair-cost-r5/results/` contains official scores, raw samples, deployment rejections and host checks. `pair-cost-r5/report/` records complete stage costs. The independent final audit is in `.runtime/release-pair-r5/report/final-audit.json`. The r4 evidence and failed runs were preserved.

This run verified the local verifier tool disk only. Its optional configuration was integrated into the installable core package. Generic EROFS resolution retained its existing path. Remote 3FS, DAX, the ready pool, model APIs and a new GRPO regression were outside this run. Two tasks and three repetitions in fixed order do not establish an advantage over the full suite or at high concurrency, or coverage of every production crash mode.
