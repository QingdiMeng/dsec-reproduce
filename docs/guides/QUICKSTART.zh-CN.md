# 通过 API 访问 DSec 并配置 Linux 节点

[English](QUICKSTART.md) · **简体中文**

本预览版支持受信用户在 Linux x86_64 上运行，需要 systemd 用户服务、cgroup v2 和可访问的 KVM。控制面可独立安装，基准数据集、模型服务和可选存储服务按需准备。本项目是 DSec 的独立复现。

## 应用如何访问沙盒

正式 SDK 连接 API Server，即使控制器和 Edge 在同一台机器上也如此。运维人员部署[高可用控制器与节点服务](CLUSTER_DEPLOYMENT.zh-CN.md)。应用开发者只需 API 地址、可信 CA、IAM 凭据和项目名称。Edge 套接字、宿主镜像路径和节点凭据由运维侧管理。

```python
from pathlib import Path
from dsec.sdk import DSecClient
from dsec.contracts.resources import NodeDemand

async def run():
    async with DSecClient(
        ["https://api1.example:8443", "https://api2.example:8443"],
        token=Path("/run/secrets/dsec-token").read_text().strip(),
        project="root", ca_file="/etc/dsec/api-ca.crt",
    ) as client:
        sandbox = await client.run_microvm(
            resource_demand=NodeDemand(1, 512, 1024, 1))
        try:
            result = await sandbox.run_shell("printf hello")
            print(result)
        finally:
            await sandbox.stop()
```

示例使用节点注册的默认环境。创建沙盒时必须显式声明所需资源。若 API 响应丢失，客户端通过请求 ID 核实原操作。节点内部的 Unix 域套接字（UDS）仅供受信服务使用。`DSecClient(socket_path)` 已退出公共接口，`DSEC_SOCKET` 也不配置正式客户端。同机开发测试可以使用 `from dsec.sdk.development import DevEdgeClient`；旧的 `libdsec_compat` 导入仍保留该开发行为。

需要原生文件、会话或流式输出的应用可以在创建时声明所需执行能力。

```python
from dsec.contracts.native import STREAM_FEATURE, SESSION_FILE_FEATURE
from dsec.sdk import DSecMicroVMRunArgs

sandbox = await client.run_microvm(
    DSecMicroVMRunArgs(environment_id="general-tools"),
    request_id=create_request_id,  # save this 32-lowercase-hex ID before sending
    resource_demand=NodeDemand(1, 512, 1024, 1),
    required_capabilities=[STREAM_FEATURE, SESSION_FILE_FEATURE],
)
```

`run_container` 也接受这个可选字段。系统自动加入对基础原生协议的依赖。旧版 API 在创建前拒绝此类请求。调度时会排除能力不匹配的节点，选中的节点在首次分派前再次检查。收到 `NoPlacement` 表示还没有节点接纳请求。同一创建 ID 不能改用另一组要求，否则会返回冲突。若回复丢失，请查询原 ID。省略能力要求时沿用原行为。使用这一功能前，请升级所有 API 副本。

运维人员必须为选中的环境发布与具体制品绑定的执行能力声明。单独在节点上声明协议版本还不够。旧版默认 smoke 环境没有这样的声明。[发布执行能力声明](ARTIFACT_PUBLICATION.md)。IAM 保留解析后的部署修订号，Edge 会再次核对。该修订号写入创建凭证，不传入 VMM。工具依赖、存储与网络策略以及快照兼容性仍需在准备好的环境中分别验证。FnCall 始终按声明的独立调用语义执行。[通用性设计与验收边界](../architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md)

接下来的宿主步骤准备节点和用于开发验收的独立实例。API/IAM 部署和生产隔离要求另行完成。

<a id="install"></a>

## 1. 安装并选择外部制品

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./dsec_reproduce-0.1.0.dev0-py3-none-any.whl
.venv/bin/dsec-host --help
```

使用 Python 3.11 或更高版本。离线安装时对本地 wheel 添加 `--no-index --no-deps`，核心包运行时无需第三方 Python 依赖。从源码构建需安装 setuptools 77 或更高版本以及 wheel。每台机器都要新建虚拟环境。

| 组件 | 哪些场景需要 | 如何准备 |
| --- | --- | --- |
| Firecracker 与 guest 内核 | 所有 MicroVM | 提供已知哈希的可信二进制。实验版使用 Firecracker 1.17.0 和 6.1.186 内核。 |
| guest 文件系统与命令 agent | 所有任务 | 构建下文的小型测试 guest，或使用所选环境已准备好的构建配方。 |
| KVM 设备权限和 `kvm` 组 | 所有 MicroVM | 管理员一次性授予运行账号访问权限。`sg` 只能刷新已有组成员身份。 |
| Docker、GCC、静态 libc、e2fsprogs | 构建测试 guest | 这些是构建工具。运行已构建的 file-ext4 guest 无需 Docker。 |
| 支持 EROFS 的内核、静态 BusyBox、mkfs 工具 | 准备分层镜像 | 显式提供构建输入。需含 mount、chroot 等 applet，联网 guest 还需 ip。 |
| 实例专用网络 helper | 联网 guest | 管理员审查并安装生成的 helper，准备转发配置和 ip/iptables 工具。 |
| OverlayBD/ublk 服务及设备规则 | OverlayBD 根盘 | 外部 root 服务须允许访问选定的存储路径，设备须允许相应组访问。配置 `UMask=0007`，以便服务组能够回收运行目录。 |
| 3FS 服务与客户端挂载 | `threefs_lazy` 存储 | 按需部署，本地存储路径无需 3FS。 |

安装命令不会下载 guest 内核、镜像、模型、数据集或存储二进制。请记录所选制品的源码修订、许可证及 SHA-256。可选的网络安装器仅负责网络权限。首次部署可先选择无网络的 file-ext4 guest，以后再添加能力，无需安装训练框架或 TB2.1。

<a id="guest"></a>

## 2. 构建独立于 Terminal-Bench 的测试 guest

选择本地已有、来源可信且包含 `/bin/sh` 和挂载工具的 Linux 镜像。先取得精确的 Docker 镜像 ID。如需拉取或导入，由管理员或构建用户预先完成。构建工具导出该镜像的内容，静态编译命令 agent，生成 64 MiB 的 ext4 模板。

```sh
python3 tools/build_smoke_guest.py --image sha256:<local-image-id> \
  --agent-source guest_agent.c --out ./artifacts/smoke.ext4
```

构建阶段需要静态编译支持，全程离线，不启动虚拟机。若目标位置已有模板，构建工具会拒绝覆盖。请保留模板的只读属性，运行中的实例使用各自的私有副本。

<a id="node"></a>

## 3. 配置独立实例

```sh
.venv/bin/dsec-host --config host.json init --instance local \
  --state-root "$HOME/dsec-state" --network-interface eth0 \
  --binary /opt/dsec/firecracker --kernel /opt/dsec/vmlinux \
  --template ./artifacts/smoke.ext4
.venv/bin/dsec-host --config host.json doctor
.venv/bin/dsec-host --config host.json run sandbox --validate-only
.venv/bin/dsec-host --config host.json render --out rendered-units
systemd-analyze --user verify rendered-units/*.service
```

请将网卡和制品路径替换为宿主机的实际值。示例中的小额资源预算用于准入控制，宿主容量可单独配置。运行账号须拥有状态目录、属于指定服务组，并有权遍历各级父目录。状态目录路径须足够短，以满足 Linux Unix 套接字的路径长度限制。

检查生成的服务单元，再将其安装到自己的 systemd 用户服务目录。

```sh
mkdir -p "$HOME/.config/systemd/user"
cp rendered-units/dsec-local-*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start dsec-local-sandbox.service dsec-local-worker.service
.venv/bin/dsec-host --config host.json wait
.venv/bin/dsec-host --config host.json doctor --live
.venv/bin/dsec-host --config host.json smoke --restart-services --out acceptance.json
```

使用尚未占用的实例名，并在独立实例上做重启验收。smoke 测试会保存结果并释放自己的沙盒。停止两个服务前，先通过 `status` 确认队列已空。停止沙盒服务会保留 VMM 以便恢复，因此仍需显式停止自己创建的沙盒。

<a id="workload"></a>

## 4. 完成 API 部署并运行第一个任务

本地 `smoke` 通过后，按[使用外部 Linux 节点部署 Docker 控制器](CLUSTER_DEPLOYMENT.zh-CN.md#docker-preview-with-an-external-linux-node)操作，在节点配置中填入本实例实际的 **Edge 和 worker 两个套接字**。节点声明的环境须包含 `default`。计数器示例至少需要 1 个并发槽、1 个 CPU、512 MiB 内存、1024 MiB 磁盘和 1 Mbps 网络预留。预留带宽时仍可使用无网络 guest。控制器需单独安装核心 wheel；这次固定回复的验证无需任务应用或模型。

1. 生成并审查部署配置，启动三个 etcd 成员，按集群指南运行初始化程序。
2. 将仅供节点使用的部署包传到声明的目录，并保护其中的凭据。启动 Edge、worker、节点网关及两个 API 副本。
3. [让初始节点加入并激活](CLUSTER_DEPLOYMENT.zh-CN.md#dynamic-membership-maintenance-and-retirement)。自动生成的部署使用受管成员资格。即使网关已启动或 Watcher 中出现了该节点，也必须完成激活才能调度。
4. 在源码发行包中，使用生成的运维客户端配置，执行下面的 API 验收命令。

```sh
.venv/bin/python -I tools/verify_rollout_api.py \
  --api-config "$DSEC_PREVIEW_DIR/client.json" \
  --out ./evidence/first-api-use
```

每次使用新的证据目录。省略可选 MBPP/TB2.1 参数时，脚本用包内兼容 Miles 的循环和两条固定策略回复执行计数器任务。检查结果中是否有 `miles_loop_through_api`、得分 1 和两条策略回复。这里验证的是执行与评分链路。脚本仅使用 API 配置，凭据和 CA 引用相对于该配置文件解析；请勿传入 worker 或 Edge 套接字。

生成的 `client.json` 是 root 项目运维人员的私有配置。仅供此次受信预览使用，请为其他调用方单独配置适当范围的凭据。如果返回 `NoPlacement`，请通过 API 检查节点是否为 ACTIVE、是否声明了 `default` 环境、worker 是否健康，以及节点能否满足资源请求。结果不确定时，请使用原 episode ID 核对。停止服务前，通过[资源清单 API 或管理控制台](CLUSTER_DEPLOYMENT.zh-CN.md#remote-native-sdk)确认 episode 已停止、配额和节点租约均已释放。

## 5. 添加任务应用或可选存储

TB2.1/Miles 和 MBPP/verl 通过 `RolloutAPIClient` 和 API 配置文件接入。以往的训练得分来自使用本地 worker 的实验，API harness 的验收单独记录。[案例及适用范围](USE_CASES.zh-CN.md)

[TB2.1 应用](../../apps/tb21/README.zh-CN.md)是单独安装的可选包。只需准备选定任务和制品，注册固定版本的环境目录，再配置专用实例。单独安装核心包即可使用 DSec，TB2.1 留到需要时准备。

对于联网、OverlayBD、3FS 或 DAX 环境，请按[宿主配置](DSEC_HOST_CONFIGURATION.zh-CN.md)显式设置相应字段。root 网络 helper、存储组遍历目录的权限，以及 systemd 允许存储服务写入的范围，需分别配置。不要使用全局可写目录或赋予 worker 任意 sudo 权限。

管理员可以使用 `dsec-artifact-integrity seal` 为自己持有的不可变制品发布 fs-verity 凭据。此功能需要 Linux 及支持 verity 的文件系统。发布时由内核构建 Merkle 树，并核对原有的整文件 SHA-256。运行时读取受保护摘要，内核在读取每一页时检查完整性。凭据、制品及其各级父目录须由 root 拥有，其他账号不能有写权限。原来的整文件哈希校验仍可使用。显式选择 fs-verity 后，缺少凭据或内核保护会导致校验失败。[制品发布与验证范围](ARTIFACT_PUBLICATION.md)

安装后的 r5 候选版验证了对写入、未封存副本和损坏页面读取的拒绝行为，并完成 15 条固定轨迹的任务评分。本次集成覆盖本地验证器磁盘，3FS 和 DAX 尚待单独验证。

## 已验证的范围

发行版已有安装后运行、独立于 Terminal-Bench 的任务、代表性 TB2.1 任务、GRPO、重启与复用，以及资源边界的验收记录。可以从源码构建新的测试 guest，独立于 TB 镜像进行检查。验收复用了实验机已有的 KVM、VMM、内核和可选存储服务，第二台物理机上从零安装全部可选依赖尚未经此验收。[安装验收记录](../reports/DSEC_V01_INSTALL_ACCEPTANCE.md) · [r5 GRPO 分组训练与预备状态隔离验收](../reports/DSEC_V01_GRPO_ACCEPTANCE.md)
