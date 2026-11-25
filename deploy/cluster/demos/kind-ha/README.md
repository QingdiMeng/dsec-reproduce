# DSec 控制面 HA · kind 演示设计

目标是在**一个 Linux 宿主**上重现一个逻辑节点的整体失联。用户在真实 MicroVM 上执行一次操作，失去最初的 API 回复后，经存活 API 查回同一个结果。验收关注 DSec 的请求身份、可写实例数、配额和恢复时间。这里的 Kubernetes 集群提供实验拓扑，宿主上的 DSec Edge 运行工作负载。

TASK-027 提供配置与测试设计，TASK-028 完成单宿主 H0–H3 实机复核。TASK-029 完成 H4 的 inotify 定位与两种 leader 位置的恢复复测。TASK-004 的三物理故障域验收仍开放。

## 拓扑和入口

| kind 节点 | 工作负载 | 宿主 SDK 入口 | 宿主 Node 的 etcd 入口 |
| --- | --- | --- | --- |
| control-plane | Kubernetes 自身控制面和自己的 etcd | — | — |
| worker a | api-a、etcd1、专用 PVC | 127.0.0.1:25443 | 127.0.0.1:25379 |
| worker b | api-b、etcd2、专用 PVC | 127.0.0.1:25444 | 127.0.0.1:25380 |
| worker c | api-c、etcd3、专用 PVC | 127.0.0.1:25445 | 127.0.0.1:25381 |

每个工作负载用 `dsec.demo/domain` 精确选择 worker。API 和 etcd 各有一个 `externalTrafficPolicy: Local` 的 NodePort，kind 给每个 worker 建立单独的回环端口映射。这样可明确指定最初受理请求的副本、故障副本和接续副本。三个 etcd 成员用固定的专属 headless Service 发现彼此；独立 PVC 跟随原 worker 恢复。实验停止和重新启动**同一个** worker 容器，保留其节点身份及本地卷。四个 kind 节点共享宿主内核和电源。

DSec NodeGateway 和 Edge 留在宿主，通过专用实例目录提供一台 MicroVM 的容量。API Pod 经 mTLS 连接 NodeGateway 的宿主可达地址。DSec SDK 在宿主上使用三个 HTTPS 入口，正常业务验收还需检查客户端持有的 CA、项目凭据和实际节点身份。Kubernetes 节点、DSec 工作节点、etcd 投票成员分别计数。

## 准备清单

从仓库根目录执行。设 `OUT` 为本次独占的空私有目录；`KIND_IMAGE`、`ETCD_IMAGE` 和 `API_IMAGE` 为可在实验机取得的带 SHA-256 的镜像引用。API 镜像需要以本次验收 wheel 和仓库的集群 Dockerfile 构建，包含 Python、DSec 及 `/opt/dsec-initialize.py`。为离线环境预载并检查每个 worker 上实际可解析的镜像摘要。记录 kind、kubectl、Kubernetes、etcd、wheel 和镜像版本。

```bash
DEMO=deploy/cluster/demos/kind-ha
python3 "$DEMO/render.py" kind > "$OUT/kind.json"
python3 "$DEMO/render.py" quorum --image "$ETCD_IMAGE" > "$OUT/quorum.json"
python3 "$DEMO/render.py" initialize --image "$API_IMAGE" > "$OUT/initialize-job.json"
python3 "$DEMO/render.py" api --image "$API_IMAGE" > "$OUT/apis.json"
```

角色资产来自[部署准备工具](../../prepare.py)的一份新建、单 Node 的 demo 输出。使用独立的项目、IAM 前缀、CA、端口及 Node 根目录。宿主 etcd 地址用 `--host-address 127.0.0.1 --port-base 25379`；`--node-address` 选 **Pod 可达的宿主地址**，和 Node 服务证书 SAN 一致。生成的控制面 Pod 内部继续使用 `etcd1`、`etcd2`、`etcd3` 短名称。该生成器的客户端默认 API 端口属于 Compose 布局，demo 客户端应改用上表三个回环地址。完整的 Node 启动和准入流程见[集群部署指南](../../../../docs/guides/CLUSTER_DEPLOYMENT.md)。

| demo 输入 | 来源与处理 |
| --- | --- |
| ConfigMap `role-config` | 从输出目录复制 `config/controller.json` 和 `config/initialize.json` 到 demo 专用配置目录。controller 的 coordination 删除 `token_file`、保留认证用户名和密码文件；initialize 设置 `export_node_token=false`。初始化后的 API 和宿主 Node 使用既有的 etcd 密码认证和续期流程。宿主 demo 专用 `node.json` 的 coordination 同样删除 `token_file`、保留 authentication。 |
| Secrets `etcd1-tls`、`etcd2-tls`、`etcd3-tls` | 各自取 `secrets/etcdN` 的成员证书、密钥、CA、健康检查和 JWT 文件。逐一核对成员证书包含对应短名称与宿主访问用的 127.0.0.1。 |
| Secrets `api-secrets`、`init-secrets` | 分别来自 `secrets/controllers` 和 `secrets/initialize`。根口令只挂载到一次性初始化 Job。DSec 校验凭据所有者、模式与文件类型，initContainer 将投影文件复制到内存卷，创建为运行 UID 1000 所有的私有普通文件。CA 签发私钥始终保留在集群外。 |
| ConfigMap `checkpoint-driver` | 以原文件名装入仓库的 [verify_cluster_node.py](../../../../tools/verify_cluster_node.py)。使用现有 `--child-config` 入口，只有演示用 API 进程安装回复前检查点。 |
| 宿主 Edge | 独立的测试沙盒目录、KVM、启动制品及准入预算。Node 通过上表的三个 etcd TLS 端点读写同一前缀。记录实例基线、原有宿主服务和租约。 |

在独立 kubeconfig 中建名为 `dsec-ha-demo` 的 kind 集群。例如 `kind create cluster --name dsec-ha-demo --image "$KIND_IMAGE" --config "$OUT/kind.json" --kubeconfig "$OUT/kubeconfig"`。之后每条 kubectl 命令指定该 kubeconfig 和 context `kind-dsec-ha-demo`。先创建同名 namespace、角色 Secrets 和两个 ConfigMap。使用 `kubectl create secret generic ... --from-file=<角色目录>` 直接导入，避免把凭据写入清单、日志或 Git。

按 **quorum → initialize → api** 顺序 apply 对应文件。进入下一阶段前分别验证三成员 peer TLS 和同一 cluster ID、初始化 Job Complete 且认证已启用、三个 API 各自通过带凭据的 SDK 健康检查。按部署指南启动并激活专用 Node，验证三个副本看到同一可调度 Node。启动证明应包含 Pod UID、Pod 到 worker 的归属、etcd member ID 与 leader、每个 NodePort 的本地端点。TCP readiness 只用于流量转发；判分使用经过认证的实际操作。

## 故障测试协议

现有[三成员检查](../../../../tools/verify_cluster_control.py)覆盖 IAM 与 etcd 多数派，[原有回复丢失检查](../../../../tools/verify_cluster_node.py)覆盖两个 API 进程与一个真实 Node。本轮新增的变量是**同一 worker 上 API 和 etcd 同时失联**。下面各场景串行运行，每轮开始时恢复三个成员和所有 API。

| 场景 | 动作 | 可判定的通过条件 |
| --- | --- | --- |
| H0 健康基线 | 通过各入口发现同一 Node；创建一个 MicroVM，在 guest 写入随机标记，读回并停止。 | 取得真实 SDK/Edge 回执，IAM 与物理租约回到基线，三个 etcd 成员健康。 |
| H1 丢回复＋失去 follower 域 | 找一个 follower 所在 worker，只向该 worker 的 API 发创建请求；到达回复前检查点后停止整个 worker。通过存活入口用原 request ID 查询并接回沙盒。 | 原提交可查，沙盒 ID 和 owner 一致，只有一条 Edge 创建记录、一个物理实例和一份准入。可执行命令并读回标记。停止该实例释放容量后，继续在故障未恢复时以新 ID 创建并停止另一个实例。 |
| H2 丢回复＋失去 leader 域 | 恢复健康后重新查实际 leader；在该域的 API 重复 H1。在整个 leader 失联期间采样剩余成员的 leader 和 SDK 结果。 | 剩余成员选出新 leader，原请求可查且无重复执行，新请求可受理。报告选举与业务恢复耗时，保留选举窗口内的错误。 |
| H3 恢复 | 启动被停止的同一个 kind worker，等待本地 etcd 数据卷和 API 再次可用。 | 三个原 member ID 和 cluster ID 保持一致，三个 API 均能查询历史证据；已释放的请求无新增实例和收费。 |
| H4 多数派丢失（扩展） | 完成 H3 后留下一个已知实例，停两个 demo worker，向剩余 API 发一个带唯一 ID 的新创建，再恢复至少一票。 | 失去多数派期间新请求不能获得成功回执。对超时或未知结果，在恢复 quorum 后查原 ID 和 Edge journal 判定；保留故障前实例及其原 owner，恢复清理后确认无重复实例和泄漏配额。 |

**确定性的失效窗口。** 生成唯一 request ID，在目标 API Pod 的 `/gate/gate.json` 写入 `{"request_id":"…"}`。用只含该入口的 SDK 客户端异步提交创建。当 `/gate/boundary.json` 出现时，从该 Pod 读取并核对 `stage=result_before_reply`、request ID、PID 和 Pod UID，立即把证据保存到宿主。此时 Edge 已给出结果。只对经本次 kind 集群标签、节点角色和 Pod 归属核对的 worker 执行 `docker stop -t 0`。保留另外一个未设 gate 的 API 作为明确的查询入口。检查点自身 45 秒超时；未在窗口内注入、无法确认归属、或 leader 在注入前已变化，均记录该轮未命中，恢复后再试。

SDK 的未知结果通过 `lookup_request(original_id)` 查询，再用查回的沙盒 ID attach；如测试重试，只能使用原 ID、同样的参数和项目。对照 Edge journal、IAM receipt、guest 标记以及物理租约，保留创建和停止前后的计数。另做一次有副作用命令的丢回复复测时，在已有 VM 内用单个命令 request ID 追加唯一标记，恢复后标记必须恰好出现一次。此检查证明命令去重，与创建去重分别报告。

**计时与终点。** 用同一宿主单调时钟记录故障前最后成功请求、注入、观察到新 leader、首次成功查询原请求、首次成功创建新请求、成员重新健康。按每秒一次记录采样误差。每轮最多等 120 秒，超时记为失败并保存最后的 SDK 错误、etcd 状态及 Pod 事件；120 秒是演示等待上限。副本查询完成和新 VM 创建分别计时。每轮 H1/H2 后要先恢复 H3。单轮数据不计算分位数。

**清理与产物。** 驱动在 finally 中先恢复本轮停止的 worker，再用原 owner 完成沙盒停止与回收；核对 IAM、Edge 物理租约和宿主服务基线，最后删除本轮的 kind 集群。结果目录至少保留 topology、版本与镜像摘要、每轮 timeline、请求和资源计数、cleanup、整体 verdict。发生 UNKNOWN 时保留原 ID 核查，仍有未解租约时保留控制面与私有诊断信息，报告失败。发布示例报告时排除 token、私钥与带授权头的日志。

## Linux 实机结果（2026-11-24 至 25 日）

四个 kind 节点共用一台 Linux 宿主，业务使用一个独立 Edge、正式 SDK 和 Firecracker MicroVM。测试停掉目标 worker 容器，使该域的 API 和 etcd 一起失联。按宿主单调时钟，从发出停止命令起算，每种角色各有一轮完整计时。

| 停止的域 | 首次观察到剩余两票达成一致 | 查回原创建 | 新创建返回 | 原 worker 启动命令返回后，三副本均能查询原记录 |
| --- | ---: | ---: | ---: | ---: |
| follower b | 0.65 s | 0.79 s | 3.72 s | 20.99 s |
| leader a | 0.54 s（新 leader c） | 1.67 s | 4.59 s | 23.23 s |

H0 的三个入口均能发现同一 Node。H1 和 H2 均在回复前检查点停机，原 SDK 收到未知结果。用原 ID 查回、接续并重试时，Edge 创建日志只增一条，物理租约与 IAM 并发占用均为一。guest 写入并读回标记，停止后两者归零。故障域离线期间仍可创建第二个实例。首次新创建都收到一次 `NoPlacement`，确认没有准入后，同 ID 延迟一秒重试成功。三成员恢复后身份和 cluster ID 均匹配。独立的命令测试在回复丢失后重放相同追加命令，文件里只有一行。

H4 同时停止 b 和 c。新请求返回 `CoordinationUnavailable`。恢复两票后核对，同一请求没有 IAM 准入或 Edge 创建记录，原实例可执行命令并通过 SDK 释放。额外要求三个成员在 120 秒内全部恢复的检查失败。worker c 上 kube-proxy 启动报 `too many open files`，该域通过集群 DNS 查询 etcd peer 超时。宿主的 inotify 实例上限为 128，该轮缺少区分 FD 与 inotify 实例限额的系统调用证据。保留原有宿主参数。独立物理宿主故障仍由 TASK-004 验收；当时 H4 的三成员恢复未通过。后续诊断与复测见下文。

首次部署还发现初始化容器只读根目录缺少临时目录。清单现为初始化和 API 配置有界内存卷 `/tmp`，并保留只读根目录。API 镜像构建也已在缺少 BuildKit 的 Docker 上验证。首次 H0 的验收断言把 SDK 的签名路由与 Edge 的本地 ID 直接比较，修正为解码、核对 owner 和 admission 后重跑。结束时删除了本轮的四个 kind 节点、节点卷和专用网络，原五个宿主容器的 ID、启动时间和重启次数均与基线相同。

各次尝试、镜像摘要、单调时间线以及回收证据见[实机记录](acceptance-20261124.json)。

参考 [kind 配置](https://kind.sigs.k8s.io/docs/user/configuration/)、[Kubernetes 节点选择](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) 和 [etcd 故障模型](https://etcd.io/docs/v3.5/op-guide/failures/)。

## H4 宿主 inotify 诊断与复测

2026-11-25 在相同 kind 版本与四节点布局重现双域重启故障。实验机设置为 `fs.inotify.max_user_instances=128`、`fs.inotify.max_user_watches=65536`。本轮在 worker b 捕获 kube-proxy 的启动系统调用。下列前两项由 kube-proxy 的启动包装器测量，第三项来自该 kube-proxy 自身的 strace。

| 观察 | 结果 |
| --- | --- |
| UID、启动时已打开 FD、进程 FD 软/硬上限 | UID 0，3 个 FD，上限均为 1,048,576 |
| 同一启动环境打开普通文件与创建 inotify 实例 | `open("/dev/null") = 3`；关闭后，`inotify_init1()` 返回 errno 24 |
| kube-proxy 创建监听器 | `inotify_init1(IN_NONBLOCK\|IN_CLOEXEC) = -1 EMFILE`，随后记录 `failed complete: too many open files` |

在上限 128 的对照中，两个 worker 重启后 etcd2 超过 120 秒仍不可达；从已恢复的多数派释放了原实例，物理租约和项目并发均归零。失败发生在创建 inotify 实例时，早于添加 watch。kind 的四个节点共用宿主，节点内的 UID 0 映射到宿主 UID 0。实验机在多个节点同时启动时耗尽了该用户的实例配额。Linux 对进程 FD 上限与用户 inotify 实例上限都能返回 `EMFILE`，诊断时应同时记录失败的系统调用、普通文件打开结果和进程 FD 上限。[Linux inotify_init(2)](https://man7.org/linux/man-pages/man2/inotify_init.2.html)

针对此故障，可在 Linux 宿主读取并临时调整实例上限，再用原版 kube-proxy 重跑 H4。512 是本机的复测设置。管理员可按所在主机的基线和使用规模选值。以下命令仅在旧值低于 512 时写入，设置在本次启动期间有效。[kind 已知问题](https://kind.sigs.k8s.io/docs/user/known-issues/#pod-errors-due-to-too-many-open-files)

```sh
sysctl fs.inotify.max_user_instances fs.inotify.max_user_watches
if [ "$(sysctl -n fs.inotify.max_user_instances)" -lt 512 ]; then
    sudo sysctl -w fs.inotify.max_user_instances=512
fi
```

在宿主上把实例上限由 128 调到 512 后，每轮重建专用集群，核对原版 kube-proxy 的镜像与启动命令，再用同一个 H4 协议停掉 b、c 两个 worker。恢复计时从发出第一个 worker 重启命令开始。

| 故障前 leader | 返回原三个 member ID 和 cluster ID | 三端点健康、三个 API 查回同一请求并读回 guest 标记 |
| --- | ---: | ---: |
| a | 19.16 s | 21.10 s |
| c | 18.93 s | 22.06 s |

两轮的失多数派写入都返回 `CoordinationUnavailable`。恢复后，拒绝的请求无准入记录或 Edge 创建日志，原 VM 的进程 ID 与故障前相同。停止原 VM 后，物理租约、IAM 准入和并发用量均为零。每轮都回收了专用集群和四个节点卷，并核对了原有五个宿主容器的身份、启动时间和重启计数。每种 leader 位置各测一轮。原始对照、恢复时间线和清理回执见[H4 复测记录](h4-20261125.json)。
