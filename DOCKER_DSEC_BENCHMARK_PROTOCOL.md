# Docker 与 DSec 对照：发布版验收口径

目标是验证可复现系统的能力与成本，而非预设 DSec 在所有指标上优于
Docker。使用同一任务集合、任务修订、镜像身份、agent/model 参数、
verifier、超时和并发预算。每次配对记录任务 ID、trial、环境目录摘要、
运行时版本、缓存条件与原始日志；失效 verdict 和超时保留在分母中。

| 阶段 | 必须分开记录的指标 |
| --- | --- |
| 制品准备 | 下载、转换/解包、发布与校验墙钟时间；唯一物理字节、临时峰值字节 |
| 调度与启动 | 排队原因/时长、租约到首命令可执行、预热池准备与空闲成本 |
| 工作 | 命令与模型 API 分段耗时、吞吐、成功率、有效评分率、token/费用 |
| 后端资源 | 完整 scope 的 CPU 秒、内存峰值与积分、物理读写、网络字节、分配磁盘块 |
| 恢复与隔离 | 暂停/恢复耗时、故障后的状态连续性、并发隔离与失败范围 |

Docker 侧应包括容器、daemon、镜像存储和 verifier 共享服务；DSec
侧应包括 VMM、sandboxd、worker、OverlayBD/ublk、FUSE/3FS（若启用）、
guest 实际驻留内存、页缓存、只读层和私有写层。共享服务先报告整批
增量，只有隔离 scope 后才能归因到后端。VMM PSS 与 Docker cgroup
`memory.current` 不可直接相除；ublk 逻辑字节也不能与底层物理 I/O
相加。峰值必须有采样间隔、scope 身份及丢样记录。
物理 I/O 使用运行中直接读取的 cgroup `io.stat`，按设备号及阶段保存
原始值。guest `fsync` 不保证宿主 backing file 的脏页已记入物理
`wbytes`；准备阶段先完成宿主写回并采基线，工作结束后再完成写回及
稳定采样。准备、首启、任务、回收阶段的增量分别报告，不能把延迟
写回漏掉或误记到下一阶段。systemd 的 unit 结束摘要只作辅助诊断。

先跑固定命令轨迹验证语义和计量边界，再跑同任务配对 episode，最后
扫描并发。主对照为两端均已构建制品、同等缓存状态；冷准备、远程 3FS、
DAX、ready 池、OverlayBD 按需读取分别作为具名条件，并计入其准备
与常驻成本。报告 p50/p95、完整分布及原始结果，不能把先前混跑的整机
峰值回填为某个后端。TB2.1 是其中一个工作负载；还需至少一个非 TB2
工作负载证明系统接口的通用性。

既有证据与已知边界见 [TB2 对照](TB2_DOCKER_DSEC_COMPARISON.md)、
[资源计量](TB2_RESOURCE_ACCOUNTING.md) 和
[通用 DAX 对照](GENERIC_DAX_DOCKER_SCOPE_REPORT.md)。

## 2026-10-04 实验机门禁

只读预检脚本 `experiments/openenv_api/benchmark_scope_preflight.py` 已在实验机
运行，原始记录见 `results/benchmark-preflight/host-20261004.json`。当前
`docker.service` 同时服务 9 个常驻容器，包含 3FS、监控和旧对照容器；
用户级 `dsec-sandboxd.service` 与 `dsec-elastic-rollout-worker.service`
的 cgroup 没有 `io.stat`，因为用户 slice 未获 I/O 控制器委派。系统级
`dsec-elastic-ublk.service` 有独立 `io.stat`。因此现有服务可继续做功能
与逐沙箱诊断，但不满足完整后端物理 I/O 配对门槛。

下一轮 pilot 须使用独立 Docker daemon（现有 E2 DinD 脚本可作基础），
并把测试用 DSec daemon/worker 放进有 I/O accounting 的独立系统 scope；
不得修改生产服务的 cgroup 或把用户 slice 的缺失读数填 0。然后对
`regex-log`、`log-summary-date-ranges`、`install-windows-3.11` 和
`reshard-c4-data` 核验同源镜像、任务层与完整 scope，再运行冷 Docker、
已缓存 Docker、远端按需 EROFS 三组突发。第三组的四任务 TB2.1 远端
EROFS 制品已发布；还须验证其余三任务的 VM 路径和完整服务归属，
不能用本地 EROFS 冒充远端读取。

目前 `regex-log` 已完成
[远端 EROFS 无网络功能烟测](TB2_2_1_REMOTE_EROFS_PILOT.md)；四任务远端
制品均已发布和哈希校验，四任务无网络 VM 功能烟测均通过。官方
verifier、并发和完整资源归属仍未过门禁。

`experiments/openenv_api/probe_system_io_scope.sh` 是上述 system scope 的
一次性权限与计数探针：它通过 `sudo systemd-run --system --wait --collect`
执行一个只读取自身 cgroup 文件的 Python 进程；临时 unit 结束即回收。
探针成功只证明测试进程能获得 I/O 计数，并不代表完整 DSec 后端已隔离。
后续真实 VMM 同 scope 探针已用宿主 `fsync` 与 4 MiB 校准证明原始
`io.stat` 能计入物理写入；结果和阶段边界见
[隔离 scope pilot](TB2_2_1_ISOLATED_SCOPE_PILOT.md)。这个探针尚未包含
独立的 sandboxd/worker、OverlayBD/ublk 与 3FS 服务端成本。
Docker 独立 daemon 的空 store、manifest/config 身份核对与单任务
冷拉取/已缓存管道已通过，详见
[隔离 scope pilot](TB2_2_1_ISOLATED_SCOPE_PILOT.md)；它尚不能与
没有相同资源边界的 DSec 数据计算收益百分比。

TB2.1 四任务的同镜像固定轨迹已完成 Docker/DSec 配对；其中三任务
两侧完成官方 verifier，第四项缺离线依赖。时长与现有资源采样的
原始结果及其计量边界见
[四任务配对 pilot](TB2_2_1_FOUR_TASK_PAIRED_PILOT.md)。

## 2026-10-06 完整后端成本 r4

冻结安装包、两种任务、固定命令轨迹与具名离线依赖条件的实机配对已通过。
Docker 独立 dockerd/containerd 同时隔离 mount/net，DSec 含独立
daemon/worker、OverlayBD/ublk 和 VMM；各端完整 cgroup 子树计量，物理
NVMe 写入 4 MiB 校准通过。三个新建 episode/任务/后端共 12 次，另有
DSec 准备态基线的 3 次分叉，共 15 次评分均为 1。创建、动作、官方
verifier、写回和回收均计入 episode；后端与基线准备另计。实际脚本与
本地原始证据位于 `experiments/release_pair/` 和 `.runtime/release-pair-r4/`。

| openssl-selfsigned-cert 条件 | 后两次完整墙钟中位数 |
| --- | ---: |
| Docker 新建、制品缓存 | 3.960 s |
| DSec 新建、制品缓存 | 5.664 s |
| DSec 准备态分叉 | 5.008 s |

DSec 同一 SDK reset 边界下，新建中位数 1.576 s、准备态分叉 0.847 s。
准备基线另用 5.310 s 并产生约 2.00 GiB 物理写入；仅 3 次分叉没有摊回
准备成本。Docker create_s 包含 verifier 挂载准备，不能将该字段直接与
DSec SDK reset 宣称为相同边界的 VMM 启动对照。

本轮 DSec 后端准备 15.155 s，Docker 1.677 s。DSec 准备阶段物理读取
17,219,198,976 B；`VerifierArtifactStore.__init__` 在 sandboxd 启动时调用
`resolve('local')`，首次解析全文 SHA-256 逻辑大小 22 GiB 的 ext4 工具盘。
结束后仅用 mincore 检查该固定文件，驻留 22,996,107,264 B（21.42 GiB），
没有再次读取或清缓存；原始采样的高占用主要为 file。此边界破坏了严格
按需读取的启动条件，不能把结果当作已经证明该条件的优势，也不能把
这些宿主文件页归为 VMM 匿名内存。当前文件系统不支持已验证的 fs-verity
路径；后续优化必须保留完整性保证，不能以文件名/stat 直接跳过全文校验。

宿主不受影响与资源回收门槛通过；原 r1/r2/r3 失败轮不参与结论。本轮
不含远端 3FS、DAX、ready 池或模型调用；固定顺序、两任务小样本不能
替代全量任务和大并发评测，也不预设 DSec 总成本低于 Docker。

## 2026-10-06 本地工具盘 r5

后续已在独立支持 verity 的卷上完成可选完整性路径验收，未修改根文件
系统。三项保护检查、冷解析、15 次评分、完整后端计量及回收均通过。
DSec 准备从 15.155 s 降到 0.928 s，该阶段采样峰值从 22.184 GiB 降到
0.145 GiB；完整 episode 仍比 Docker 慢。一次性发布成本和对照局限见
[r5 报告](DSEC_VERITY_PILOT_REPORT.md)。r4 正文保留为历史结果，不把
本地工具盘收益外推至远端 3FS 或 DAX。
