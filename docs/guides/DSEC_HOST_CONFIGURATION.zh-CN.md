<a id="installed-single-host-configuration"></a>

# 安装后的单宿主机配置

[English](DSEC_HOST_CONFIGURATION.md) · **简体中文**

`dsec-host` 是 wheel 提供的配置与验收入口。核心可单独安装，任务插件按需安装。镜像、任务定义、内核、Firecracker 和存储服务须显式准备。配置使用 schema 1 的 JSON，所有相对路径均相对于配置文件目录解析。

```json
{
  "schema": 1,
  "instance": "local",
  "state_root": "/srv/dsec/local",
  "service_group": "kvm",
  "sandbox": {
    "binary": "/opt/dsec/firecracker",
    "kernel": "/opt/dsec/vmlinux",
    "template": "/opt/dsec/guest.ext4",
    "capacity": 2,
    "snapshot_concurrency": 2,
    "snapshot_strategy": "full",
    "snapshot_cache_policy": "retain"
  },
  "worker": {},
  "scheduler": {
    "cpu": 2,
    "memory_mb": 1024,
    "disk_mb": 2048,
    "network_mbps": 1000,
    "api_episode_slots": 2,
    "api_inflight": 2,
    "api_rpm": 60,
    "api_tpm": 100000,
    "min_memory_free_mb": 1024,
    "min_disk_free_mb": 4096,
    "network_interface": "eth0"
  }
}
```

示例给出准入预算，不能用来推断实测并发上限或 guest 内存限制。VM 实际限制由选定环境决定。状态目录须归运行账号所有，权限为 0700，该账号须能写入其父目录。路径须满足 Linux Unix 套接字长度限制。每个实例在 `state_root/sandboxes` 和 `state_root/worker` 下分别设置独立的 daemon 和 worker 套接字、rollout 记录及调度预算。

<a id="container-runtime-ownership"></a>

### 容器运行时的职责

沙盒服务负责容器后端、制品解析及生命周期日志。开发专用 SDK `DevEdgeClient` 在节点本地连接服务套接字，调用方无需知道本机镜像路径，也无需访问 Docker。`health` 声明 `container-rpc-v1`。新客户端发现旧服务时会拒绝容器调用，不会回退为自行管理宿主机 Docker。请从同一版本升级客户端与沙盒服务。

为兼容 schema 1，保留 `worker.container_root`、`worker.container_catalog` 和 `worker.container_agent` 配置键，但 `dsec-host` 将其按 `DSEC_CONTAINER_ROOT`、`DSEC_ENVIRONMENT_CATALOG`、`DSEC_CONTAINER_AGENT` 应用于**沙盒服务**。路径相对于配置文件解析。`worker.docker_broker_socket` 同时传给两个服务：Edge 用于运行时操作，worker 的已有资源监测器用于观测。若仍需要旧 E1/E2 的其他制品变量，也应提供给沙盒服务，不要只设置在训练进程中。

将 `worker.container_agent` 指向安装后的独立实现。

```bash
python -c 'import dsec.runtime.backends.container_agent as agent; print(agent.__file__)'
```

历史模块 `container_runtime_agent` 是用于兼容旧导入路径的桥接模块。仅将其绑定到工具镜像内而不提供 DSec 包，不能得到独立可用的 agent。自定义 agent 仍需明确提供路径。

单一 Edge 通过独占锁持有容器目录。已有 `lifecycle-requests/*.json` 保留原请求 ID、操作名称、摘要及 schema。在途或未提交的结果保持 UNKNOWN，直到只读检查能证明其已完成；不会为了取得证明而重放请求。正式部署中，容器与 MicroVM 创建共用 Edge 节点预算。旧 worker 准入套接字与 `--node-budget` 不兼容，`dsec-host run sandbox` 会清除继承的旧套接字环境变量。关闭客户端或正常停止服务不会停止活容器，退役实例前请先通过 SDK 释放所属沙盒。

R3 实机验收确认旧客户端创建的容器可移交新 Edge，另一容器经历服务重启后仍可继续使用，并验证了动作去重、写入隔离和最终租约释放。详见[安装验收](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。这里的宿主容器后端用于受信单宿主开发和对照实验；该变更未实现 VM 内容器隔离、容器暂停或内存卸载，也没有通用的容器清单或 TTL 控制器。

启用 OverlayBD 时，`state_root` 及其 `sandboxes` 目录权限为 2710、属组 `kvm`，存储 daemon 可遍历但不能列出或写入控制目录。worker 记录及控制套接字仍为私有，每个磁盘运行目录沿用现有的存储组访问权限。上级目录也须允许存储服务遍历，服务的挂载命名空间须允许写入所选沙盒根目录，例如通过 systemd 的 `ReadWritePaths`。仅修改文件权限不能授予这一写入范围，请在部署存储服务时一并配置。

`service_group` 通过 `sg` 刷新已经授予的组成员身份。若服务管理器已拥有所需组则省略此项，它不会授予新权限。用户级服务使用虚拟环境 Python 的隔离模式，并在启停时核对进程身份。`Restart=always` 处理 `sg` 在子进程失败后返回零的情况；显式执行 systemd stop 不会重启单元。停止沙盒服务会保留 VMM，退役实例前须通过 SDK 停止沙盒。

worker 通过弱依赖 `Wants` 引用沙盒服务。因此 daemon 自动恢复时，worker 不会停止，已有租约也不会被释放。重启冒烟测试还会关闭该独立实例受监督的启动器，检查恢复后仍是同一 worker 和 VMM，再测试已暂停沙盒的恢复。

<a id="optional-catalog-storage-and-task-plugins"></a>

## 可选环境目录、存储及任务插件

用 `sandbox.microvm_environment_catalog` 指定固定版本的环境配方。各配方可独立选择本地或 3FS 层、DAX，以及文件根盘或 OverlayBD 根盘。3FS 为可选项。OverlayBD 需要 `sandbox.overlaybd_ublk_socket` 和 `sandbox.overlaybd_global_config`。`run sandbox --validate-only` 会检查哈希、层布局及 guest 资源限制，服务提供环境前也会再次检查。

使用 3FS 时，须准备匹配的服务端/客户端二进制及动态库、已配置的 RDMA 设备、监听网卡与地址，并提供运行账号可读且实际在线的 `fuse.hf3fs` 来源挂载。保留可复现的运行依赖集：在目标运行镜像内启动服务前，用 `ldd` 检查恢复的可执行文件，将必需库固定在镜像或显式版本化的库挂载中。不能依赖只存在于旧容器可写层的软件包。发布到该存储还需要单独可写的制品目录，只有 `allow_other` 不会获得发布权。FUSE 挂载传播仅限专用存储子树，避免上级路径递归绑定后发生重叠。核心包不负责部署或修复外部集群。R3 在两台 VM 中使用同一真实 3FS EROFS 层，验证了私有写入隔离和暂停/恢复。这是功能检查，未测分布式存储或冷读取性能。

TB2.1 另需 `worker.tb2_tasks_dir`、`sandbox.tb2_manifest` 以及所选的 `tb2_verifier_artifact_manifest`/`tb2_verifier_artifact_local`。安装后的 `dsec_adapters.tb2_dsec_environment.TB2DSecEnvironment` 直接接收任务目录和环境目录。训练端优先使用 `DSEC_TB2_TASKS_DIR`；旧导入 `OPENENV_TB2_TASKS_DIR` 保留兼容性，无需 OpenEnv。正式评分使用任务自身的 `tests/test.sh`，离线诊断结果不能当作官方分数。

官方验证器继承 guest 的网络和代理配置。挂载依赖缓存不会强制 uv 离线，也不能证明单题依赖完整。可为 worker 服务显式设定 `DSEC_TB2_VERIFIER_ONLINE=0`，选择 uv 离线模式；`=1` 则允许 uv 在已有 guest 网络策略内联网。两者都不授予网络权限或修改防火墙。未覆盖时使用 uv 环境的默认行为。每道题均需检查缓存和工具兼容性及依赖。验证失败时，在回收 VM 前导出已有日志、reward/CTRF 文件及失败回执，记录文件缺失和导出错误。若引导失败且无有效 CTRF，episode 保持未评分。

在 `network` 下配置隔离网络字段 `helper`、`max_slots`、`dns` 和可选 `dax_binary`。此功能要求环境目录。helper 须由 root 拥有、运行用户不可写，仅作用于该实例的 `state_root/sandboxes`。它固定的普通与 DAX 二进制必须与目录一致；显式 DAX 路径会选用 helper 批准的别名。不要复用绑定到其他运行根目录的 helper。helper 与存储分别由管理员配置，`doctor` 不修改权限。

实例可以保留 HTTP 代理，同时让指定软件源直连。设置 `sandbox.egress_proxy_url`，例如将 `sandbox.egress_proxy_bypass_host` 设为 `["archive.ubuntu.com", "security.ubuntu.com"]`。daemon 将这些主机加入 guest 命令的 `no_proxy` 和 `NO_PROXY`，普通 `apt-get` 命令也适用，其余目标继续使用代理。此项由运维显式配置，没有内置 Ubuntu 例外。它只改变代理选择，网络 helper 的防火墙规则仍然有效。启用前先验证 guest 能否直连。主机名会被校验，最多 32 个。

可选 `worker.metrics_port` 将已有 Prometheus 端点绑定至回环地址。`scheduler.shared_services` 使用现有共享服务监测配置。API 预算适用于 worker 的 `policy_call` 计量；外部训练器直调模型时须在客户端另行限流。

`scheduler` 中已有资源和 API 字段的默认值不变。Edge 从公共预算文件中仅读取物理 `NodeBudget` 字段；`api_episode_slots` 是 worker 的作业配额，在途数、RPM 和 TPM 上限属于其独立 API 配额。`dsec-host` 通过 `--node-budget` 传入该文件。独立沙盒 daemon 可使用只描述物理资源的 JSON，含 `network_interface`，另可用 `disk_path` 和 `disk_device` 监测宿主资源。正式调度 worker 要求服务声明 `node-admission-v1` 能力。

每个物理沙盒分配 VMM、容器或宿主句柄前，Edge 会在 `state_root/sandboxes/node-leases` 下持久登记一份租约。SDK 直建、多个连接该 Edge 的 worker、基线分叉和从 ready 池取用 VM，共用这一准入点。取用 ready VM 时转移原租约，只为增加的资源预留申请准入，内存和磁盘不重复计入。TTL 清理会释放物理租约，无需等 worker 回执。清理失败或归属未知时保留租约。worker 重启只恢复作业槽位，不新增节点租约。

guest 虚拟内存上限、物理准入估计值和实测 PSS 是三个独立量。默认活动/创建预留为 1 CPU、512 MiB 内存、1024 MiB 磁盘及 1 Mbps 网络，不会改变清单规定的 guest 内存上限。可选 `scheduler.node_default_demand`、`node_ready_demand` 接受 `NodeDemand` 的字段 `cpu`、`memory_mb`、`disk_mb`、`network_mbps`、`disk_io_mbps` 来配置估计值。覆盖配置必须提供前四项，`disk_io_mbps` 可选。ready 状态默认保持活动时的内存和磁盘估计，将 CPU 降至最多 0.05，网络和磁盘 I/O 设为零。池内启动取创建和 ready 两种估计值的较大者，取出后须获得转为活动状态所需的额外预留。默认值用于起步，须在真实宿主机上校准，不构成密度测量或硬 cgroup 限制。宿主压力下限仍适用，也包括其他负载。

节点范围是**一个 Edge 实例**。同宿主上相互独立的 Edge 应拆分预算。`node_status` 提供权威的范围、预算、预留、活跃租约标识及宿主采样；`scheduler_status.resource_scopes` 从 Edge 取得节点视图。同一 Edge 下的多个 worker 会重复呈现全局节点视图，不能将其物理预留指标相加。作业和 API 视图仍限各 worker。多个 worker 不会自动共享模型提供方的账号限额，须自行分摊。API 配额对象仅协调同进程的调度器，进程重启后速率窗口重置。不支持在线替换部分预算。

仅 `NodeAdmissionBusy` 能证明创建操作没有产生沙盒副作用。调度 worker 记录原因及等待时间，用同一稳定请求 ID 重试。SDK 直调方收到该错误后自行决定何时重试。UNKNOWN、传输失败或资源提交结果不确定时不重放。对于 shell 执行和停止，`busy-nonadmission-v1` 另保证操作锁竞争引发的 `ServiceBusy` 不会留下已准入意图或副作用。仅当服务声明该能力时，SDK 才可等待后用原指定 ID 重试。旧服务上的显式 ID 调用保留原行为，UNKNOWN 始终不可重试。请求 ID、参数摘要和生命周期日志保持原格式；可选的创建资源提示在重试时也必须一致。

升级时先停旧 worker，以原实例根目录和新预算重启 Edge，再重启 worker。Edge 保守接管已有 VM 和容器记录；缺少登记或 Docker 暂不可用，不能证明资源不存在。请保留原日志。R3 使用原持久记录验证了运行中及已暂停的旧 VM，还有旧客户端创建的容器。升级前检查目录归属与权限，Host 会拒绝意外的共享访问，例如旧版父目录继承了 0775，需由运维人员核对后更正。服务不会自行改动已有目录。OverlayBD 仍沿用前文的 2710 和组遍历权限。

API 调用排队期间不消耗速率 token 或并发额度，开始分派才占用。已尝试的调用即使取消或结果未知，也保留其 RPM/估计 TPM 预留。API 完成、取消或评分均不会释放沙盒节点租约；必须确认已清理物理资源才能释放。

<a id="commands-and-acceptance"></a>

## 命令与验收

安装后可运行 `init`、`validate`、`doctor`、`preflight`、`prepare-node`、`render`、`render-privileges`、`run`、`wait`、`status` 和 `smoke`。`doctor` 检查宿主访问权限和空闲磁盘等前提，`doctor --live` 还会检查服务健康。`wait` 等待异步服务启动。渲染不会自动安装或启动服务单元，已有文件不同时会拒绝覆盖。可运行顺序见 [README](../../README.zh-CN.md)。

`smoke` 使用非 TB 任务，保存对话、版本、rollout 身份及清理证据。若其写命令意外执行第二次会失败。`--restart-services` 另在专用实例上检查运行中进程身份及暂停恢复。最后停止所有本次拥有的沙盒；失败检查也保存证据。这是确定性的生命周期验收，不评估模型质量或训练。

对登记在环境目录内的 MicroVM 节点，请用实际的非特权服务账号执行下列命令。使用集群部署流程生成的节点配置。

每个实例的管理员安装只需执行一次。固定身份、路径、VMM 或地址分配变化后，应审阅并显式更新。日常沙盒操作仅非交互地调用已安装的固定 helper，无需逐 VM 或逐 episode 重新安装。不要给运行账号无限制的免密 sudo。退役安装前先停掉所有自有资源，再由管理员收尾。

联网执行请选用通过 `dsec_image.boot` 的 `--network` 选项构建的启动制品。创建 TAP 及传入 guest 网络参数并不能使旧无网启动制品自动联网。初始化不会改写共享镜像，验收时应使用与声明的运行环境相符的已准备独立制品。

```sh
.venv/bin/dsec-host --config host.json preflight --node-config node.json
.venv/bin/dsec-host --config host.json prepare-node --node-config node.json \
  --user runtime-user --slot-offset 4096 --out reviewed-privileges
# Inspect all generated files. Select an address range disjoint from other instances.
sudo /usr/bin/python3 reviewed-privileges/install-privileges.py
sudo /usr/bin/python3 reviewed-privileges/install-privileges.py --apply
.venv/bin/dsec-host --config reviewed-privileges/host-with-network.json \
  preflight --node-config node.json
```

`preflight` 只读检查宿主前提，不执行网络 helper、不创建状态目录、不连接控制面也不启动服务。返回值可能为 `READY`、`NEEDS_INITIALIZATION`（缺少实例 helper 或配置）、`ADMIN_CHECK_REQUIRED` 或 `BLOCKED`。普通账号可能因 ptrace 权限不足无法读取 PID 1 的命名空间，这表示尚未验证，不能据此认定配置确实冲突。管理员安装时须验证，已有 helper 在每次操作前也会检查上下文。确认命名空间不一致会立即阻止操作。资源压力检查在阻塞报告中记录 `RESOURCE_WAIT`。仅 `READY` 返回退出码零。`prepare-node` 允许缺少 helper 或需管理员验证的情形，遇到已确认障碍则在生成输出前拒绝，并将声明的环境、最低需求和 Edge/worker 套接字绑定到本 Host 配置。此操作不注册节点或授予权限。

检查覆盖 Linux 平台、实际 KVM/TUN 字符设备身份和只读 ioctl、能力边界、提权限制、用户与命名空间上下文、转发、工具、运行账号的 KVM 组、制品、存储套接字权限以及当前内存/磁盘下限。可用内存余量取可见 cgroup v2 内存限制内的值。容器标记只作诊断；受限 Pod 因实际缺少能力或设备被拒，与提供商名称无关。缺少宿主能力、用户命名空间被重映射或 `NoNewPrivs` 等问题无法通过此初始化工具修复，应由宿主管理员或提供商准备前提，或改选其他节点。账号组、软件包、内核模块及存储服务仍由管理员配置。命令不修改 Docker、转发或全局网络规则。

`READY` 表示已观测到所需前提，不能证明 VM 能够启动或隔离已生效。只读检查无法证明所有 seccomp/AppArmor 策略、实际命名空间操作、sudo 执行或存储服务健康。接纳生产工作前仍须显式启动服务并完成真实运行验收。

此命令生成实例专用 helper、不可变配置、sudoers 规则及 `host-with-network.json`。root 安装器验证内嵌 SHA-256、检查 sudoers 并拒绝覆盖不同的已有安装，不导入运行时或实验代码。修改安装目标前，安装器用仅依赖标准库的内嵌探针复核当前平台、固定的运行 UID/KVM GID，并创建子进程降权至运行身份后复查设备访问。保存的预检报告不能充当授权。指定账号只获得 helper 固定操作集的权限。helper 固定普通/可选 DAX VMM 构建，限制所分配的地址槽，降到运行 UID 后才启动 VMM。渲染阶段不授予任何权限。安装后用 `host-with-network.json` 重新渲染服务。宿主转发、所需网络工具和存储服务可写范围须预先就绪，安装器不修改这些配置、不替换其他 helper 或重启已有服务。既有流程仍可用底层 `render-privileges` 命令，生成的安装器同样会复核当前平台。

用安装版解释器的隔离模式检查固定任务轨迹。

```sh
.venv/bin/python -I tools/verify_installed_task.py --config host.json \
  --trajectory tools/fixtures/tb21-openssl.json --expect-score 1 --out tb21-acceptance.json
```

测试样例固定 TB2.1 源码修订。此命令保存原始观测、官方评分与清理状态，用于功能验收而非模型评估。底层 worker 单独保存官方验证器文件。

若独立实例有两个槽位并可同时容纳基线和一个分支，可用下列命令验证跨 episode 复用准备态。

```sh
.venv/bin/python -I tools/verify_installed_fork.py --config host.json \
  --trajectory tools/fixtures/tb21-openssl.json --out fork-acceptance.json \
  --restart-services
```

验收共用不可变准备态磁盘和继承的应用内存，先后启动两条全新的策略历史，检查私有写入及真实官方评分。随后在分支仍运行时删除源基线，验证分支暂停/恢复和最终后台制品回收。可选重启仅作用于本配置中指定的服务。请在空闲的独立实例上执行；它不测并行分叉性能。

v0.1 面向受信的单宿主运行用户。私有套接字、受限 helper 和租约无法隔离互相敌对的宿主用户。新宿主 helper 准备、制品来源与许可证审阅、完整后端对比和其余发布验收见[发布计划](../../ROADMAP.zh-CN.md)。


<a id="edge-ownership-during-restart"></a>

### 重启期间的 Edge 所有权

持久化 Edge 在创建或恢复前先独占锁定实例目录。第二个所有者会被拒绝。服务正常 `detach` 时持久保存记录、结束控制线程并关闭本地进程句柄，保留已登记沙盒以便下一次 Edge 接管。交接后旧 Python 对象失去执行、创建和停止权限。显式 Edge `close` 则会先停止沙盒，仅在清理成功后释放所有权，清理失败保留所有权以供重试。目录锁限于一个实例，不是跨实例的宿主全局锁或预算。

<a id="native-sessionsfilesstreaming-development-opt-in"></a>

### 原生会话、文件及流式操作：开发环境显式启用

本次迭代的 Python SDK 和共享 C agent 已通过本地 UDS 及单宿主 Linux 验收，包括 vsock、空闲会话快照、分层容器及重启/崩溃恢复。仅对新准备且显式启用的制品生效。超时、取消、二进制文件及重连语义见[原生 SDK 契约](../architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md)，已测范围见[安装报告](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。

显式升级 guest 制品，仅改变 Python wheel 无法为正在运行的旧 guest 添加原生监听端口。用 `tools/build_smoke_guest.py` 和更新后的 `guest_agent.c` 构建新 MicroVM 模板，将 `guest_native.c` 放在其旁。保留旧模板和目录以服务现有 guest，新不可变制品须作为独立环境登记并验收。

新分层容器实例可在源码包中将同一 agent 编译为静态 Linux 二进制。

```sh
python tools/build_native_agent.py --source guest_native.c --out "$ARTIFACTS/native-agent-v1"
export DSEC_NATIVE_AGENT="$ARTIFACTS/native-agent-v1"
```

在 Edge 上设置该环境变量，同时按常规通过 `DSEC_CONTAINER_AGENT` 指向更新后的容器 agent。后端以只读方式挂载原生二进制，容器监督器在私有 guest 根挂载后启动它，卸载前先停止。私有 UDS 位于实例目录。原生命令与文件路径和现有单次 shell 一样，在相同的 guest 根内解析。完整宿主路径 `<DSEC_CONTAINER_ROOT>/<32-character-id>/native.sock` 必须满足 Linux 的 107 字节路径名上限。路径过长时 Edge 会在创建沙盒前拒绝，应缩短配置根目录。未启用时旧容器启动与命令保持原行为，调用原生 API 会报告 `UnsupportedCapability`。此步骤不升级历史 TB2 OpenEnv 服务镜像，也不安装基准、GPU 训练器、Rust 存储二进制或系统级服务。

部署新的不可变 guest 后，用安装版 SDK 进行验证。

```sh
python -I tools/verify_native_sdk.py --socket "$STATE/sandboxes/service.sock" \
  --environment python-native --out native-sdk-acceptance.json
```

对准备好的分层容器，增加 `--backend container` 及其环境 ID。此工具最多创建两个沙盒，保存失败证据，退出时停止沙盒。检查覆盖会话状态、二进制传输、去重、重叠执行、流重连、取消和超时边界；MicroVM 另测活动状态拒绝暂停及空闲会话快照恢复。重启/崩溃验收需专用 Edge 实例，并按单独的宿主流程执行。guest 网络配方应符合服务配置：通过 `--network` 构建的启动制品必须配置 guest 网卡，即使某题或验证器本身不需要互联网。

<a id="networkless-qemu-container-and-fncall"></a>

## 无网络 QEMU Container 和 FnCall

这些 Linux 执行预览需要 **Edge 契约 2**。从契约 1 自动升级或向其回滚会被拒绝，因为旧运行时无法管理这些已持久化的实例。请先 drain，再准备新实例；要保留旧状态时须经审阅后迁移。节点网关、Edge 和配置的 episode 服务须使用同一 wheel。旧普通 Container 的请求及摘要字节保持不变。

运行时需要非特权账号对 `/dev/kvm` 和 `/dev/vhost-vsock` 的读写权限、QEMU/KVM 及准备好的制品。这些执行路径不安装宿主 Docker、不修改网桥和防火墙，也不提供出站网络。[隔离契约](../architecture/EXECUTION_ISOLATION.zh-CN.md)列出其余生产验收条件。使用新的 Container 专用状态目录，不得指向旧宿主 Docker 实例或其他 Edge 的状态。

在备有 Docker 和静态 C 编译器的 Linux 构建机上准备模板。提供已在本机存在的精确镜像 ID，不使用可变标签。可信基础镜像需要 Python 3、`/bin/sh`、mount/mountpoint 及 coreutils；任务镜像需要 Python 3 和 `/bin/sh`。静态 Docker 二进制来自固定版本的 dind 镜像，构建器仅导出已停止容器的内容，不在宿主上运行 dind。

```bash
python tools/build_docker_guest.py --base sha256:BASE_IMAGE_ID \
  --docker-tools sha256:DOCKER_TOOLS_IMAGE_ID --task-image sha256:TASK_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/function.ext4 --mode fncall
python tools/build_docker_guest.py --base sha256:BASE_IMAGE_ID \
  --docker-tools sha256:DOCKER_TOOLS_IMAGE_ID --task-image sha256:TASK_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/container.ext4 --mode container
```

将示例中的符号 ID 替换为 64 位小写十六进制字符串。保留两条输出记录。`sha256` 固定模板，`inner_image` 固定内层 Docker 实际加载的镜像配置。宿主 Docker 使用 containerd 时，清单 ID 可能与配置 ID 不同。构建器会从已固定的导出归档中提取正确的值，请勿用标签替代，也不要直接照搬宿主镜像 ID。

准备一个租用 CID 的共享目录，使用绝对路径，由运行时账户持有，权限为 0700。同一宿主机上协同工作的 Edge 必须使用同一份分配记录和同一区间，并与其他 VMM 的 CID 区间错开。创建可信的 `schema:2` 目录文件，包含 `cid_pool:{root,first,last}` 和 `environments` 映射。每个环境都必须填写以下字段。

| 字段 | 必填值 |
| --- | --- |
| `mode` | 选择 `fncall` 或 `container`，与准备好的模板一致 |
| `binary`、`kernel`、`template` | 对象，包含规范化的绝对路径 `path` 和 `sha256` |
| `image` | 构建器输出的 `inner_image` |
| `memory_mb`、`cpu_count` | 外层 VM 的内存与 vCPU，例如 1024 和 1 |
| `inner_memory_mb`、`inner_cpus` | 内层资源上限，按当前 CLI 默认值构建时分别为 512 和 1 |

在 Host 配置中添加 `sandbox.qemu_execution_catalog`。可将 `worker.container_root` 指向新的私有状态目录，默认使用 `state_root/containers`。此目录文件决定可用环境。环境未登记，或请求不支持的模式、存储或 QoS 时，创建请求会提前失败，系统不会改用宿主 Docker。启动前会验证制品内容。修改目录文件后须显式重启 Edge，请勿变更在运行实例所用的目录内容。

使用当前构建器重新构建 Container/FnCall 模板。系统拒绝 schema 1 目录和旧的无认证模板。切换到新状态前，先用匹配的旧版本排空原有实例。单独修改 schema 编号无法升级旧 guest。内核需要 `CONFIG_VIRTIO_CONSOLE=y`。Edge 在每个实例的私有目录中创建唯一的 `channel.key`，权限为 0600，并通过专用的 virtio-serial 引导套接字注入。请勿跨实例复制密钥或随日志发布。Edge 重启时应保留这个文件。运行中的 VM 如果缺少密钥或密钥发生变更，系统会拒绝访问，不会自动生成替代密钥。创建新 VM 时会换用新密钥，暂不支持在线换钥。已停止实例的私有记录保留密钥以便恢复，彻底删除已退役实例时即可一并删除。

Node 公布 `microvm` 和 `container` 两种路由后端，Firecracker 与 QEMU 分别使用各自的环境 ID。将两组 ID 加入 `node.environments` 和 `minimum_demands`。每个 QEMU 环境声明的最低需求必须覆盖一个并发槽、外层 VM 的全部内存和 vCPU，以及整个私有裸盘（默认 2048 MiB），即使内层容器只限用 512 MiB。`node-preflight` 会校验这些值，Node 和 Edge 也会在 API 创建阶段强制执行实际观测到的最低需求。FnCall 已准备好的能力由 Edge 健康接口发布，调用方无法通过配置指定。

公共客户端沿用现有 HTTPS API 和项目凭据。

```python
from dsec.sdk import DSecClient, DSecFnCallRunArgs, DSecContainerRunArgs

async with DSecClient(["https://api.example"], token=token,
                     project="research", ca_file="ca.crt") as client:
    demand = dict(cpu=1, memory_mb=1024, disk_mb=2048, network_mbps=0)
    slot = await client.run_fncall(
        DSecFnCallRunArgs(environment_id="python-function"), resource_demand=demand)
    try:
        first = await slot.invoke("echo 42 >/work/private; cat /work/private")
        fresh = await slot.invoke("test ! -e /work/private")
    finally:
        await slot.stop()
    box = await client.run_container(
        DSecContainerRunArgs(environment_id="python-container"), resource_demand=demand)
    try:
        await box.run_shell("echo persistent >/work/value")
        result = await box.run_shell("cat /work/value")
    finally:
        await box.stop()
```

函数槽在预热待命期间持续占用配额。`attach_fncall` 可以重新连接并复用原槽。Container 和 MicroVM 支持持久化 shell 和文件会话，FnCall 不支持。需要恢复结果时，请使用稳定的请求 ID。如果状态为 UNKNOWN，应查询原调用，避免以新 ID 重新触发执行。函数成功响应在进程和 tmpfs 清理完成后返回。清理失败将退役该 VM。

<a id="direct-linux-fullvm-preview"></a>

### 直接启动 Linux FullVM（预览）

在已准备好的 Linux KVM Node 上构建固定版本的独立 guest。构建时使用 Docker 导出可信本地基础镜像的根文件系统，FullVM 内无需运行 Docker 守护进程。

```bash
python tools/build_docker_guest.py --mode fullvm --base sha256:BASE_IMAGE_ID \
  --source . --out /srv/dsec/artifacts/fullvm.ext4 --disk-mb 512
```

使用兼容 Debian 的可信基础镜像，包含 Python 3、`setpriv` 和标准挂载及核心工具。内核须支持 Container/FnCall 使用的 QEMU virtio-blk、vsock 和 virtio-console 设备。在 QEMU 目录中设置 `mode: fullvm`，固定 `binary`、`kernel` 和 `template`，并设置 `memory_mb` 和 `cpu_count`。省略内层 Docker 镜像及其资源字段。此预览支持私有本地磁盘，无网络。在 Node 上以路由后端 `fullvm` 登记环境，最低资源需求须覆盖整台 VM 和私有磁盘。评估时使用全新的私有 Edge 实例，旧版 Edge 无法打开包含 FullVM 项的目录。

```python
from dsec.sdk import DSecFullVMRunArgs

box = await client.run_fullvm(
    DSecFullVMRunArgs(environment_id="linux-fullvm", memory_limit_mb=512, cpu_cores_limit=1),
    resource_demand=dict(cpu=1, memory_mb=512, disk_mb=512, network_mbps=0),
)
try:
    print(await box.run_shell("id; uname -a"))
finally:
    await box.stop()
```

请求的 CPU 与内存必须匹配环境固定的外层 VM 配额，物理资源需求还须计入已配置的宿主开销。guest 监督进程使用每 VM 独有的密钥认证 vsock。命令和原生会话以 guest UID 1000 运行，禁止获取新特权。状态显示 `backend=fullvm`。如需限定范围的 API 检查，在验收配置中使用 `paths.fullvm.spec` 和 `paths.fullvm.demand`，运行 `tools/verify_execution_paths.py --fullvm-only`。默认环境无显示设备，尚不支持 Windows 和快照／恢复。

按以下步骤启用无网络桌面预览。

- 使用固定版本的 guest 内核，提供 virtio GPU/DRM、virtio input、evdev，以及常规 FullVM 所需的 virtio-blk、console 和 vsock 驱动。驱动可编入内核，或由 guest 在经认证的服务启动前加载。
- 在基础根文件系统中加入桌面软件包及可信的可执行文件 `/opt/dsec-desktop/start`。使用构建器的 `--desktop` 选项后，该钩子会在 guest 挂载就绪后、原生服务启动前运行。钩子需要启动桌面后返回，并作为固定镜像的组成部分发布。
- 对于通过 Xfce 桌面操作文件的任务，请安装提供会话 D-Bus 服务的文件管理器，例如 Thunar。即使缺失该服务，Xfdesktop 仍然能显示图标和「重命名」菜单。
- 在 FullVM 目录项中设置 `"display": "virtio-2d"`。请使用独立的预览 Edge，并停用 QEMU 宿主约束配置。此桌面路径尚未在该配置下完成验证。

`await box.screenshot()` 通过同一认证 API 返回 PNG 字节和图像尺寸。`await box.desktop_input(events, request_id=...)` 每次最多接收 64 个键盘、按钮或绝对指针事件（坐标范围为 0–32767）。在 virtio tablet 上点击时，分别用不同请求 ID 发送按下与松开。两者置于同一批次中可能返回成功，但 guest 中看不到点击效果。移动后点击时，应先发送绝对指针坐标，再发送按钮按下事件。输入可能已送达而响应丢失时，使用 `box.query_request(id)`。无需远程 VNC 端口、guest TCP 监听、GPU 直通或图形加速后端。Ubuntu 测试镜像包含 Xfce 桌面及名称为 `DSecDesktop` 的 xterm，可搭配现有的 `tools/verify_execution_paths.py --desktop-only` 测试。


`tools/verify_execution_paths.py --config PATH --out REPORT` 使用已安装的 SDK 通过 HTTPS 执行验收，同时最多创建两个沙盒，并自动尝试停止。其 JSON 配置包含 `client`（除令牌外的 DSecClient 关键字参数）、`token_file`（权限 0600），以及由 `fncall`、`container`、`microvm` 索引的 `paths`。示例中每条路径都包含 `spec` 和 `demand`。检查覆盖函数调用状态是否干净、重复请求去重和超时、Container 持久会话与二进制文件、CID 和工作区是否独立，以及 Firecracker 暂停／恢复。请保留失败报告，并在运行后检查物理资源、IAM 和 CID 租约。租户安全、冷启动性能、镜像共享和整机高可用均需另行验证。

<a id="qemu-host-confinement"></a>

### QEMU 宿主隔离

此可选路径已在限定范围内通过 Linux 无网络 AppArmor/VMM 验收。其他生产安全与联网条件仍待验证。详见[验收报告](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。在 schema 2 的 QEMU 目录顶层加入下列字段，并替换为实际运行账户 UID、实例名称和服务 cgroup 路径。

```json
"host_isolation": {
  "cgroup_root": "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/dsec-example-sandbox.service",
  "helper": "/usr/local/libexec/dsec-example-qemu-profile",
  "memory_overhead_mb": 256,
  "pids_max": 128
}
```

broker 绑定一个共享的 QEMU 可执行文件。使用直接启动时，还绑定一个内核。如需不同的可执行文件或直接启动内核，请分别使用不同 Node。无网络的 UEFI FullVM 在目录中声明固件与 QCOW2 文件。渲染器会固定确切的只读固件和基盘文件，供管理员审批。每台 VM 只可写入自己的 QCOW2 写入层和 UEFI 变量。应先排空原实例，然后从新的 Container 状态目录开始。策略属于不可变的目录和活动 VM 身份，运行期间应保持一致。

使用服务账户渲染内容固定、可供管理员审核的安装包。

```bash
dsec-host --config host.json render-qemu-privileges --user dsec --out qemu-privileges
apparmor_parser -Q -K qemu-privileges/sample.profile
# Administrator: review and check, then install once.
sudo python3 -I qemu-privileges/install-qemu-privileges.py
sudo python3 -I qemu-privileges/install-qemu-privileges.py --apply
dsec-host --config host.json render --out user-units
```

安装器沿用受限安装流程的检查。发现现有目标与预期不同时会拒绝安装，只安装固定的 AppArmor helper、配置和 sudoers 文件。Docker、网络、cgroup、服务和模块均无需变更。运行时通过非交互式 sudo 调用 helper，不接受 shell、root 启动器或任意 profile／路径参数。Edge 本身需保留 sudo 能力，NoNewPrivs 设置在 VMM 子进程上。宿主必须支持 AppArmor 对 Unix 套接字的细粒度访问控制（`/sys/kernel/security/apparmor/features/network/af_unix` 应报告 `yes`）。仅成功加载语法正确的 profile 还不足以证明 VMM 启动正常、访问边界生效，请在部署宿主上分别验证。

生成的沙盒服务单元使用 `Delegate=cpu memory pids` 和 `DelegateSubgroup=edge`（systemd 254 及以上）。确认实际的 `ControlGroup` 与目录一致。在该委派服务外通过 SSH 前台启动会被拒绝。每台 VM 使用独立的子 cgroup，配置 `memory.max = guest RAM + overhead`、`memory.swap.max=0`、`memory.oom.group=1`、有上限的 `pids.max`，以及与 vCPU 数量相符的 CPU 配额。更新 Node 的最低需求和**客户端申报的需求**时要包含额外开销。例如 guest 为 1024 MiB、开销为 256 MiB，准入时至少需要 1280 MiB。未配置宿主隔离的开发目录沿用原最低需求，启用隔离后应显式计入额外的内存成本。

如需单独验证 cgroup 控制器，在具有独立 cgroup 根和报告路径的新专用委派服务中运行 `tools/verify_host_cgroup.py`。它会测试 48 MiB 子组的 OOM、进程数限制和 CPU 限流，随后移除空组。实际 QEMU/AppArmor、租户和网络仍需分别验收。

<a id="isolated-qemu-ipv4-egress-bounded-linux-acceptance"></a>

### QEMU 隔离的 IPv4 出站网络（限定范围的 Linux 验收）

现有环境默认保持无网络。为新建且已排空的实例准备联网时，在目录顶层添加 `network` 对象，并仅将指定环境项设置为 `"network": "isolated"`。

```json
"network": {
  "helper": "/usr/local/libexec/dsec-example-qemu-network",
  "uplink": "eth0",
  "slot_offset": 1024,
  "max_slots": 2,
  "policy": {"dns": "1.1.1.1", "egress_proxy": null}
}
```

`host_isolation` 为必填项。管理员需在 `10.232.0.0/16` 中预留无重叠的地址段，每个槽位使用一个 /31。如果区间与现有路由重叠，系统将拒绝创建。宿主必须事先启用 IPv4 转发。该路径无需创建共享网桥或宿主 Docker 守护进程。每个 TAP 均位于独立的网络命名空间，因此 guest 地址和 MAC 可以复用。QEMU 接收一个已打开的 TAP 文件描述符，并以非特权身份运行，无需进入该命名空间或取得 CAP_NET_ADMIN。

在原有参数基础上加入 `tools/build_docker_guest.py --network-policy policy.json`，重新构建每个选定的模板。`policy.json` 的内容必须与目录中的 `network.policy` 完全一致。将重建后的制品 SHA-256 写入目录。guest 的 init 在任务启动前配置 IPv4。内层 Docker 使用外层 guest 的网络，移除 capabilities，不挂载宿主目录或设备。控制通道仍使用经认证的 vsock。配置代理时，策略会添加一条例外，允许访问 `{ "ip": "192.168.0.104", "port": 18888 }` 上的 TCP 服务，并在任务内设置 HTTP(S)_PROXY。此处不接受代理凭据。

`render-qemu-privileges --clang /path/to/clang` 使用普通服务账户构建 TC eBPF 对象。构建时需要 Linux x86_64 头文件及支持 BPF 的 Clang。审核 `egress.c`、`sample.profile`、两个 helper 及其配置。安装器固定并安装六个 root 文件，创建两个私有状态目录。安装阶段无需变更网络，随后通过 SDK 创建实例时，只会调用该实例已固定的 broker 操作。运行时无需编译，也不允许调用方提交替代的 eBPF 对象。已有安装与预期不同时，安装器仍会拒绝替换；请为预览另建实例，保留正在使用的 helper 和目录。

如果新增环境可复用已安装的权限，就无需更换 helper。先排空 Node，保留上一版不可变目录，将 `"catalog_history": [{"path": "/srv/dsec/catalog-v1.json", "sha256": "..."}]` 加入新目录，再显式重启 Edge。对历史实例，只有环境定义、CID 池以及宿主／网络策略全部一致时才允许接管。更改既有镜像、资源上限或策略超出了增量添加的范围。请保留仍有实例依赖的环境。须显式登记历史目录，最多 16 个文件，并检查内容有无变化。

已准备的环境可将 `startup_timeout_s` 设为 1 至 240 秒（默认 15 秒），用于 guest 初始化、镜像导入以及认证就绪检查。QMP 就绪时限仍为 15 秒，工具命令时限也保持原值。此预算固定在每条实例记录中，旧记录按 15 秒处理。更改预算须创建新环境版本，旧实例仍按原值解释。该值仅表示允许的启动时长，实际创建延迟需通过测量获得。

环境可为私有 QEMU 根盘设置 `"disk_cache": "direct"`，使两个块节点都使用 Direct I/O，在保留 flush 的同时绕过宿主文件缓存。这样可以避免 guest 磁盘数据在宿主上形成第二份脏页／待回写副本并占用 VMM 的 cgroup 配额。现有环境及记录默认沿用 `buffered`，切换模式须创建新的环境版本。不支持 Direct I/O 时启动失败，不会自动回退。此设置与 DAX 或 guest 内存共享无关，也不会减少 guest RAM。回收由 Edge 持有的空 cgroup 前，Edge 会将可获取的峰值、OOM、内存、CPU 和 I/O 计数器写入实例目录的 `host-resources-retired.json`。内核未提供的计数器保持缺省，以区别于实测零值。内核资源删除后仍可追查失败，原有的回收前置条件保持有效。

TC 防护程序在链路启用前加载。首版策略允许 IPv4 TCP/UDP 公网出站、明确指定的 DNS、可选的代理例外，以及访问固定网关所需的 ARP。它拒绝源地址伪造、IPv6、VLAN、IP 选项、分片、元数据地址和其他私有／保留目标。返回流量由连接跟踪处理。本路径暂未实现带宽整形、在线策略轮换或 IPv6。公共 API 暂不支持通过 `network_rules` 覆盖环境的不可变策略。网络状态包含内核中实际运行的程序身份。如身份已变化，系统拒绝接管。创建中断后不会重新出借文件描述符。

在 Node 上，限定范围的 helper 通过 `test-policy SID` 操作，将固定的允许／拒绝测试数据包送入该实例已挂载的 BPF 程序，无需发送实际网络流量。此操作不接受数据包、程序 ID、路径或重复次数参数。为应用启用网络前，请搭配实际 guest 的 DNS／HTTPS、拒绝宿主和私网连接、重启以及清理检查。可移植测试、原生 C 测试及 BPF 编译成功均无法单独证明 Linux 上的强制隔离已生效。

2026-10-25，在一台已准备的 Linux Node 上使用安装后的 wheel 验收，七项公共 SDK 检查和 29 项编排检查均通过。检查覆盖已挂载程序的实际数据包测试、Container DNS／公网 HTTPS、FnCall DNS、拒绝访问宿主／元数据地址／私网、Edge 重启与接管及失败清理。所有归属该实例的命名空间、TAP、profile、cgroup、CID、私有可写磁盘和物理资源／IAM 租约均已回收，原有宿主 Docker/Grafana 容器保持原状。本次采用固定 DNS `192.168.0.1`，无代理。其他 Node 的前置条件、经配置代理的出站连接、对抗性多租户场景和连接洪泛均需单独验证。详见[验收证据](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。


<a id="qemu-shared-erofs-through-virtio-pmemdax"></a>

### QEMU 通过 virtio-pmem/DAX 共享 EROFS

此可选的环境能力与 `disk_cache` 相互独立。私有 ext4 I/O 可使用 `direct`，不可变共享数据则使用 pmem/DAX。无需修改 SDK 或增加模型依赖。2026-10-26，在 Linux 上使用安装后的 wheel，通过了两台 Container VM 和一个顺序执行的 FnCall 槽的限定范围验收，详见[验收报告](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md)。本次未测定整台 VM 的资源节省量或生产规模容量。另一次限定范围的 [SWE-Gym mypy 迁移](../reports/SWEGYM_FIRST_USE.zh-CN.md#real-task-erofspmemdax-migration-2026-10-26)通过了真实文件 DAX 检查及官方 baseline/oracle 评分。

使用以下命令发布每个**已准备好的文件系统目录**。

```bash
python tools/build_erofs_dax.py --source staged-layer --out /srv/dsec/layers/base.erofs
```

发布器需要支持 `--tar=f` 和 `noinline_data` 的 `mkfs.erofs`。它通过 tar 保留数值 UID/GID，禁用压缩，添加预留的 DAX 探针，并将容量填充至 128 MiB 的倍数。此命令不拉取镜像、不转换 OCI whiteout，也不保留任意文件系统扩展属性。转换 whiteout 应使用现有的 OCI 发布路径。请勿以压缩 EROFS 镜像替代。各层均须为无写入权限位的不可变普通文件。

将返回的 `{path, sha256, bytes}` 对象按底层在前的顺序组成 JSON 列表，传给 `tools/build_docker_guest.py --readonly-layers layers.json`。对合并后的文件树提供的每个目录重复指定 `--shared-mount /opt/toolkit`。目录可包含任务工作区或依赖，系统从组合后的 guest 文件系统将其绑定挂载到任务容器中。Container 写入时将文件复制到该 VM 的私有 ext4 上层。FnCall 以只读形式挂载。单独挂载共享目录会保留任务镜像中的旧副本。如需在发布时移除旧副本，对已固定的本地镜像使用 `tools/split_docker_image.py`，再用 `--task-archive` 和 `--task-archive-sha256` 传入精简归档，取代 `--task-image`。这两组输入互斥。构建器在改动宿主前验证归档哈希，并在复制后复验。拆分工具保留运行时 Config，以及 tar 中的所有者和元数据。已声明的卷和跨分区硬链接须另行准备。发布、目录配置及应用验证步骤见 [SWE-Gym 运维指南](../../apps/swegym/README.zh-CN.md#move-shared-task-files-to-erofsdax)。

将构建器输出的 `readonly_layers` 和 `shared_mounts` 数组添加到新环境目录项。路径由管理员控制，公共 API 不接受调用方传入路径。每个环境最多 8 层，每层容量为 128 MiB 至 32 GiB，按 128 MiB 对齐。将 pmem 总容量除以 64 得到元数据的最低内存开销，扣除后还须至少剩余 128 MiB 的 guest RAM。此条件仅约束元数据开销，实际负载须另行估算内存。私有磁盘仍单独计费，共享容量不会重复计入每个沙盒的可写磁盘预留。guest 就绪检查要求具备实际 DAX 属性，设备容量符合预期，且私有 ext4/OverlayFS 挂载正确。

对新 Node，请显式指定已存在的发布根目录，按正常流程渲染权限包。

```bash
dsec-host --config host.json render-qemu-privileges --user dsec --out qemu-privileges --layer-root /srv/dsec/layers
```

启用此能力时须由管理员安装一次。之后 broker 仅接受固定根目录内按 VM、按具体文件授予的读取／mmap 权限，不授予写权限或覆盖整个目录的规则。活动实例无法更换其层。接管和回收会保留原始策略身份，实例清理不会删除共享制品。现有无共享层的 profile 保留原有文本和生命周期行为。更新正在使用的 broker 之前，须先排空其 VM，并保留旧 helper 和配置。

运行时验证整个文件的 SHA-256，目前会读取这些本地制品。本路径的冷缓存按需读取性能**尚未验证**。要验证这项性能，须先复用现有的 fs-verity 发布凭据。

<a id="bounded-qemu-commands-and-recovery"></a>

### QEMU 命令的运行时限与恢复

现有环境的命令上限为 30,000 毫秒。如需运行更长的命令，使用 `--command-timeout-ms 120000` 构建**新** guest，并在新执行目录的环境项中将 `command_timeout_ms: 120000` 设为相同值。最大支持 300,000 毫秒，API 调用方无法提高上限。就绪检查会核对 guest 配置的时限和固定目录。请同时重建 guest。添加环境时须保留所需历史目录的每一个显式固定项，对历史的信任不会自动传递。未设置此字段的既有环境仍然兼容。

调用方为每条命令提供有界的 `timeout_ms`。状态与结果查询无需在命令的执行锁后排队。客户端断连或任务取消不会撤销已获准执行的操作。保存请求 ID，重连后查询 `box.query_request(id)`，避免再次发送。同停止请求冲突的 shell 命令若正在运行，停止请求会在准入前被拒绝并返回 `ServiceBusy`。等待命令完成或超时后再停止沙盒。此机制不支持立即取消单条命令。

Edge 所有者失联后，尚未确认的操作意图保存在宿主日志中。只有原 VM 提供匹配的请求 ID 和摘要，并返回已持久化的 guest DONE 响应时，查询才可提交 DONE。证据缺失、不匹配或无法获取时，状态保持 UNKNOWN。系统不会重放命令，也不会因 VM 仍在运行就判定操作成功。在 Linux 上，命令 helper 使用 subreaper 在超时或失败时回收孤儿进程、创建了新会话的后代和后台子孙进程。无法证明已回收时，命令或槽会被标为失败。Container shell 正常结束时仍可有意保留后台进程，内层容器的 init 会回收已退出的孤儿进程以防僵尸进程堆积。该 init 以只读形式挂载在任务共享目录外的预留位置 `/dsec-init`。Docker 隐式使用的 `/sbin/docker-init` 可能被共享挂载 `/usr` 遮住。FnCall 在调用之间仍会重置整个容器。原生会话遵守相同的预设命令时限。构建器将时限编入 agent，宿主通道负责执行限制，guest 代理允许在该时限内返回响应。
