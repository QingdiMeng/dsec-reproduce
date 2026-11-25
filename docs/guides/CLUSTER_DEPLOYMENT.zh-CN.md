<a id="ha-cluster-deployment-development-preview"></a>

# 高可用集群部署（开发预览版）

[English](CLUSTER_DEPLOYMENT.md) · **简体中文**

[kind 高可用演示](../../deploy/cluster/demos/kind-ha/README.md)提供可生成配置的四节点拓扑，以及同时失去一个 DSec API 副本和同域 etcd 成员时的验收步骤。

在已有 Edge 上，本部署增加 IAM、远程 API、资源放置和 Watcher。先安装核心包，再按[宿主配置](DSEC_HOST_CONFIGURATION.zh-CN.md)准备 Linux Edge。guest 镜像、Firecracker、KVM、网络权限、存储、模型服务和训练器均须单独准备。macOS 可以运行控制器，不能充当 Firecracker 节点。

<a id="docker-preview-with-an-external-linux-node"></a>

## 在 Docker 中部署控制面并连接外部 Linux 节点

源码包提供 [Compose 配置](../../deploy/cluster/compose.yaml)、[配置生成器](../../deploy/cluster/prepare.py)、[初始化工具](../../deploy/cluster/initialize.py)和[安装 wheel 的容器镜像定义](../../deploy/cluster/Dockerfile)。此拓扑在同一 Docker 宿主机运行两个 API/控制器副本和三个**真实 etcd 成员**。沙盒 Edge 和可选 rollout worker 运行在单独配置的 Linux 机器上。该拓扑可以测试进程和成员故障；整个 Docker 宿主机故障超出验收范围。此流程不安装 Docker、配置节点或启动训练器。

从源码包构建核心 wheel，准备适用于本机 Docker 架构的不可变镜像引用。验收使用 `python:3.12.15-slim` 和 `quay.io/coreos/etcd:v3.6.5`，均解析到仓库摘要。镜像内通过 `--no-index --no-deps` 安装 wheel；Docker BuildKit 可能仍须访问镜像仓库以解析基础镜像元数据。用专用非 root 服务账号运行生成器。Compose 以该账号的 UID/GID 读取私有凭据。文件归属错误时须修正归属，不要通过 `sudo` 准备部署或放宽凭据文件权限。

使用实际套接字路径及可信的最低资源需求编写节点规格。

```json
{
  "id": "node1",
  "edge_socket": "/srv/dsec/edge/service.sock",
  "worker_socket": "/srv/dsec/worker/worker.sock",
  "max_sandboxes": 2,
  "backends": ["microvm"],
  "environments": ["default"],
  "minimum_demands": {
    "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
  }
}
```

如果仅需访问原生沙盒，可省略 `worker_socket`。只有在安装了相应应用、制品并配置验证器后才声明 MBPP 或 TB2.1 环境。启动 worker 时须保留这些案例设置；例如已准备 TB2.1 缓存时仍须选用对应的 `DSEC_TB2_VERIFIER_ONLINE` 策略。仅复制网关配置不会配置 worker 或提供联网能力。待 Edge 和 worker 健康后再提交应用作业。

选择空闲端口和 Docker 与节点之间可以互访的明确地址。在 macOS 上填写局域网地址。替换下文所有示例地址和路径。`DSEC_PYTHON_IMAGE` 和 `DSEC_ETCD_IMAGE` 必须是包含完整 `repository@sha256:...` 的引用，不能只提供标签。

```bash
python -m build --wheel --outdir dist/core .
export DSEC_PREVIEW_DIR="$HOME/dsec-compose-preview"
python deploy/cluster/prepare.py \
  --out "$DSEC_PREVIEW_DIR" \
  --wheel dist/core/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  --python-image "$DSEC_PYTHON_IMAGE" --etcd-image "$DSEC_ETCD_IMAGE" \
  --project dsec-preview --host-address 192.0.2.10 --node-address 192.0.2.20 \
  --node-directory /srv/dsec/gateway-preview --node-spec /tmp/node-spec.json

docker pull "$DSEC_PYTHON_IMAGE"
docker pull "$DSEC_ETCD_IMAGE"
cd "$DSEC_PREVIEW_DIR"
docker compose build api1
docker compose up -d etcd1 etcd2 etcd3
docker compose run --rm initialize
```

生成器拒绝覆盖已有输出目录。目录中含预览 CA、密码、密钥和凭据，须限制访问。`deployment.json` 记录 wheel、镜像及模板的身份标识，不含原始凭据。初始化工具启用 etcd 认证，仅授予其服务身份对 `/dsec/compose/<project>/` 的访问权。网关客户端证书没有 Common Name，使用显式 etcd token；节点间 TLS 和客户端证书验证保持开启。API 副本和节点均不接收 CA 签名密钥或根密码。服务容器使用隔离的 Compose 网络及卷、只读根文件系统并移除 capabilities，不使用特权模式、宿主网络或 Docker 套接字挂载。仅初始化阶段可写入导出的凭据。

生成目录前，在同一 `prepare.py` 命令末尾添加 `--plan` 可先验证输入，查看拟用的控制器和 etcd 监听地址、节点目标目录、wheel 摘要和 etcd 物理故障域数。规划阶段仅读取拓扑、节点规格和 wheel，不生成凭据也不写文件，无需 OpenSSL、Docker 或已安装的 DSec 运行时。去掉 `--plan` 才生成私有部署文件；安装与启动仍是独立步骤。Python 的 `read_plan(args)` / `render(plan)` 接口也会在写入前拒绝已改变的 wheel。计划是内存描述，不能作为安装回执。

继续支持上面的单节点参数。多个外部节点请用下方的[批量清单](#batch-node-inventory-and-shared-role-configuration)。每个 MicroVM 节点支持只读预检及经审核的受限网络 helper 初始化，见[节点准备](#node-setup)。管理 API 支持按版本注册节点、维护与退出，相关流程见下文。自动升级与回滚仍在规划中。CPU/GPU 混合部署时，GPU 训练和推理服务作为外部 API 客户端接入。此生成器不配置 GPU 调度或沙盒 GPU 直通。

将 `node/` 的**内容**安全传至 Linux 上配置中指定的 `--node-directory`。私有文件归节点服务账号所有，权限设为 `0600`。先启动已有 Edge 和 worker，再启动安装好的网关。

```bash
python -m dsec.host.cluster node --config /srv/dsec/gateway-preview/node.json
```

在 Docker 宿主机上，用初始化生成的凭据启动 API 副本。

```bash
docker compose up -d --no-deps api1 api2
docker compose ps
```

提交工作前先注册并激活初始节点。生成的控制器和节点配置开启受管成员模式。初始化工具引导 IAM 凭据和配额，不自动加入节点。以下命令在运维人员已安装核心包的环境中运行，示例使用新建的 `node1`。

```sh
python -m dsec.host.fleet --client "$DSEC_PREVIEW_DIR/client.json" \
  --out "$DSEC_PREVIEW_DIR/join-node1.json" \
  join --spec "$DSEC_PREVIEW_DIR/node/registration.json"
python -m dsec.host.fleet --client "$DSEC_PREVIEW_DIR/client.json" \
  --out "$DSEC_PREVIEW_DIR/activate-node1.json" \
  activate --node node1 --version 1
```

使用 join 返回的版本，`1` 只适用于示例中的新节点。保留两份意图/结果文件。回复丢失时应先检查已存请求和成员状态，再执行下一次变更。批量模式的输出使用 `nodes/<id>/registration.json`，单节点模式使用 `node/registration.json`。查询状态、维护与退役见[成员工作流](#dynamic-membership-maintenance-and-retirement)。已启动但仍为 JOINING 的节点不能接纳新工作。

只向调用者提供 `client.json` 及其引用的 **API CA 和 IAM token**，复制后按新位置调整文件引用。不要分发整个凭据目录。API 默认端口为 18440/18441，etcd 为 18430–18432，节点网关为 18444。控制台入口为 `/console/`。部署不含入口或负载均衡容器。只读 SDK 调用可切换副本，变更请求回复丢失则须按原 ID 核实结果。

预览证书有效期 30 天，etcd JWT 有效期 24 小时。生成的服务通过权限限于指定前缀的 `dsec` 身份及私有密码文件自动登录，在内存中续期。同进程并发续期会串行执行。续期临时中断时，尚有效的 token 可继续使用；过期或明确被拒的 token 会触发拒绝。仅明确的 HTTP 401 / gRPC UNAUTHENTICATED 允许刷新认证并重发一次。权限不足或传输结果未知不走该路径。API/节点副本续期无需根密码或重启。轮换证书、路由密钥及服务密码仍需运维操作。仅配置 `token_file` 时沿用静态 token 方式。部署输出与数据应一起保留；`docker compose down` 保留数据，`down -v` 会删除单宿主预览中的具名 etcd 卷。跨宿主导出使用私有绑定目录，也须保留。

2026-10-12，两个从 wheel 安装的控制器、启用 TLS 和认证的三成员 etcd 及一台 Linux 物理节点通过 Docker 预览验收。受控丢弃创建或命令回复后，通过第二个 API 找回原结果且无重复副作用；损失一个 etcd 成员时命令仍可执行，损失两个时拒绝新建，恢复后原 VM 和配额连续并正常释放。固定回复的 Miles、MBPP 检查均通过，包括并发二下的八个独立 MBPP episode。这属于 harness 验收，未验证 GPU 训练、多节点调度或物理宿主高可用。原生会话与流式传输需要配套的原生 guest，本次 Docker 验收使用的旧计数器 guest 不支持。另测 TB2.1 `regex-log` 固定轨迹，保留预制离线缓存策略后，原始官方验证器得 1 分并生成完整 CTRF 证据。worker 健康传输失败或放置后失去就绪状态时返回有类型的 UNKNOWN，并保留原准入记录。按保存的 episode 和原节点显式恢复。形式化就绪不变量和受控实现回归覆盖此边界，仅凭 `NOT_FOUND` 不能判定已分派的请求可安全重发。

<a id="batch-node-inventory-and-shared-role-configuration"></a>

## 批量节点清单与统一角色配置

用 `--nodes /path/nodes.json` 替代 `--node-address`、`--node-directory`、`--node-spec` 和 `--node-port`，两种模式不可混用。从随包提供的[清单示例](../../deploy/cluster/nodes.example.json)开始。格式如下。

```json
{
  "schema": 1,
  "root_quota": {"concurrency": 16, "cpu": 32, "memory_mb": 32768, "disk_mb": 65536, "network_mbps": 1000},
  "nodes": [
    {
      "address": "192.0.2.20", "port": 8444,
      "directory": "/srv/dsec/node1-gateway",
      "spec": {
        "id": "node1", "edge_socket": "/srv/dsec/node1-edge/service.sock",
        "max_sandboxes": 8, "backends": ["microvm"], "environments": ["default"],
        "minimum_demands": {
          "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
        }
      }
    }
  ]
}
```

每个 Edge 对应一条记录，填写其实际 IPv4 监听地址、私有网关目录和已准备好的规格。各节点可有不同的容量、环境、最低资源需求及可选 `worker_socket`。仅提供 GPU 模型服务的机器是 API 客户端；除非也运行沙盒 Edge，否则不加入此清单。其 schema 不包含 GPU 调度或模型配置。`root_quota` 是可选的 IAM 策略上限，不是节点容量之和。省略时沿用预览默认值：并发 4、CPU 8、内存 8192 MiB、磁盘 16384 MiB、网络 1000 Mbps。初始化工具和控制器接收相同的配额，最终仍需通过 Edge 的物理资源准入。

规划器会拒绝重复 ID、与其他节点或已声明的控制面/etcd 角色冲突的监听地址、无效的资源字段，以及同地址节点之间重叠的网关、Edge 或 worker 根目录。不同地址可以使用相同路径。这项校验只检查结构，远端宿主是否就绪、网络是否连通、实际资源是否可用需另行验证。

```bash
python deploy/cluster/prepare.py \
  --out "$DSEC_PREVIEW_DIR" \
  --wheel dist/core/dsec_reproduce-0.1.0.dev0-py3-none-any.whl \
  --python-image "$DSEC_PYTHON_IMAGE" --etcd-image "$DSEC_ETCD_IMAGE" \
  --project dsec-cluster --host-address 192.0.2.10 \
  --nodes /path/nodes.json --plan
```

添加 `--topology /path/topology.json` 可采用下文的跨宿主控制器/etcd 布局；省略则仍将控制面放在同一 Docker 宿主机。去掉 `--plan` 才会生成新的私有目录。两种输入都由同一个纯 `configuration.role_configurations` 实现根据已验证的计划生成控制器路由、节点配置、初始化工具、SDK 配置和 Compose 环境。批量模式无需独立运行时。

批量模式为每个节点生成 `nodes/<id>/node.json` 和 `nodes/<id>/secrets/`；旧单节点输入仍生成 `node/`。每个节点有独立的服务器证书及内部 token，控制器路由引用对应 token 文件。节点不会收到其他节点的服务器私钥或 token、控制器私钥、引导或根密码、CA 签名密钥。当前受信节点仍共用路由密钥及前缀受限的 etcd 服务身份，尚不适用于互不信任的节点或租户。批量节点自行认证并续期 etcd 凭据，无需挂载静态节点 token 导出，也无需事后人工复制 etcd token。

将各 `nodes/<id>/` 的**内容**安全传至相应 Linux 宿主上的 `directory`，保留私有权限和预配服务账号的归属。先启动准备好的 Edge 及可选 worker，然后执行以下命令。

```bash
python -m dsec.host.cluster node --config /srv/dsec/node1-gateway/node.json
```

此步骤仅生成初始配置，生成器需要新的输出目录并创建新的身份，不能在已有集群中用它原地加入节点或升级。下文的受限 MicroVM 初始化不负责创建账号、安装内核模块、软件包或存储服务。完整的自动部署及升级回滚仍需另行实现和实机验收。通过管理 API 注册节点只变更成员资格，不重新生成已有集群凭据。

<a id="cross-host-role-exports"></a>

## 按宿主导出角色

每台宿主机安装 Docker Engine 和 Docker Compose v2 或更高版本。生成器及导出器在管理员机器上运行，服务容器不挂载 Docker 套接字。向 `prepare.py` 提供 `--topology /path/topology.json`，配置三个 etcd 成员及两个控制器。以下示例有三个独立地址。

```json
{
  "etcd": [
    {"address": "192.0.2.10", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.20", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.30", "client_port": 2379, "peer_port": 2380, "uid": 1000, "gid": 1000}
  ],
  "controllers": [
    {"address": "192.0.2.10", "port": 8443, "uid": 1000, "gid": 1000},
    {"address": "192.0.2.20", "port": 8443, "uid": 1000, "gid": 1000}
  ]
}
```

使用实际服务账号 ID 和未占用且可达的端口。同宿主服务必须使用相同账号。开发演练允许重复地址，但会减少故障域。本模式不启动总 Compose 文件，应导出按宿主划分的目录。

```bash
python deploy/cluster/export_hosts.py --prepared "$DSEC_PREVIEW_DIR" \
  --out /private/dsec-host-exports
```

仅向每个地址安全传输其对应的宿主目录。旧输入模式另将 `node/` 导出到指定节点目录。批量输入的各宿主导出仅包含该宿主的 `nodes/<id>/` 目录，并通过 `host.json` 映射到配置的目标路径。以节点服务账号将各节点的内容移到相应位置。仅运行节点的宿主不接收控制器凭据、初始化工具或 Compose 服务；其网关是前述独立安装的 Linux 进程。只向调用者分发 `client.json` 及其引用的 API CA 和 IAM 凭据。保留私有权限，每台宿主的部署目录、凭据和 `data/` 均归对应账号所有。不要把总 `pki/` 或根密码复制到每台机器。只有首个控制器宿主接收初始化凭据。导出器本身不启动服务。

在各宿主目录中构建相应的 API 镜像（若有），再启动本机的 etcd 成员。等所有成员都启动，在首个控制器宿主运行 `docker compose run --rm initialize`。确认需要认证的仲裁集群已健康，然后启动节点的 Edge、worker 和网关，最后用 `docker compose up -d --no-deps api1` 或 `api2` 启动各宿主的 API。远端依赖不会列成本地 Compose 服务。在已有数据上重新生成全套密钥并非凭据轮换。

etcd 节点间的服务端证书仍需与地址匹配。每个成员另用 CA 签发的仅限客户端证书标识自身；白名单只允许指定的三个成员 CN。这样能兼容 Docker NAT 对源 IP 的重写，同时保持服务器 TLS 验证，拒绝其他客户端身份。参见 [etcd 节点间 TLS 配置](https://etcd.io/docs/v3.6/op-guide/configuration/)。

2026-10-12 的双宿主验收在两台主机上各部署一个 API 副本。Mac 运行两个 etcd 成员，Linux 运行第三个成员和物理沙盒节点。七项受控检查通过：worker 失去就绪状态时返回有类型的 UNKNOWN、按原 episode 恢复、跨宿主 API 故障切换后仍为同一 VM 且命令仅执行一次、JWT 到期后的长时间运行、少数成员故障、多数成员故障时拒绝协调操作，以及成员重入后配额清零。故障只停止专用服务角色，没有关闭任一宿主机或其 Docker 守护进程。独立认证测试将 JWT TTL 缩短到 12 秒，验证主动续期、服务器因过期拒绝后仅刷新一次，以及前缀权限拒绝。标准部署仍采用 24 小时 TTL。专用 peer 证书通过 TLS 身份的正反例测试。在 Linux API 上使用安装版 wheel 复测八项 harness 检查，包括 Miles 循环、MBPP 样例及八个 episode，以及 TB2.1 原始 regex-log 验证器得 1 分。此轮未进行模型推理或 GPU 训练。

三个成员分布在两台物理机时，一台必然持有多数成员。失去该机即失去仲裁。双宿主能验证跨机路由以及 API 或成员故障切换；若要证明任意单宿主失联时服务仍可用，须增加第三个独立故障域、保持入口可用，并单独进行故障试验。

<a id="prerequisites-and-trust"></a>

## 前提与信任边界

可用入口后至少部署两个控制器副本，并在独立故障域中部署三个 etcd v3 仲裁成员。控制器以 TLS 和 IAM bearer 凭据接收请求。每个节点要求控制器 mTLS 和专用内部 bearer 凭据。etcd 必须验证身份并限制本部署的键前缀。不要开放未经认证的 etcd 网关或私有 Edge 套接字。JSON 网关需要 etcd 签发的 token；配置 `coordination.authentication` 启用自动登录和续期，或用 `coordination.token_file` 指定静态 token。仅凭 TLS Common Name 无法授权，见 [etcd 网关说明](https://etcd.io/docs/v3.6/dev-guide/api_grpc_gateway/)。自动登录时各服务以受限身份续期，凭据过期且无法续期时拒绝协调。受信任的节点服务身份只能证明自己的 Edge 状态。此预览信任所有能访问 IAM 存储的服务，不提供互不信任租户间的隔离。

准备 SAN 与配置地址匹配的 TLS 证书、`subjectKeyIdentifier`、`authorityKeyIdentifier`、正确的密钥用途及可信 CA。节点服务器证书须含 `serverAuth`，控制器访问节点的客户端证书须含 `clientAuth`。密钥和凭据归服务账号所有，权限 `0600`。CA 私钥单独保管。所有副本和节点网关共用一个私有的 256 位路由完整性密钥，以 64 个十六进制字符表示。路由密钥与 token 轮换需显式运维操作，尚未实现自动轮换。

<a id="node-setup"></a>

## 节点准备

新生成的控制器和节点配置会启用 `node_lifecycle`。节点启动后还需显式加入并激活，单凭健康无法接收任务。缺少此设置的旧配置沿用原行为。启用受管成员模式前，先升级全部控制器、节点网关和 episode 服务，不要混用旧准入代码。注册老节点前应先结束升级前的准入请求；结果未决的工作保留原身份和配额。迁移须由运维显式执行。

启动 MicroVM 节点前，使用生成的 `node.json` 和已准备的宿主配置执行[宿主预检与受限初始化](DSEC_HOST_CONFIGURATION.zh-CN.md#commands-and-acceptance)。在准备管理员安装材料前即可发现受限容器缺少 KVM/TUN 访问权限或所需网络能力。声明 `microvm` 的网关也会先复核平台，再创建状态和锁或连接协调存储。控制器和 bootstrap 角色无需 KVM。预检通过后仍须验证真实执行和隔离。

以自己的根目录及 `--node-budget` 配置启动现有 Edge。增设网关前，确认 `health` 已声明 `node-admission-v1`。Edge 预算须覆盖 guest、制品成本，并保留宿主资源压力下限。IAM 配额不能替代物理准入。

创建节点配置 JSON，将示例路径和地址全部换为实际部署值。网关仅声明已在本 Edge 准备好的环境。每个环境有可信的最低需求，防止客户端通过声明零资源规避 IAM 配额。

```json
{
  "coordination": {
    "endpoints": ["https://kv1.example:2379", "https://kv2.example:2379", "https://kv3.example:2379"],
    "prefix": "/dsec/production/v1/",
    "ca_file": "/etc/dsec/etcd-ca.crt",
    "authentication": {
      "username": "dsec", "password_file": "/etc/dsec/etcd-password",
      "renew_before_seconds": 300
    },
    "timeout": 3
  },
  "route_key_file": "/etc/dsec/route-key",
  "node": {
    "id": "node1", "edge_socket": "/var/lib/dsec/edge/service.sock",
    "gateway_root": "/var/lib/dsec/gateway", "max_sandboxes": 8,
    "worker_socket": "/var/lib/dsec/worker/worker.sock",
    "backends": ["microvm"], "environments": ["default"],
    "minimum_demands": {
      "default": {"concurrency": 1, "cpu": 1, "memory_mb": 512, "disk_mb": 1024, "network_mbps": 1}
    }
  },
  "server": {
    "host": "0.0.0.0", "port": 8444, "max_requests": 32,
    "cert_file": "/etc/dsec/node.crt", "key_file": "/etc/dsec/node.key",
    "client_ca_file": "/etc/dsec/controller-ca.crt",
    "node_token_file": "/etc/dsec/node-token"
  }
}
```

```bash
python -m dsec.host.cluster node --config /etc/dsec/node.json
```

同一 Edge 套接字只允许一个网关占有。进程锁绑定 Edge 目录，另一个配置即使选择不同的网关根目录也不能重复占有。网关不新增 Docker/KVM/存储驱动，也不修改防火墙。健康检查的读取有明确超时；回执核对独立执行，慢日志查询不会占用健康探针。

<a id="dynamic-membership-maintenance-and-retirement"></a>

## 动态成员、维护和退出

节点集群变更需要在**根项目**具备 `iam.policy` 权限。租户策略权限不能用于基础设施管理。以下运维客户端使用生成的私有 `client.json`，不运行 SSH、sudo 或直连 Edge。每次变更都会在联系 API Server 前，将公开意图与请求 ID 写入新的 `0600` 结果文件。回复丢失后请保留这些文件，不自动重放变更或更换所有者。

初始节点请使用生成的 `registration.json`。先启动准备好的 Edge、可选的 episode 服务及节点网关，然后执行以下命令。

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out join.json \
  join --spec /private/deployment/node/registration.json
python -m dsec.host.fleet --client /private/deployment/client.json --out activate.json \
  activate --node node1 --version 1
```

从响应中读取实际版本，示例数值仅适用于全新节点。`JOINING` 不接纳工作。激活时须由该节点提供已认证的证明，其中包含可用的 Edge 健康状态。配额准入在同一 etcd 事务中检查 ACTIVE、项目用量和节点在途计数；过期 Watcher 观测无法绕过并发的 drain。

完全新的节点可在无需更新或重启控制器的情况下加入。按批量清单格式写入一条 JSON 记录（`address`、`port`、`directory`、`spec`），使用新身份和独立宿主路径。在持有**原私有总部署和 CA 密钥**的管理员机器上执行以下命令。

```sh
python deploy/cluster/enroll_node.py --deployment /private/deployment \
  --node /private/new-node-entry.json --out /private/new-node-bundle
```

将该包私下传至声明的 Linux 目录。安装同一份已验证核心 wheel，完成宿主预检和受限初始化，再启动 Edge、可选 episode 服务及 `node.json` 网关。join/activate 时使用包内的 `registration.json`。签发工具复用现有 CA 和受限协调凭据，不导出 CA 私钥、引导 token 或控制器身份，也不修改控制器配置。控制器通过 etcd 发现已注册路由，并用有界并发探测。新网关使用 `node_token_mode: derived-v1`，仍需验证 TLS、控制器 mTLS 和各节点专属 HMAC 凭据。etcd 只存公开端点。沿用共享协调存储和路由密钥的设计，节点仍是受信基础设施，不能让租户自行登记。只签发包不会注册节点或增加项目配额。

状态序列为 `JOINING → ACTIVE → DRAINING → MAINTENANCE → ACTIVE`，或 `DRAINING/MAINTENANCE → RETIRED`。通过 API 开始维护。

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out drain.json \
  drain --node node1 --version 2
python -m dsec.host.fleet --client /private/deployment/client.json get --node node1
# Wait for admitted work to finish, or explicitly stop it through its normal API.
python -m dsec.host.fleet --client /private/deployment/client.json --out maintain.json \
  maintain --node node1 --version 3
# Apply the separately reviewed host maintenance while no owned resources remain.
python -m dsec.host.fleet --client /private/deployment/client.json --out resume.json \
  resume --node node1 --version 4
```

Drain 阻止**新的准入**。已有沙盒和 episode 保留原归属，仍可运行或停止。进入维护状态前，IAM 在途准入数须为零，且所属节点须证明 Edge 租约、沙盒、episode 与调度器工作均已清空。ready 池或私有开发资源也可能阻止该证明，须通过实例自身的操作停用并退回准备资源。维护命令不会终止运行中的作业、清空 ready 池、安装权限或升级软件。节点不可达或存在 UNKNOWN 工作时继续阻止维护并保留配额。

节点退出时先 drain，清理后再调用 `retire --node node1 --version VERSION`。退役需要相同证明，单独停止节点进程或删除其配置不足以退役。随后停止已空闲的服务，显式撤销管理员安装。API 只撤销集群成员资格，Linux 文件和 helper 权限须另行清理。节点身份保留为墓碑，物理端点只能由新身份复用。原有回执和沙盒路由保持原归属。若要放弃尚未 ACTIVE 的注册，`withdraw` 会凭“未接纳受控工作”的仲裁证明，将 JOINING 转为墓碑，不据此认定宿主已清理。

每次变更必须提供预期版本。回复丢失后，仅使用保存的原请求 ID、参数和版本核实同一次转换，并写入新的结果文件。若该转换仍是最近一次且已完成，可以找回结果；若其后已发生其他转换，迟到的请求会失败，不会重新激活已退役的节点。模型 `NodeLifecycle.tla` 和受控 CAS 测试覆盖此边界。新旧准入代码混用、从空白主机部署和证书续期需单独迁移和验收。

<a id="compatible-runtime-upgrade-and-rollback"></a>

## 兼容运行时的升级与回滚

此流程升级 Python 核心运行时，保留原节点身份、CA、凭据、路径及日志。仅支持显式声明的集群、成员、协调、Edge 和 episode 兼容契约分别相等的版本。wheel 版本和内容构建 ID 标识软件，成员 CAS 使用独立的版本号。发布方须核实实际兼容性后才能沿用契约版本。状态迁移、etcd、VMM、内核、helper、guest 原生 agent 和模型升级均须另行规划。所有副本须能识别待处理升级。未记录发行身份的部署，应先在维护窗口安装能报告发行身份的基线，不能直接接管或充当自动回滚目标。

使用节点的非特权服务账号，在旧安装旁暂存发布者已验证的核心 wheel。从可信发行渠道取得预期 SHA-256；只对来源不明的下载计算摘要，无法证明其可信。

```sh
python -m dsec.host.releases stage --wheel /private/dsec_reproduce-VERSION-py3-none-any.whl \
  --sha256 PUBLISHED_SHA256 --root /srv/dsec/releases
python -m dsec.host.releases verify --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  > /private/candidate-release.json
```

暂存使用独立虚拟环境及 `--no-index --no-deps`，核对安装内容与 wheel。已完整暂存的相同版本可在验证后复用。中断且缺少 `release.json` 的目录不完整，不会启动或自动复用，应单独保留检查。旧版本原样保留。在观测窗口结束前请保存旧安装及已验证构建引用，只将公开的候选引用传至管理员 API 客户端。

按前文 drain 节点并进入维护状态。读取实际成员版本，再通过具备根项目权限的管理 API 准备升级。

```sh
python -m dsec.host.fleet --client /private/deployment/client.json --out upgrade.json \
  upgrade --node node1 --version MAINTENANCE_VERSION --release /private/candidate-release.json
```

事务要求未释放的准入数为零、所属服务均无资源、软件契约兼容且确实运行源版本。源和目标引用记录在原节点记录中。升级未决时拒绝恢复调度、退役和再次升级。回复丢失后保留原请求 ID 和版本，检查 `node.get` 并核实同一请求，不要另拟升级计划。

通过现有进程管理器，按**网关、episode 服务、Edge** 的顺序停止该空闲节点的服务。仅更新明确指定的服务启动命令，保留 Host/Node 配置和状态根目录。以下每条命令分别在对应服务中运行。

```sh
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role sandbox --config /private/host.json --node-config /private/node.json
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role worker --config /private/host.json --node-config /private/node.json
python -m dsec.host.releases run --release-dir /srv/dsec/releases/CONTENT_BUILD_ID \
  --role node --config /private/node.json --node-config /private/node.json
```

先启动 Edge 并等到健康，再启动已配置的 episode 服务并等到健康，最后启动网关。仅当节点未配置 episode 服务时才可省略 worker。启动器核验已加载内容、成员身份及宿主/套接字绑定，再调用原有宿主组件。容器 agent 使用所选安装版本。此步骤不安装权限或控制其他进程。

启动后用新的结果文件和待处理的成员版本执行 `commit --node node1 --version PENDING_VERSION`。提交操作会查询真实网关、Edge 和已配置的 episode 服务，要求三者证明目标版本正确且无资源占用。启动失败、版本混用、节点不可用或回复 UNKNOWN 时，均不允许恢复调度。提交后需用新版本显式 `resume`。启动和节点准入都会复核已提交构建，启动旧安装无法接收新工作。

要放弃未提交的候选版本，只停止它的空闲进程，按相同顺序用相同配置启动保留的**源版本**。然后执行 `rollback --node node1 --version PENDING_VERSION` 并保留结果文件。回滚需要源版本实际运行的证明，只切换文件或候选进程退出并不足够。回滚后保持 MAINTENANCE，直至显式恢复调度。不要用旧 etcd、Edge 或 episode 状态覆盖当前日志。若已成功提交、仍要回到早期版本，请以保留的兼容版本为目标新建升级；旧计划中迟到的操作须因版本检查失败。

对 Docker 中的控制面副本，先创建独立且不含凭据的镜像构建目录。

```sh
python deploy/cluster/upgrade.py context --deployment /private/deployment \
  --wheel /private/dsec_reproduce-VERSION-py3-none-any.whl --sha256 PUBLISHED_SHA256 \
  --out /private/candidate-image
docker build --build-arg PYTHON_IMAGE=python@sha256:PINNED_BASE_IMAGE_DIGEST \
  --tag dsec-api-candidate:VERSION /private/candidate-image
# Use the exact pinned base from candidate-image/build.json.
# source-release.json is the release field of authenticated source API health.
# Record immutable local image IDs for both the old and candidate images.
python deploy/cluster/upgrade.py plan --deployment /private/deployment \
  --source-release /private/source-release.json --target-release /private/candidate-image/release.json \
  --source-image sha256:OLD_IMAGE_ID --target-image sha256:NEW_IMAGE_ID --out /private/api-upgrade
docker compose --project-directory /private/deployment -f /private/deployment/compose.yaml \
  -f /private/api-upgrade/upgrade-api1.json up -d --no-deps --no-build api1
```

变更 api2 前，先确认 api1 的**已认证**健康结果报告准确的目标版本，查询 IAM 和节点状态，并验证工作仍可到达原所属节点。只读就绪探测可在有界超时内重复，受保护的变更不可重放。通过后才应用 `upgrade-api2.json`。如失败，保留健康副本并检查故障副本。显式回滚使用 `rollback-api1.json`/`rollback-api2.json`，仍须逐个进行。覆盖配置只用 `pull_policy: never` 固定对应 API 镜像，不替换配置、凭据、etcd 成员或卷。该工具只生成计划，不调用 Docker。两个 API 副本同机运行仍无法证明宿主故障可用性。

<a id="certificate-and-credential-rotation"></a>

## 证书与凭据轮换

轮换由运维人员显式执行。SDK、控制器和节点网关在每次请求或连接时重载选中的安全文件。启用后，下述轮换无需重启这些进程或重建沙盒。撤销之前已接纳的操作可继续完成，此后接纳的请求须使用当前信任材料和凭据。当前没有全局分发或确认服务，撤销前须逐一验证各副本和节点。私有清单、密码和密钥归服务账号所有，权限 `0600`；目录权限 `0700`。请勿记录或粘贴密钥值。

对于已生成的受管部署，先准备各角色的补丁。

```bash
python deploy/cluster/rotate.py enable --deployment /private/deployment \
  --out /private/rotation-enablement
```

此步只写出补丁，不安装文件或重启服务。复制时保留文件权限。分别安装各节点的私有清单和已修改的 `node.json`，仅重启其网关。再安装控制器清单及配置，每次滚动一个 API 副本。保持 Edge 和 episode 服务运行。首次启用会改配置，所有参与方都需运行支持该功能的版本。迁移期间，`allow_legacy: true` 仍接受先前的每节点凭据，等所有发送方使用独立节点认证密钥环后再关闭。路由签名沿用 `route-key`，已有沙盒和 episode ID 继续有效。

每份清单含一个正整数 `generation`，所引用文件的绝对路径位于使用该文件的服务命名空间。将新密钥、证书与信任链写入新的私有文件，然后用下一代号和显式预期代号发布引用。

```bash
python -m dsec.host.credentials publish \
  --kind tls-client --path /run/dsec/client-tls.json \
  --document /private/next-client-tls.json --expected-generation 1
```

支持 `tls-client`、`tls-server`、`node-auth` 和 `etcd-identity`。发布时验证材料、锁定目标、校验预期代号并原子替换清单。重复发布完全相同的已选值是幂等的。运行中的读取方拒绝更早代号，也拒绝同代的材料或清单改写。服务重启后私有发布清单仍是权威记录。不要恢复旧文件撤销轮换；重叠期内有意回滚也须使用**新的代号**。

Docker 角色以只读方式挂载文件时，在宿主机通过显式映射发布。

```bash
python -m dsec.host.credentials publish --kind tls-server \
  --path /private/deployment/secrets/controllers/server-tls.json \
  --document /private/next-server-tls.json --expected-generation 1 \
  --reference-prefix /secrets \
  --material-directory /private/deployment/secrets/controllers
```

发布者在宿主私有目录中验证直属文件，在发布文档中保留 `/secrets/...` 引用。路径逃逸和符号链接都会被拒绝，服务不会获得写权限。先安装全部文件再发布引用。该映射也支持 `etcd-identity`，节点认证文档无需路径映射。这避免依赖 Docker Desktop 对绑定根目录不同的 UID 映射。不要放宽文件归属或文件与目录检查来处理命名空间差异。

<a id="api-access-tokens"></a>

### API 访问 token

协议只允许根项目 IAM 凭据管理员使用。prepare **先于 API 调用**写入新的私有 token、替代客户端配置和操作意图，即使回复丢失也要保留全部输出。

```bash
python -m dsec.host.credentials prepare-api --client /private/client.json \
  --out /private/api-token-r2
python -m dsec.host.credentials check-api \
  --client /private/api-token-r2/client.json
python -m dsec.host.credentials get-api --client /private/api-token-r2/client.json \
  --rotation-id ROTATION_ID
python -m dsec.host.credentials complete-api \
  --client /private/api-token-r2/client.json --rotation-id ROTATION_ID \
  --out /private/api-token-r2/completion.json
```

使用 `intent.json` 中的 `rotation_id`；结果未知时不得改用新身份。prepare 后两个凭据暂时都有效。complete 须使用**新凭据**，在同一 IAM 事务中撤销旧凭据。cancel 须使用**旧凭据**，撤销候选凭据。要放弃重叠期，请使用 `cancel-api`、相同参数及新的输出路径。回复丢失时查询状态，CLI 不会自动重试未知变更。迟到的 cancel 不能恢复已经撤销的旧凭据。IAM 台账和审计不记录原始 token。

以私有方式分发替代客户端配置和 token。客户端配置 `token_file` 后，每次调用都会读取选定的文件；原子替换文件即可让已有 SDK 对象切换凭据。不得用已撤销 token 重新 bootstrap，也不要重设 IAM 台账让旧凭据重新生效。请保留另一名已授权管理员，以免操作失误后无法恢复。

<a id="node-authentication-and-tls"></a>

### 节点认证与 TLS

节点认证使用以下独立清单，示例值仅作占位。实际使用时生成相互独立的 256 位随机密钥。

```json
{"generation":2,"active":"old","keys":{"old":"64_HEX_DIGITS","new":"64_HEX_DIGITS"},"allow_legacy":false}
```

最多保留四个密钥。先向所有接收节点发布重叠密钥，再向控制器发布；在后续代号中将控制器 `active` 切到 `new`。逐条检查路由后，才发布移除 `old` 的新代号。错误时不会回退到旧密钥。长期路由签名独立于节点认证，不随此操作轮换。

显式签发新的叶证书。

```bash
python deploy/cluster/rotate.py issue --ca CA.crt --ca-key CA.key \
  --cn NAME --host HOST --usage serverAuth --out PRIVATE_NEW_DIRECTORY
```

控制器/etcd 客户端使用 `clientAuth`。服务器证书每个 SAN 都要指定一次 `--host`。可选 `create-ca --cn NAME --out PRIVATE_DIRECTORY` 创建私有签发机构，其私钥只留在管理员处。更换 DSec CA 时，先扩展通信双方的信任集合，再切换客户端和服务端叶证书，逐对验证后才移除旧 CA。API 客户端和控制器到节点的清单使用 `ca_file`，可选 `cert_file`/`key_file` 及 `peer_sha256` 叶证书固定值。服务器清单使用 `cert_file`、`key_file`、可选 `client_ca_file` 和可选客户端叶证书固定值。若保留同一 CA 却需撤销旧叶证书，须显式固定指纹；签发新证书本身不会撤销该 CA 仍有效的其他证书。

`python -m dsec.host.credentials check-api --client CLIENT.json` 报告服务端 TLS 当前选定代号和公开指纹。`python -m dsec.host.fleet --client CLIENT.json security --node NODE` 经 API 查询节点。逐个检查每个 API 端点，对比预期指纹，避免首个健康副本掩盖未轮换的副本。即使某连接在撤销信任或指纹固定值前已完成握手，接纳应用请求前仍会复查。请求不会用旧身份自动重发。

启用后签发新节点包须显式提供 `--ca CURRENT_CA.crt --ca-key CURRENT_CA.key` 参数。导出的内容包括当前选定的受限账号、TLS 和节点认证清单，不沿用旧引导凭据。工具不会推断或导出签发者私钥。

<a id="etcd-service-identity"></a>

### etcd 服务身份

etcd 管理员认证材料与服务导出分开保管。使用管理员 `EtcdStore` 选项 JSON 创建新的前缀受限账号。

```bash
python -m dsec.host.etcd_rotation --admin-config /private/etcd-admin.json \
  prepare --username dsec-r2 --out /private/etcd-account-r2
```

工具检查现有角色是否恰好具有本部署独立前缀的读写权。创建账号前先记录新密码及意图，管理员变更结果未知时不自动重试。先检查账号、角色状态和保存的意图再核实结果。向控制器和节点只分发新的受限密码。发布包含 `generation`、`username` 和绝对路径 `password_file` 的 `etcd-identity` 清单，选中的身份随之替换，已缓存的旧 JWT 立即失效。新登录失败时不尝试旧账号。确认所有服务均能用新账号访问原台账，再撤销旧账号。

```bash
python -m dsec.host.etcd_rotation --admin-config /private/etcd-admin.json \
  retire --username dsec --replacement-config /private/etcd-new-service.json \
  --out /private/etcd-account-r2/retirement.json
```

替代配置使用同一前缀以及受限账号。retire 拒绝 `root`，先检查替代账号的角色和认证后的台账访问权，才删除旧服务用户。仅当收到准确的过期认证修订拒绝（HTTP 400/code 3）时，才用选中身份登录并重发一次，因为 etcd 在执行获授权的变更前就会返回该错误。普通校验错误、权限拒绝和未知回复不走此路径（[etcd 错误定义](https://github.com/etcd-io/etcd/blob/v3.5.21/api/v3rpc/rpctypes/error.go)、[应用前授权](https://github.com/etcd-io/etcd/blob/v3.5.21/server/etcdserver/apply_auth.go)）。etcd 基于自身当前的认证状态验证并授权 token 请求，缓存 token 并非独立授权（[etcd 认证设计](https://etcd.io/docs/v3.5/learning/design-auth-v3/)）。通过 TLS 清单更新 etcd 客户端证书不会替换服务密码或撤销旧证书。更换 etcd 节点间或服务器 CA，以及 JWT 签名根，均应按成员逐个操作，见 [etcd 传输安全](https://etcd.io/docs/v3.6/op-guide/security/)。

2026-10-24，从 wheel 安装的版本在两个 API 副本、Mac 上三个真实 etcd 成员和一个 Linux 节点上通过 21 项检查。轮换节点密钥、IAM token、API/节点 CA 和叶证书、etcd 客户端叶证书及受限账号后，同一容器、标记及 SDK 对象均保持有效。已退役身份被拒，五个控制面进程和三个节点进程的启动时间保持不变。IAM 在途用量归零，节点退役验证了资源已清空。Linux 宿主原有的五个容器保留原进程身份。验收覆盖这一有限拓扑与路径，未验证独立宿主 HA 或自动全集群发布。首次试验暴露了 etcd 认证修订过期的处理问题；修复精确拒绝分支后，用新 wheel 完整重跑。

<a id="controller-setup"></a>

## 控制器准备

各控制器共用上文相同的 `coordination` 段和 `route_key_file`。添加下列配置，启动至少两个独立实例，证书须匹配各实例对外地址。

```json
{
  "bootstrap_token_file": "/etc/dsec/bootstrap-token",
  "root_quota": {"concurrency": 16, "cpu": 16, "memory_mb": 8192, "disk_mb": 16384, "network_mbps": 16},
  "nodes": {
    "node1": {
      "endpoints": ["https://node1.example:8444"],
      "token_file": "/etc/dsec/node1-token",
      "tls": {
        "ca_file": "/etc/dsec/node-ca.crt",
        "cert_file": "/etc/dsec/controller-client.crt",
        "key_file": "/etc/dsec/controller-client.key"
      }
    }
  },
  "watcher_poll_seconds": 2, "watcher_max_age": 10,
  "placement_choices": 2,
  "server": {
    "host": "0.0.0.0", "port": 8443, "max_requests": 32,
    "cert_file": "/etc/dsec/api.crt", "key_file": "/etc/dsec/api.key"
  }
}
```

将随机引导 token 写入私有文件，仅初始化 IAM 一次。

```bash
python -m dsec.host.cluster bootstrap --config /etc/dsec/controller.json
python -m dsec.host.cluster controller --config /etc/dsec/controller.json
```

只有身份和根配额均相同时，bootstrap 才幂等，不会隐式更改现有配额。委派、策略及凭据操作通过已认证的 `/v1/call` JSON 封装访问，须具备对应的 `iam.*` 权限。子预算计入父项目配额，子权限不能超出委派方或祖先策略上限。创建凭据接收调用方生成的 `new_token`，因此回复中断时不会留下调用方从未知晓的凭据。etcd 不存原始凭据，成功的 IAM 变更会原子记录审计条目。

生命周期请求封装均包含 `project`、32 位小写十六进制 `request_id`、`operation`、`sandbox_id`（创建时为 null）及对象 `args`。创建另需显式 `resource_demand`。公共 `DSecClient` 构造这些请求，只连接验证过的 HTTPS API 端点。必须配置 IAM 凭据和项目，不回退到 `DSEC_SOCKET`。`ClusterClient` 仍是相同 API 实现的别名。本地测试及节点内部 worker 从 `dsec.sdk.development` 显式导入 `DevEdgeClient`，旧版 `libdsec_compat.DSecClient` 保留原行为。

<a id="optional-management-console"></a>

## 可选管理控制台

在控制器副本启用 `server.console: true`，可通过 `/console/` 访问控制台。IAM、浏览器信任、操作和恢复边界见[控制台指南](CLUSTER_CONSOLE.zh-CN.md)。此功能默认关闭，节点网关无法托管。

<a id="remote-native-sdk"></a>

## 远程原生 SDK

对 Miles 和 verl 使用的 rollout/任务 API，还需配置节点的可选 `worker_socket`。它必须是使用同一 Edge、有持久记录的调度 worker。网关在核对绑定及健康状态后才声明 `rollout-api-v1`。为各应用环境登记合适的可信最低需求。MBPP 的请求为 1 CPU、512 MiB 内存、256 MiB 磁盘且无需网络，请为 `python-mbpp` 设置匹配的下限或提高应用请求。TB2.1 的环境和验证器另在节点上配置。

向应用提供引用凭据的客户端 JSON 文件。

```json
{
  "endpoints": ["https://api1.example:8443", "https://api2.example:8443"],
  "project": "root",
  "token_file": "/run/secrets/dsec-token",
  "ca_file": "/etc/dsec/api-ca.crt"
}
```

token 文件须归客户端用户所有，权限 `0600`。相对文件引用按 JSON 所在目录解析。请在训练或奖励 worker 上准备这些文件。不要将原始 token 写入 Hydra 覆盖参数或回执。

```python
from dsec.sdk import RolloutAPIClient

async with RolloutAPIClient.from_config("/run/dsec/api-client.json") as client:
    episode = await client.attach(saved_rollout_id)
    dialogue = await episode.dialogue()
```

调用路径为应用 → API Server → 已认证节点网关 → 私有 rollout worker → Edge → 沙盒。worker 管理步骤、对话、验证器证据和恢复。创建请求分派前先预留 IAM 配额并确定唯一节点。公共 rollout ID 在 worker 中按项目隔离。分派后即使收到 `NOT_FOUND`，也不能再次发送创建请求；可经任一 API 副本调用 attach/status 查询原所有者。已释放的准入无法重建 episode。准入前明确收到 `NoPlacement` 时，可用同一 episode ID 等待，剩余 RPC/放置预算逐次减少。UNKNOWN 和其他错误不走该重试路径。时限应覆盖 TB2.1 验证器预算，健康检查使用独立的短时 worker 通道。UNKNOWN 不能转成零奖励或触发换节点调度。须取得权威的物理清理结果后才释放配额。worker 持有的沙盒必须通过 `rollout.*` 操作才能维持一致的资源记录。

对已保留的准入记录，运维人员或应用可以显式调用以下接口。

```python
episode = await client.recover_create(saved_rollout_id)
```

恢复会重新校验项目和基线访问权，在已固定的节点上复用保存的规范化创建参数。健康恢复后，`ADMITTED` 可以跨过原来的首次分派检查点。`DISPATCHED`/`BOUND` 只查询原 worker 记录，不重发创建。已释放准入不能重新启动。已分派创建若长期 UNKNOWN，继续占用配额，需运维人员提供更多证据。当前 worker 不支持取消排队中的 rollout。验证器失败详情保留在持久记录，可通过 status 读取；API 不开放任意宿主文件。每对 Edge/worker 只由一个集群管理。以上协议是本项目的扩展，不能视为论文所用的线协议。

GPU 训练前先运行固定回复的 harness 检查。

```sh
python tools/verify_rollout_api.py --api-config /run/dsec/api-client.json \
  --mbpp-environment python-mbpp --samples 8 --concurrency 2 \
  --out /data/evidence/api-use-case-unique
```

使用该选项时安装 `apps/mbpp`。要检查已准备的 TB2.1 任务及其官方验证器，请一并添加 `--tb21-tasks`、`--tb21-catalog` 和 `--tb21-trajectory`，并安装 `apps/tb21`。启动模板须匹配该任务有序层清单及命令、验证器预算，不能直接复用 MBPP smoke 模板。若要求离线验证，须准备完整缓存并显式配置环境；仅挂载缓存不能保证任务可离线运行。固定回复验证 harness，不评估模型准确率、梯度或训练收益。

```python
import asyncio
from pathlib import Path
from dsec.sdk import DSecClient
from dsec.contracts.resources import NodeDemand

async def main():
    async with DSecClient(
        ["https://api1.example:8443", "https://api2.example:8443"],
        token=Path("/run/secrets/dsec-token").read_text().strip(),
        project="root", ca_file="/etc/dsec/api-ca.crt",
    ) as client:
        sandbox = await client.run_microvm(resource_demand=NodeDemand(
            cpu=1, memory_mb=512, disk_mb=1024, network_mbps=1))
        try:
            session = await sandbox.open_session()
            await sandbox.write_file("/tmp/input", b"binary\x00data")
            result = await session.run_shell("wc -c /tmp/input")
            print(result)
            await session.close()
        finally:
            await sandbox.stop()

asyncio.run(main())
```

在调用方的作业状态中持久保存沙盒、会话及操作 ID。变更回复丢失时 SDK 抛出 `RequestOutcomeUnknown`，不会转发到另一 API 重试。重新连接后，创建用 `lookup_request(create_id)` 查询，命令用 `lookup_request(command_id, sandbox_id=...)` 或 `session.query(command_id)` 查询，流式会话通过 `session.events(stream_id, cursor=...)` 重连。只读调用可在配置的多个端点间切换。

发送创建请求前，IAM 固定节点，节点持久记录 `DISPATCHED`。如果 Edge 日志仍为 `NOT_FOUND`，说明结果未知，配额继续保留，因为请求可能还在队列中。若 Edge 明确在产生副作用前因资源或准入拒绝，则关闭该准入记录；检查实际容量后以**新的**操作 ID 重试。预览版不会隐式将现有准入改派到别处。失联节点被移出候选，其已有准入仍占用配额。不能把健康检查失败视为 VM 已停止的证明。

<a id="live-episode-service-restart-acceptance"></a>

## 运行中 episode 的服务重启验收

`tools/verify_rollout_restart.py` 在运维人员控制的服务重启前后核对一条已提交的 Agent 动作。请用独立项目和 Edge，确保没有其他已接纳作业。工具创建计数器 episode，持久保存对话，以同一公共 ID 重连，并显式重提相同的已提交动作标识，验证日志去重。随后执行下一步 Agent 动作，运行计数器验证器，停止 episode，并核对清理结果。

提供私有 JSON 计划，用参数数组显式指定命令。命令由运维人员为专用部署提供。工具不调用 shell、不安装服务、不挑选 PID，也不修改宿主 Docker 或 Grafana。下列命令请替换为审阅过的可执行脚本。

```json
{
  "inspect": ["ssh", "node1", "/srv/dsec/acceptance/inspect-vmm", "{rollout_id}"],
  "inspect_outage": ["ssh", "node1", "/srv/dsec/acceptance/inspect-quota", "{rollout_id}"],
  "phases": [
    {"name": "all_api", "stop": [["/srv/dsec/acceptance/stop-apis"]], "start": [["/srv/dsec/acceptance/start-apis"]]},
    {"name": "gateway_worker", "stop": [["/srv/dsec/acceptance/stop-gateway-worker"]], "start": [["/srv/dsec/acceptance/start-gateway-worker"]]},
    {"name": "api_node_edge", "stop": [["/srv/dsec/acceptance/stop-api-node-edge"]], "start": [["/srv/dsec/acceptance/start-api-node-edge"]]}
  ]
}
```

只读 `inspect` 命令须输出一个 JSON 对象，其 `physical` 中包含 `sandbox_id`、正整数 `pid`、`/proc` 的 `start_ticks` 和 `boot_id`；另附 `held_concurrency`、`edge_leases`、`admission_state`。将公共 episode 的 worker 持久记录解析至物理沙盒。停止后 `physical` 可为 null。可选的 `inspect_outage` 仅从已认证 IAM 存储返回 `held_concurrency` 和 `admission_state`。这是独立的节点/运维检查，提供物理证据，应用仍只使用 API。检查不得执行 guest 工作。停止和启动命令须幂等，仅作用于自身服务，45 秒内完成。启动命令返回后服务须能独立存活。整个试验期间 etcd 保持运行。

```bash
python tools/verify_rollout_restart.py --api-config /run/dsec/api-client.json \
  --plan /private/restart-plan.json --out /data/evidence/service-restart-unique
```

报告保留局部失败、源码和计划标识及命令日志。要求 VMM PID、进程启动时间及 boot ID 均保持一致，所存历史与对话完全匹配，配额和 Edge 租约均保持为一，直至权威清理将其归零。此项验证只涉及一条动作已提交、当前静止的运行中 episode，未验证被中止的在途验证器、原生流、模型会话或物理宿主的恢复。

2026-10-12，双宿主安装版在三种重启组合下通过九项检查：两个 API 副本；网关和 worker；两个 API 副本加网关、worker 和 Edge。每种组合都保留同一 VMM PID、启动 tick、boot ID、完整一致的已提交历史与对话，以及一个未释放的准入和租约。中断时返回有类型的 UNKNOWN，配额未释放。恢复后下一步和计数器验证器成功完成，奖励为 1；清理后配额和 Edge 租约均归零。etcd 仲裁和物理主机一直运行。早期工具错误来自对对话步骤、旧命令超时和计数器奖励字段的错误假设，未发现对应的运行时恢复失败。保留失败报告后重跑了修正版工具。另有协议回归使用真实 API 和 worker，搭配模拟的服务控制与执行，单独检查这些接口假设。

<a id="validation-and-remaining-limits"></a>

## 验证与剩余边界

`tools/verify_cluster_node.py` 可针对已准备的节点启动两个独立本地控制器，并在创建、命令和流的指定回复边界关闭接纳请求的控制器。工具会回收自身创建的一个沙盒。例如，在所提供配置中指定两个空闲 API 监听端口后运行以下命令。

```bash
python tools/verify_cluster_node.py --root /tmp/dsec-ha-acceptance \
  --config-a /etc/dsec/test-controller-a.json \
  --config-b /etc/dsec/test-controller-b.json \
  --token-file /etc/dsec/test-token --ca-file /etc/dsec/api-ca.crt
```

用 `tools/verify_cluster_control.py --help` 做隔离的三成员 etcd 故障测试，用 `tools/check_cluster_refinement.py --help` 验证规定的代码变异。确切验收条件见[设计和验收记录](../architecture/CLUSTER_CONTROL_DESIGN.zh-CN.md)。本地仲裁和两个控制器进程可以验证进程及成员故障，不能证明物理宿主失效后的可用性。投入生产前需将入口、控制器和仲裁成员分布在独立故障域中。

本预览尚不支持自动配置、外部 OIDC 集成、路由密钥轮换、动态调整配额，或对长期未知分派进行人工恢复的完整流程。基线创建路由、旧 TB2 容器后端和项目磁盘 I/O 配额会被显式拒绝。原生 SDK 为通用接口，TB2.1 和 RL 适配器均为可选应用。
