# DSec 弹性计算系统：实验机制到运行时能力

当前安装候选为 source-release-r5：49 项核心回归、55 个安装文件摘要、
本地工具盘 fs-verity 三项真实保护检查、15 次固定轨迹评分及宿主回收
均通过。冷解析不读数据，后端准备 0.928 s；完整 episode 仍慢于 Docker，
不宣称总体性能优势，见 [r5 报告](DSEC_VERITY_PILOT_REPORT.md)。最新
候选的短 GRPO 与准备态分叉回归已通过：评分 [1,0]/[0,0]，首步非零
梯度、96 个 adapter 张量变化，四次真实恢复和跨 episode 隔离通过，
50 条沙箱记录全部停止且无资源残留，见
[最新训练验收](DSEC_V01_GRPO_ACCEPTANCE.md)。下文原训练及安装结果
保留历史版本归属。安装不引入 TB2.1 数据、模型或 GPU 训练补丁。

## 历史阶段记录

以下保留各轮当时的状态与边界；最新发行验收以上面的具名候选为准。

2026-10-06 单机 v0.1 发行入口收敛中：任务与 Miles 插件迁入可安装的
`dsec_adapters` 包，旧路径仅作兼容别名。`dsec-host` 提供统一配置、依赖诊断、
服务生成、就绪等待、验收与受限网络权限安装包生成；安装版不依赖实验目录
或 `PYTHONPATH`。实验机独立 venv 的非 TB 任务及运行中/暂停后双服务重启
已通过，评分 1、动作去重和租约释放通过。TB2.1 的安装版首次回归暴露 ublk
旧写目录白名单和 guest 启动必须有虚拟网卡两项部署条件，失败证据保留；
已补上存储组只搜索目录规则和可审查的独立 netns 安装入口。管理员安装后，
独立 EROFS/OverlayBD/netns 实例的官方 TB2.1 评分为 1，运行中与暂停后双服务
重启亦通过；冻结 wheel 的自动监督保留 worker/VMM 并通过非 TB 评分与回收。
见[安装验收](DSEC_V01_INSTALL_ACCEPTANCE.md)。短 GRPO 冻结版本复核通过：
Qwen3.5-4B thinking 两组 × 两样本，评分 `[0,1]` / `[0,0]`，两次训练切换、
TITO、四次实际快照恢复及回收通过；第一组非零梯度，第二组零梯度，
不作学习收益结论。安装版跨 episode 准备态分叉复核亦通过：新 history、
私有写入、官方评分 1、服务重启、源删除后恢复和最终共享对象回收通过；
两个当前临时实例的 15 条沙箱记录全部停止，临时服务关闭，原正式服务正常。
当前仍为开发发行包，
项目自身 MIT 与 Miles 派生部分 Apache-2.0 已落地，新候选 wheel 的运行代码
与功能冻结版本相同；新 venv 安装及许可边界核对通过。完整后端 Docker
对照尚待完成，见[两天收敛计划](DSEC_V01_RELEASE_PLAN.md)。源码交付审查与
新主机依赖说明现已收敛：白名单源码包在实验机离线重建两个发行包，
新的非 TB guest 评分与重启恢复通过；Linux 33 项核心、5 项应用测试通过。
可选应用校验 89 个任务文件，代表任务的已准备 OverlayBD 路径及新转换
EROFS/file-ext4 路径官方评分均为 1；跨 episode 隔离与共享回收通过。
本轮修复 mixed-root 实例中 setgid 继承导致 file-ext4 启动拒绝的问题，
唯一运行代码变化为 `sandbox_sdk.py`，历史 GRPO 证据仍对应原版本。
全部临时沙箱和租约已释放；具体部署路径见 [QUICKSTART](QUICKSTART.md)。

2026-10-06 准备态 episode 分叉扩展完成共享 lower 与并行恢复优化：正式 SDK/worker/daemon 上同一 TB2.1 基线生成两轮 × 两个独立 episode，官方 verifier 1 分、独立写层、源删除后子分支暂停恢复与最终共享对象回收通过。四分支 reset 可用时间 0.92–1.15 s，首版端到端 2.06–4.07 s；两路全部就绪 1.08–1.16 s，本轮冷创建 1.51 s，一次性封存 3.51 s。该小样本证明消除了长源锁和逐分支准备层复制；不证明 60 ms 或大规模吞吐。保持显式选择，默认创建路径不变；完整准备态内存分叉属于系统扩展，不能称为论文原样提供的 pack_diff API。受控 daemon/worker 重启后，原分支状态延续、封存基线继续分叉及源删除后的暂停恢复亦通过；重启后首次完整基线校验保留（1.399 s）。已修复 worker 三个基线字段漏恢复，正式版本为 `release-episode-fork-cow-r2`。详见 [接口与验收边界](AGENT_ENVIRONMENT_CONTRACT.md)。

更新：2026-10-06。TB2.1 是验证负载，不是系统边界。系统入口是 libdsec 风格 SDK 与常驻 rollout worker；任务只提供环境 ID、资源需求、执行动作与评分器。环境、存储和运行时能力必须在沙箱创建前确定，并在 rollout 生命周期内保持不变。

发布边界：DSec 核心及统一框架接入接口使用默认 `runtime` 包。Miles 的 CPU/GPU 内存优化、固定镜像补丁、模型适配与 GPU 探针只进入显式 `training-experiment` 包；下文训练结果是接入证据，不计入 DSec 系统功能或发布完成度。沙箱自身的 DAX 共享读取与资源调度仍属于 DSec 运行时能力。

统一 [AgentEnvironment 协议](AGENT_ENVIRONMENT_CONTRACT.md)已部署到正式 worker：非 TB2 计数任务、Miles 确定性回调、客户端重连、步骤完成后受控 worker 重启、动作去重和租约释放均通过。TB2.1 的独立 netns 路径已用同版完整发行包和系统 Python 跑通官方评分及证据保存，不依赖 OpenEnv；89 个任务现已注册到正式通用 daemon，完成制品与配置校验；正式 SDK 的真实创建、重连、官方评分与证据保存已在一个任务上通过。真实 Qwen3.5-4B thinking 的两轮推理→训练→恢复推理已在隔离入口通过，取得正分、有限非零梯度和 adapter 更新；Miles 单 rank NCCL 卸载修复已提交草稿 [PR #3914](https://github.com/radixark/miles/pull/3914)。真实训练调用随后已切到正式 SDK 入口，两次正分、TITO、梯度/参数变化和恢复推理均通过，详见 [正式训练记录](AGENT_ENVIRONMENT_CONTRACT.md#真实-miles-训练接到正式入口2026-10-05)。正式入口的 `openssl-selfsigned-cert` 已由真实 4B 模型取得官方 reward=1，4B 曾在长样本训练 loss 阶段显存不足；分块重计算修复后，Qwen3.5-2B 已经正式入口完成两轮训练及恢复推理（官方有效 0/0 分、零梯度，未发生学习更新），完整 TITO、checkpoint 与资源释放审计通过。随后切回 4B 的训练实验曾在输出层整份 logits 分配处 OOM，分配器实验未通过并已撤回；最新分块投影试验取得官方 1 分，但在输出层之前的 unfused attention 处 OOM；FA2 后端验证后，4B 已经正式 DSec 入口完成同一真实 TB2.1 任务两轮正分训练：reward=1/1、梯度非零、两 checkpoint 的 192 个 adapter 张量变化，exit=0，用时 441.612 秒。保留 thinking 和 32768 回复上限，实际 response 为 4657/2729 token；此项是 Miles 接入验收，不证明学习收益或 89 题全部通过。

生产 `sandboxd`、worker 与依赖源码已按[运行时版本收敛](ELASTIC_RUNTIME_CONVERGENCE.md)统一部署。通用目录驱动 microVM 的每次创建可选本地/3FS EROFS 层与 file-ext4/OverlayBD＋ublk 根盘；四种组合均在常驻 rollout worker 上完成真实创建、执行、暂停恢复与停止。独立 root 级 ublk 服务已安装，未重启原 ublk 服务。通用目录还可为固定哈希、2 MiB 对齐的本地只读 EROFS 层声明 DAX 与专用 VMM；生产本地/3FS 配套层、`dax=always` 挂载及双服务重启恢复均通过。重复完成步骤未再次执行。[两 VM 对照](GENERIC_DAX_PAIR_REPORT.md)证实第二台读同一 256 MiB 时，DAX 组 VMM 合计 PSS 仅再增 0.35 MiB，块设备组再增 257.0 MiB。[同文件两实例 Docker 对照](GENERIC_DAX_DOCKER_SCOPE_REPORT.md)进一步测得 DSec 新增宿主内存 395.4 MiB、Docker 264.2 MiB；[逐进程归因](GENERIC_DAX_READY_MEMORY_REPORT.md)显示差额主要来自两台 VM 实际驻留的匿名 guest RAM 页合计约 110 MiB、隔离 sandboxd 的约 16 MiB 匿名页，而非重复 DAX 文件页。下调 guest 上限或仅开启零大小 FPR balloon 未显著降低这部分驻留。完整 TB2.1 成本结论仍待测。通用环境目录现对本地制品每 daemon 只做首次全量哈希，创建时复核文件身份；daemon 冷启动仍会完整哈希。通用 guest 命令上限现由环境目录固定，默认 30 秒，防止超限请求变成未知结果。最近一次服务检查中 `sandboxd`、worker 和两个 ublk 服务均正常。后续重点是制品发布与冷启动校验、异构大镜像突发创建对照、E4 放置策略及非计划故障。

[启动内存候选试验](GENERIC_GUEST_MEMORY_OPTIMIZATION_REPORT.md)进一步否定了两项捷径：精简当前内核只让两 VMM 匿名 PSS 少约 0.3 MiB；直接复用完整启动快照虽使两 VMM PSS 从约 105 MiB 降到 25 MiB，但 512 MiB 快照文件使同 scope 总内存升至约 544 MiB，且克隆 agent 状态重复。两项均未部署；已删除对应大文件。不能只凭进程 PSS 推广共享完整快照。

[通用制品发布器](ARTIFACT_PUBLICATION.md)已实现内容寻址、发布时校验、OverlayBD 根盘引用重写及目录原子提交，并用隔离真实 VM 验证本地发布路径。3FS 失联时目录远端 `stat` 可能卡死，已改用有界子进程探针。磁盘清理后 3FS 对象读取恢复，该修复和发布器随完整 84 文件运行时部署到生产；暂存版本的 3FS VM 双服务重启，以及生产本地/3FS DAX＋OverlayBD 两组暂停恢复通过。生产目录尚未添加冗余演示环境，daemon 冷启动本地全文哈希仍未解决。

## 已接通的通用环境路径

`environment_catalog.py` 定义内容固定的环境目录。任意合法环境 ID 可指向两种容器根文件系统：

- `erofs_layers`：按目录声明有序的 1–17 个只读 EROFS 层，运行时将其挂成 OverlayFS lowerdir，并为每个沙箱创建私有 upperdir。每层可选择本地文件或 3FS FUSE 上以 SHA-256 命名的不可变文件；远端完整摘要在发布时校验，创建时只检查活挂载和大小，以保持按需读取。对应 E1 的共享制品与 E2 的远端数据源发现，不再把 `base/workspace/toolkit` 三个名字写死在运行路径。
- `erofs_split`：本地元数据 EROFS 和外部数据 blob；同一内容摘要可以选择 `local` 或 `threefs_lazy`。对应 E2 的按需数据源选择。远端只验证活的 3FS FUSE 挂载、文件大小和制品引用；完整摘要在制品发布阶段验证，不能在每次沙箱创建时读取整个远端 blob。

目录中的 `runtime_image` 必须是 Docker SHA-256 镜像 ID，EROFS 层、元数据及本地数据也必须匹配 SHA-256。SDK 根据 `environment_id` 选择后端，状态返回实际挂载、私有 upper、存储来源、运行镜像与制品摘要探针。正在使用的目录内容变化后，旧客户端拒绝新建；新客户端也不能用变化后的目录附着旧沙箱。这是 E5 对恢复身份连续性的约束。

rollout profile 新增可选 `environment_id`，在 `environment=erofs_split` 或 `erofs_layers` 时固定目录 ID；`task_id` 继续表示训练/评测任务，两者不再绑定。`rollout_workerd.py` 的创建、重连和未知结果恢复路径均使用该 ID。旧 E1/E2 profile 格式保持可用。

## 真实系统验证

先在 `192.168.0.110` 的隔离代码目录 `openenv-api/generic-code` 和独立运行目录 `openenv-api/generic-container-runtime` 验证，再将六个通用组件部署到 `service-code`；原文件备份在 `service-code/backup-generic-20261003`。常驻 `sandboxd` 进程未重启，它使用的服务端代码在这次更改中未修改。部署后又以实际 `service-code` 路径启动双层环境并执行命令，结果通过：

- 任意 ID `general-smoke-env` 用 E2 制品走本地 `erofs_split`，真实容器挂载 EROFS + OverlayFS，命令成功，停止清理成功。
- 任意 ID `general-layered-env` 用 E1 三层制品、`general-two-layer-env` 用其中两层，均真实启动并执行命令；旧 `e1-real` 和 `e2-full` 本地路径的回归也通过。
- 通用 rollout 在新 worker 进程中重新附着原沙箱，保留写入的文件与步骤位置，随后停止。篡改目录文件后，同一客户端拒绝继续创建，新客户端拒绝附着旧沙箱。
- 部署后的通用双层环境也通过 worker 重连：同一沙箱、步骤位置与先前写入的文件均保持一致。
- 本地目录单元检查 4 项通过：摘要变化拒绝、非 3FS 挂载拒绝、可变层数和顺序、混合本地/3FS 层。

3FS 曾返回远程 I/O 错误，原有 `ready.json` 是过期标记。定位到 FoundationDB Ratekeeper 在根分区可用空间约 50 GB 时将事务配额降为 0（`WorstFreeSpaceStorageServer` 为负），管理租约无法续期，继而导致两副本离线。2026-10-03 将两份旧 microVM 内存快照压缩、校验 SHA-256 后回收原文件，根分区可用空间升至约 55.5 GB；随后归档旧 boot bundle 后升至约 62 GB。受控启动后管理租约恢复，两副本均为 `SERVING-UPTODATE`，FUSE 上 1 KiB 与 32 MiB 的已有文件读取及 SHA-256 校验通过。独立 FUSE 客户端已恢复，未重启 3FS 服务端。

在隔离代码目录进行真实共享层回归：`shared-three-layer-env` 和 `shared-two-layer-env` 均引用 3FS 上同一份 `base`（5,238,784 B）和 `workspace`（3,035,136 B）内容寻址 EROFS 对象；前者另有本地 `toolkit`。两次沙箱创建、EROFS lowerdir、OverlayFS 私有 upper、3FS FUSE 来源探针、命令执行与停止清理均通过。该结果证明跨环境引用同一对象可用，不代表已量测按需读取字节数或单机物理磁盘节省。已有本地原件仍保留。

相同回归随后以 `service-code` 实际部署路径再次通过。改动的四个组件备份在 `service-code/backup-threefs-layers-20261003`，运行中的常驻服务未重启；测试使用独立 Python 进程和独立容器运行目录。3FS 服务端和客户端在验证后仍运行，根分区剩余约 62.0 GB。

TB2.1 的 89 个任务有 595 个层条目，指向 319 个唯一 EROFS 层。其中 31 个公共层本身合计 1,946,959,872 B（约 1.95 GB），被不同任务引用 305 次；若每个引用它的任务各存一份，合计需 8,424,407,040 B。按各层实际任务数分别计算 `层大小 × (引用任务数 − 1)`，共享避免了 6,477,447,168 B（约 6.48 GB）重复副本。其余 288 个任务独有层合计 23,440,764,928 B，所有唯一层合计 25,387,724,800 B。595 个层条目中有两次是在同一任务内重复引用同一 4 KiB 层，因此按不同任务去重后的引用数是 593，不能直接把全部条目都视为独立任务副本。现有本地 `layers/` 池已经按层身份共享，因此迁移到同机 3FS 本身**不会再次节省这 6.48 GB**；若保留本地池还会增加占用，3FS 副本和缓存亦需单独计量。`erofs_split` 的整个 blob 目前没有跨任务块级去重，只有显式抽成共享层并按内容寻址发布时才获得这类复用。

## 尚需纳入系统的机制

| 实验发现 | 已有运行能力 | 系统化后续工作 |
|---|---|---|
| E1/E2：共享层与远端按需读取 | 通用容器环境目录、本地/3FS 可选数据源；旧固定制品实际运行通过；3FS 服务已恢复 | 验证同一目录的双存储 sandbox 路径；建立发布器、目录版本和每环境读取字节计量；异构环境突发负载量测 |
| E3：DAX 降低 microVM guest 重复缓存，FPR 有资源权衡 | 通用 microVM 目录可固定只读 EROFS DAX 层与 VMM；两 VM 同读证实消除第二份 guest 缓存，快照和重启恢复通过；同文件两实例宿主内存微基准下仍比 Docker 多 131.2 MiB | 建立 DAX 制品发布器和适用门槛；在完整 TB2.1 同任务对照中计入创建、服务与宿主成本 |
| E4：LS/BE QoS 在干扰下有效 | 容器固定 CPU 上可选 `SCHED_IDLE` / core cookie | 由资源调度器按负载分类与放置；扩到 VMM 线程，并量测 LS 尾延迟及 BE 损失 |
| E5：rollout 与沙箱双状态恢复 | worker 持久步骤、请求账本、容器重附着、microVM 快照 | 通用环境 ID 与制品版本随 rollout 冻结；持续验证故障恢复、不重放副作用及内存回收代价 |

`work_scheduler.py` 现已将整个沙箱生命周期作为租约边界接入常驻 `rollout_workerd.py`，并在 worker 重启时恢复持久占位。实验机使用用户级 systemd 服务运行，已验证异构环境排队、自动补位、重启恢复和停止释放。系统级 Prometheus 抓取与 Grafana 调度仪表盘已接通，详见 [弹性调度闭环](ELASTIC_SCHEDULER_INTEGRATION.md)。逐沙箱资源计量也已接通，容器使用专属 cgroup v2，microVM 由 sandboxd 按持久进程身份校验后代读 Firecracker PSS、CPU 与进程 I/O；实机两侧均已抓到非零内存指标。3FS 服务端和客户端的整服务 cgroup 内存/CPU/I/O 也已分别暴露；`threefs_lazy` 任务现在会在服务失联或采样过期时排队，本地任务继续执行。本地层任务期间 3FS 服务同样有活动，尚不可把共享增量按任务归因。共享成本归属和宿主级准入尚未完成；同机进程的 Docker 权限和旧 TB2 容器后端仍在当前门禁之外。完成这些后，再用 TB2.1、其他任务和 RL rollout 做同一系统的工作负载验证。

[调度版 SDK](SCHEDULED_DSEC_SDK.md)已部署到正式 worker。标准应用入口现在通过 worker 创建、执行、暂停、恢复和停止，并提供稳定 rollout/action ID 的未知结果处理；本地与 3FS 的真实通用 microVM 各完成暂存和正式两轮验证。验证后没有活跃或等待租约，108 条沙箱记录均为 `STOPPED`。当前实验机根分区剩余约 94 GiB。`fs-verity` 的内核支持虽存在，但当前根文件系统对新文件启用 verity 返回 `ENOTSUP`；因此尚不能用它跳过 daemon 冷启动的本地全文哈希，也不能以文件名或 `stat` 替代完整性证明。

`sandboxd` 的 microVM 创建入口现在要求匹配的持久活跃调度租约；无租约的直连创建在请求账本写入前被拒，直连 `prewarm` 也被拒。启用后本地和 3FS 通用 microVM 再次完成正式回归，114 条沙箱记录全部停止、队列为空，根分区剩余约 94 GiB。

通用 EROFS 容器创建已在请求账本和实际 Docker 创建点进行两次租约核对。本地两层和 3FS 共享两层环境通过独立暂存、正式 worker 的真实创建与执行；直连 SDK、直接后端调用均被拒绝，拒绝时请求账本和私有目录没有新增内容。最终无活跃 Docker 沙箱或租约、无私有目录残留，114 条 microVM 记录均停止。此机制仍依赖受信任的宿主用户与固定容器根目录配置；直接 Docker 命令及旧 TB2 容器路径尚未纳入。

[Docker 代理](DOCKER_BROKER_ISOLATION.md)已把通用容器生命周期及 worker 的 Docker 身份监控移出 worker 进程，暂存和正式环境本地/3FS 回归、代理重启恢复均通过。worker 与代理目前仍共享拥有 `docker` 组的用户；真正的宿主权限隔离需要管理员创建无 Docker 权限的独立 worker 身份并迁移服务和状态目录。
