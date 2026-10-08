# DSec 模块化重构设计

状态：设计已确认，R0/R1 已实施；R2 已完成评分插件、SDK/容器 Edge 拆分、Edge 节点租约迁移、生命周期状态转换拆分、创建/ready 池/registry 组合、执行通道拆分及 Storage 接口收敛，剩余任务规则迁移与 R3 待完成。日期：2026-10-08。
代码基线：`7f70524187e61e13f123c7999098858cf3411bd5`。

## 1. 目标与范围

目标是通用的 DSec 弹性沙箱系统。底层按论文的机制和职责组织，顶层提供稳定的
沙箱 SDK、可恢复的 rollout 服务和可选应用接入。TB2.1、MBPP 是使用者，不决定核心模型。

系统资源管理范围为沙箱：guest RAM、VMM、环境层与可写盘缓存、共享存储服务，
以及创建、暂停、快照、恢复和回收所需资源。模型权重、KV cache、优化器、训练/推理
GPU 显存和模型 offload 归外部框架管理。节点准入可观察外部负载造成的资源压力，
但不卸载模型或调整推理服务配置；worker 的策略请求接口只调用外部模型服务。

本轮先整理模块、依赖与状态所有权，保留已验证行为；明确缺失能力的实现位置和验收条件。
不把重构与增加全部生产能力合并，不改写 Rust 存储组件，不迁入 RL 算法或 GPU 内存补丁。
目录调整通过不代表论文机制已经复现。

论文给出的运行路径包含 libdsec、控制面以及 edge → aether → chronus；存储、内存、CPU
与暂停恢复有各自机制。以[论文 §3、§5、§6、§7](https://arxiv.org/html/2609.22978)为设计依据，
公开描述不足以确认的线协议不宣称兼容。本项目保持 libdsec 风格调用语义，独立定义版本化协议。

## 2. 逻辑架构

```mermaid
flowchart TB
    App[应用：TB2.1 / MBPP / 自定义任务] --> Worker[Rollout Worker：任务状态与恢复]
    Trainer[Miles / verl / 其他训练框架] --> Worker
    Trainer --> Policy[模型服务：采样与 token/logprob]
    Worker -. 后续独立 agent loop 的模型调用 .-> Policy
    Worker --> SDK[libdsec 风格 SDK]
    User[普通沙箱使用者] --> SDK
    SDK --> API[API 入口：鉴权与路由]
    API --> Place[Placement / Watcher]
    API --> Edge[Edge：节点准入与沙箱生命周期]
    Edge --> Driver[后端 Driver：Firecracker / Container]
    Edge --> Storage[Storage：环境层 / 可写盘 / 快照]
    Edge --> Isolation[网络与权限策略]
    Edge --> Aether[Aether：连接与会话管理]
    Aether --> Chronus[Chronus：命令 / 文件 / 流式 I/O]
    Storage --> Sources[3FS / 本地来源与缓存]
```

图是目标逻辑架构。虚线表示尚未完整实现的训练生命周期解耦，不是已上线功能。
FnCall 将来走单独执行路径；当前不提供。完整 VM、分布式 IAM/placement/watcher 也不冒充已实现。

**模块不等于服务进程。** 首个部署仍是单机：API 路由与 Edge 可以同进程，placement
只有一个候选节点，worker 独立运行。保持请求入口和职责边界，以后再拆服务。
普通沙箱操作无需经过 rollout worker；需要 episode 管理的应用才使用它。

## 3. 顶层模块

采用一个 `dsec` Python 包，先保留现有发行包名称和 CLI。八个核心模块如下。

| 模块 | 职责 | 禁止承担的职责 |
| --- | --- | --- |
| `contracts` | Spec、ID、能力、错误、结果、协议版本和恢复状态 | 启动进程、读宿主资源、导入任务插件 |
| `sdk` | 薄客户端、沙箱句柄、请求关联、attach/query、libdsec 风格参数 | 启动 Docker/VMM、挂载磁盘、管理 sudo |
| `control` | 请求入口、授权边界、节点路由、placement 与 watcher 接口 | 保存第二份沙箱执行状态、执行 guest 命令 |
| `runtime` | Edge、生命周期、节点准入、后端驱动、会话、隔离、ready 池 | TB2 评分、模型解析、训练算法 |
| `storage` | 环境清单、分层、来源、缓存、块设备、快照与发布 | 驱动 rollout、分配 CPU、操作训练显存 |
| `rollout` | episode、动作与对话日志、任务插件调用、评分证据、恢复 | GRPO/GAE、模型特有解析、直接调用 VMM |
| `observability` | 事件与指标定义、采样、统计口径、Prometheus 导出 | 决定生命周期、把采样异常解释为任务失败 |
| `host` | 部署配置、依赖检查、安装计划、权限 helper、服务组合 | 在普通 SDK 调用中修改宿主全局配置 |

建议目录：

```text
src/dsec/
  contracts/       sandbox.py  environment.py  execution.py  protocol.py
  sdk/             client.py  sandbox.py  rollout.py  transport.py
  control/         api.py  routing.py  placement.py  watcher.py  authorization.py
  runtime/
    edge.py  registry.py  lifecycle.py  admission.py  resources.py  pool.py
    backends/      firecracker.py  container.py
    sessions/      channel.py  dispatcher.py
    isolation/     network.py  policy.py
  storage/
    catalog.py  layers.py  sources.py  cache.py  devices.py
    snapshots.py  publication.py  integrity.py
  rollout/         worker.py  episodes.py  actions.py  dialogue.py  evaluation.py
  observability/   events.py  meters.py  prometheus.py
  host/            configuration.py  doctor.py  install.py  privileged_helper.py
native/            guest_agent/（现有 C 实现，随后演进 aether/chronus）
integrations/      miles/  verl/（可选依赖与薄适配器）
apps/              tb21/  mbpp/（任务、数据准备、评分器、示例配置）
tests/             unit/  contracts/  integration/  acceptance/  packaging/
```

这是职责分组，不要求一次创建所有文件。暂未实现的 IAM 等能力在设计中保留边界，
不增加空服务、假的成功响应或无用抽象。每个实现只保留一个正式位置。

## 4. 底层机制的约束

以下是本项目的实现约束；当前状态依据代码和[路线图](../../ROADMAP.md)，不是目标能力清单的完成声明。

| 领域 | 目标实现与约束 | 当前需要处理的差距 |
| --- | --- | --- |
| 环境组合 | 显式区分 base、workspace、toolkit；不可变只读层共享，实例写入私有层；离线合并保留删除语义 | 不再让 `tb2-*` ID 决定核心存储行为；合并策略进入通用发布流程 |
| 按需存储 | 可选 local/3FS 来源；元数据与数据的布局能力显式声明；读取粒度、缓存与实际远端读取可追踪 | 不把本地全量预置描述为远端按需验证；3FS 本机部署不等于分布式验收 |
| microVM 磁盘 | 只读 EROFS 层与 OverlayBD＋ublk 可写 ext4 盘分别建模；需要 Docker data-root 时提供独立可写盘 | 现有存储组件继续复用；不把 FUSE 单盘组合设为唯一正式路径 |
| 内存 | DAX 用于适合共享的只读层；DAMON/FPR 是另一种回收策略；按设备和内核能力验证组合 | 当前 DAX 本地 backing 限制明确暴露；不能把两个 VM 的验证扩展为所有配置 |
| CPU | LS/BE 调度策略属于 runtime resource policy，记录逻辑 CPU/SMT 和实际应用状态 | 现有容器验证不能代表 VMM QoS 已完成 |
| 暂停恢复 | microVM 的暂停检查点与终止 VMM 分开记录；恢复继续原身份。容器冻结与内存卸载分别声明能力 | 不把 Docker pause 当作内存卸载已完成 |
| 环境发布 | `pack_diff` 发布磁盘增量；经过构建身份隔离、残留清理、完整性检查后成为新环境 | 现有磁盘 COW 是基础；prepared-state fork 不替代完整发布流程 |
| 会话执行 | Edge 管资源；aether 管连接与 session；chronus 管 session 内操作 | 当前 `guest_agent.c` 是单连接命令原型，尚非完整多会话、流式实现 |
| 安全 | Edge 通过受限 helper 应用实例策略；文件/socket 保护与出网策略分别建模 | netns 不等于完整 eBPF/AppArmor；宿主容器路径只保留可信开发/对照定位 |

特别区分两类 FUSE：3FS 客户端使用 FUSE，与自建的 EROFS 多层呈现/转发实现不是同一组件。
不能仅因使用或移除 FUSE，就判断是否与论文对齐。

ready 池、内存基线分叉和本地代理属于工程扩展，保留为显式选项。普通创建、池命中与
基线分叉分别统计；不把池中就绪实例的领取时间作为冷启动时间。

## 5. 状态所有权与依赖规则

| 状态 | 唯一负责模块 | 其他模块的权限 |
| --- | --- | --- |
| 沙箱生命周期、TTL、VMM 身份、网络/磁盘句柄、节点资源租约 | Runtime Edge | 查询或提交命令，不直接修改 registry |
| 不可变环境与快照对象、内容摘要、依赖、存储引用 | Storage | Edge 持有租约；停止后确认释放；回收由 Storage 根据引用和租约决定 |
| rollout、step/action ID、对话、任务预算、评分及证据 | Rollout Worker | SDK/训练框架通过协议读取与提交 |
| token/logprob、模型权重、优化器、原生训练样本 | 训练框架；将来的持久策略记录由明确的轨迹存储接口承接 | Worker 可保留原始响应及引用，不用文本伪造 token/logprob |
| 节点健康、负载和 placement 视图 | Control 的派生视图 | 从 Edge 重建，不作为沙箱是否存在的事实源 |
| 项目权限及配额（后续） | Control authorization | Edge 仍须做本地准入；权限与余量不能互相替代 |
| 监控样本与聚合指标 | Observability | 可失效或重建，不作为动作是否执行的证据 |

节点 CPU/内存/磁盘/网络的物理资源账本最终由 Edge 管；worker 保留作业并发/API 限额。
现有 `WorkScheduler` 的节点预留迁入该账本，过渡期间只保留一个预留路径，避免两个服务
分别扣除同一份内存。API 额度的部署共享范围必须明确，不能每个 worker 各算一份全局额度。

依赖规则：

- `contracts` 不依赖其他项目模块；`sdk` 只依赖 contracts 和客户端传输。
- control、runtime、storage、rollout 通过 contracts 中的接口或消息交互，不导入彼此的具体驱动。
- 后端不管理 episode，storage 不导入 runtime；Edge 用组合方式连接两者。
- 任务与训练集成依赖核心；核心不依赖 `apps`、Miles、verl、OpenEnv 或 benchmark 特例。
- host 作为服务组合入口注入实现，不被 SDK 或 contracts 导入；监控接收事实，策略另有明确接口。

## 6. 关键接口与执行流程

### 6.1 接口

`SandboxSpec` 表达 backend、environment digest、资源限制、TTL、网络策略、初始身份和
storage/memory/QoS policy。`EnvironmentManifest` 表达层角色、顺序、格式、依赖摘要和
验证要求。`FrameworkProfile` 暂作兼容配置入口，逐步转换为这些通用对象；不再用实验名
`e3_mixed` 或任务名前缀表达内核能力。

`Capabilities` 至少区分 command、persistent session、streaming、file I/O、pause、
memory offload、disk snapshot、memory restore、prepared fork、pack_diff、DAX 和 QoS。
能力结果包含不支持的原因，并校验组合；不能只列一组与当前环境无关的布尔值。

后端 Driver 提供 launch、inspect、freeze、checkpoint、restore、terminate 等运行原语；
命令执行属于 SessionChannel。Storage 提供 prepare、create_writable、checkpoint_disk、
publish_diff、release 等接口。具体系统调用留在各实现内，不传入 TB2 任务路径。

### 6.2 创建和执行

1. SDK 提交稳定 request ID 和 SandboxSpec，由 API 路由至 Edge。
2. Edge 校验能力与当前资源，持久记录创建意图，取得资源/存储/网络句柄，启动后端。
3. 会话通道就绪并核对身份后才返回 READY；失败按已取得资源逆序补偿并记录未决项。
4. exec 带 sandbox/session/action ID 经入口路由。接收、完成和结果落盘有独立状态。
5. 调用方断线只说明结果未知；查询或对账后处理，不自动重放有副作用的命令。

UNKNOWN 是动作结果状态，不是健康检查立即销毁实例的理由。健康检查使用独立控制连接
或保留容量，避免长命令占满连接池后误判。命令、RPC、episode、verifier 的超时分别配置。
输出采集上限、模型反馈截断和原始证据保留也分别配置。

### 6.3 暂停、分叉与发布

- 同一 episode 暂停/恢复保留身份、对话和已完成动作；状态可恢复后再确认完成。
- 准备态分叉产生新 sandbox/rollout ID、私有写入和独立历史；共享只读基线及引用。
- `pack_diff` 输出新环境的磁盘状态，不带旧会话、任务答案、worker 日志或模型上下文。
- stop 可重复调用；资源实际释放前保留租约和清理进度，不先宣告成功或永久漏占资源。

跨进程恢复必须依赖版本化 journal 和实际进程/设备身份核对；不能根据一个 PID 或旧
目录名就操作宿主进程。一般副作用不承诺任意故障下 exactly-once，保持未知状态和证据。

### 6.4 任务与 RL

TaskAdapter 负责 instruction、准备要求、动作解释和 verifier；Worker 负责持久推进及评分
记录；TrainerAdapter 负责模型模板、采样、原生 token/logprob 与训练样本转换。GRPO 分组身份
由训练框架保留，未知或失败 slot 不通过压缩数组替换成其他任务的样本。

现有 reset/step/evaluate/stop 入口和[反馈协议](AGENT_ENVIRONMENT_CONTRACT.md)继续有效。
MBPP 的评分回执与 TB2 的 worker 评分路径逐步接到同一证据接口，但保留原奖励语义。
任务未通过、预算耗尽、评分器故障、动作 UNKNOWN 分开记录；infra error 不伪装成模型零分。

短期仍允许训练框架驱动 agent loop。完整论文式解耦作为独立里程碑：worker 与 agent sandbox
承接运行状态，通过可重连的策略接口请求推理，训练进程退出后可继续或安全暂停。
仅持久保存 shell 日志不能宣称已完成该能力；缺失旧 TITO 的 episode 不进入训练更新。

## 7. 现有代码迁移位置

| 当前文件或组件 | 目标位置与必要拆分 |
| --- | --- |
| `sandbox_client.py`、`libdsec_compat.py` | sdk；容器启动和 journal 管理从客户端移至 Edge |
| `scheduled_dsec.py`、`rollout_client.py` | sdk 的 rollout 客户端；不导入服务端 scheduler 实现 |
| `sandbox_sdk.py`、`durable_manager.py` | runtime edge/lifecycle/registry/pool；组合替代管理器继承耦合 |
| `sandboxd.py` | control 入口及 host 服务组合；不再绑定 TB2 verifier |
| `microvm.py`、`container_backend.py`、容器 supervisor/broker | runtime backends；共享生命周期约束、保留后端差异 |
| `network_namespace.py`、权限 helper、`egress_proxy.py` | runtime isolation 与 host 管理端；策略与安装分开 |
| catalog、OverlayBD/ublk client、共享层、artifact publisher/integrity | storage；统一对象引用与发布流程 |
| `work_scheduler.py`、`admission_guard.py`、`work_journal.py` | runtime admission/resource 账本与 rollout 作业/API 限额拆分 |
| `rollout_workerd.py`、`rollout_store.py`、`agent_environment.py` | rollout 与 contracts；shell 格式化单独模块；任务分支移出 worker |
| resource meter/monitor、shared service monitor | observability；保留实例/共享服务/节点的不同统计边界 |
| `tb2_*` 与 adapters 内 TB2 清单和 verifier | apps/tb21；通过插件注册，不由核心按 ID 猜测任务类型 |
| Miles adapters、OpenEnv 兼容入口 | 可选 integrations；通用任务契约留核心，框架细节移出 |
| `dsec_host.py`、service admin、安装模板 | host；CLI 薄入口调用正式模块，不复制业务逻辑 |

旧顶层 import 在过渡期保留薄转发与弃用说明，禁止新旧两套实现并存。对安装入口、配置格式、
journal schema、环境摘要、已有实例路径分别做兼容检查；不能靠重新创建所有实例掩盖恢复回归。
权限 helper 的升级单独出安装计划，不借重构修改宿主 Docker、Grafana 或全局网络。

## 8. 分阶段实施与验收

| 阶段 | 交付 | 通过条件 |
| --- | --- | --- |
| R0：契约冻结 | 本设计、现有 API/CLI/schema 清单、模块依赖基线、恢复和安全不变量 | 审阅确认；可区分机械迁移与行为变化 |
| R1：模块搬迁 | 先 contracts/storage，再 runtime/host，随后 sdk/control/rollout；移动已有实现，保留兼容 shim | 当前 CI、发行归档、旧 import 和入口通过；核心不导入 benchmark/训练依赖 |
| R2：职责收敛 | 薄 SDK、统一 Edge 生命周期/节点账本、任务插件、明确 session/storage 接口 | 容器路径不再由 SDK 管宿主；无双重资源预留；UNKNOWN 和重启恢复契约通过 |
| R3：真实回归 | Linux 沙箱生命周期、存储与并发回归；TB2.1/MBPP 接入检查；短 GRPO 接口验收 | 独立写入、恢复、不重复动作、评分证据与清理通过；与基线比较创建/执行开销 |

R1 不启动新功能开发。R2 涉及行为的改变拆成独立小提交，分别验收后再推进；安装或 journal
迁移有 dry-run、备份和回滚步骤。不中途替换存储格式，不清理仍被引用的基线或历史证据。

R3 不重复整轮 187 update 的模型效果实验，也不重跑全部 TB2.1 成功率来证明目录重构。
用固定命令工作负载检查执行与资源行为，再用已有固定应用样本完成一次创建、评分和短训练
闭环。若更改奖励、样本或模型上下文语义，则另立实验，不以旧训练成绩为验证。

重构结束标准：模块边界、单一状态所有权、客户端/插件独立安装、兼容入口、故障回归及
真实短验收通过。之后按路线图补齐 aether/chronus、完整 pack_diff、独立 agent loop 和生产隔离，
不在本轮夹带完整 VM、FnCall、多节点平台或新的 GPU 调优。

验收报告分开记录“代码已实现”和“真实执行已验证”。原始证据不覆盖；设计决定和变更原因
维护在本文，优先级维护在 ROADMAP，避免每次改动新增临时 Markdown 和执行脚本。

## 9. 本提案的默认选择

1. 保持 Python 主体、现有 C guest agent 与 Rust OverlayBD/ublk；不进行语言重写。
2. 保持单机可安装，同时采用论文的逻辑组件边界；生产缺口明确报出。
3. 核心不含任务集、模型和训练算法；OpenEnv 为可选兼容入口。
4. 已验证存储和恢复路径优先复用；DAX、FPR、QoS、ready 池通过可验证的能力/策略组合启用。
5. 状态所有权与故障语义优先于文件数量和命名；每一轮迁移都有可停止的验收边界。

## 10. 实施节点

契约验收确认：用户于 2026-10-08 确认契约通过验收，并明确系统只负责沙箱。
据此冻结模块职责、状态所有权、资源管理范围以及 R0 兼容基线，进入 R2 职责收敛。
该确认是契约关卡的通过记录；R3 Linux/KVM 和真实训练运行关卡保持待验收状态。

### R0/R1：契约冻结与通用模块迁移

资源字段及默认值、请求摘要、变更操作集合和 CLI 入口固定在
[兼容基线](../../tests/contracts/v01_compatibility.json)。
[契约回归](../../tests/contracts/test_module_boundaries.py)检查旧/新导入身份、旧 journal 恢复、
依赖方向，以及 guest/root helper 的独立源码边界。

37 个通用顶层实现迁入 `src/dsec`，旧路径作为模块别名转发。存储、客户端、runtime、
worker、host 和采样实现只有一份；请求/资源契约与文件持久化原语已提取。
除请求/资源定义拆分外，迁移代码移除 import 后的 AST 与基线一致；权限 helper 源码
与基线逐字节相同。未连接实验机、升级 helper 或改变运行服务。

发行工具支持 `src/dsec` 的 package-dir 映射，源码归档包含新包和契约 fixture。
独立虚拟环境从 wheel 安装、脱离源码目录检查了 12 个兼容别名与 5 个 CLI；这不是第二台
Linux 主机或真实 microVM 验收。Mac 上 Linux 专用检查仍须明确跳过。

本节点运行 210 项检查：203 通过、7 跳过，无失败或错误。包含既有核心回归、两个应用
回归、契约检查与发行归档检查；跳过项涉及 Linux `/proc`/设备要求或缺失的历史实验
工作区。当前记录不包含 Linux CI 结果或运行中服务升级验收。

R1 节点结束时的 R2 剩余工作：旧 façade 的容器管理移入 Edge、worker/API 内 TB2 特例插件化、节点账本
与作业/API 限额分离、生命周期管理器拆分，以及 session/storage 接口收敛。
`compat.libdsec` 和现有 profile 验证规则是迁移期间的兼容边界，不是最终薄 SDK 或通用
能力解析器。`service_admin.py` 暂保留原路径，保护其旧脚本身份检查；任务集成仍按现有
包安装。R3 的真实 Linux/KVM 和短训练验证尚未执行。

### R2：worker 评分插件节点

评分接口提取至 `contracts.evaluation`，TB2.1 官方 verifier 规则移至可选
`apps/tb21`；旧请求和配置通过 `compat` 转接。通用 worker 的导入与构造不加载
TB2.1 应用，配置缺失在分配状态目录前报错。通用 SDK 可以选择部署方已注册的评分器。
worker 持有评分身份、参数、结果提交与 UNKNOWN 状态；插件只负责任务规则和执行评分。

评分取消或结果无效不会成为零分；新完成记录核对评分器身份和参数后返回缓存结果，
旧 TB2 完成记录仍能读取且不重跑 verifier。命令转换后的实际提交值随动作保存，
防止重启后按新配置重新解释旧动作。[评分契约](AGENT_ENVIRONMENT_CONTRACT.md#worker-评分插件)
描述边界，[故障回归](../../tests/unit/test_worker_evaluators.py)覆盖中断、错误证据、
结果与参数变更，以及禁止 RPC 动态加载代码。

本地共运行 221 项回归，214 通过、7 项因 Linux 条件跳过，无失败或错误。
独立 wheel 环境验证了核心单独安装、可选应用缺失时的预检查、安装应用后的注册和
任务/参数约束。源码归档、文档链接及核心 wheel 边界检查通过。

本节点尚未迁移 TB2 manifest/API 规则和 adapter；PATH 修复仍由兼容桥保留。
本评分插件节点结束时，薄 SDK/Edge 容器生命周期、节点资源账本和 session/storage 收敛尚未完成。
本地插件与发行回归不替代 R3 的真实 Linux/KVM、运行服务升级和短训练验收。

### R2：薄 SDK 与容器 Edge 节点

libdsec 风格参数提取至 `contracts.sandbox`；正式客户端成为 `sdk.client`。
旧 façade 路径只转发同一模块。客户端不再持有 Docker 后端、制品目录或容器 journal，
运行时通过同一 Unix socket 接收容器创建、状态、执行、查询和停止请求。
运行参数的校验在客户端及 Edge 执行，制品/目录的解析只在 Edge 执行。

容器 Edge 接管既有后端与生命周期 journal；请求记录仍用原 create/stop 摘要和 schema，
没有套入第二份 microVM journal。实例目录独占锁阻止两个 Edge 同时管理同一根目录；
执行/停止的互斥在副作用前生效，读取状态与请求不占用前台工作计数。
容器停止后释放服务内的句柄缓存。关闭客户端或服务的 owner handle 不停止容器。

结果提交失败和后端执行失败统一报告 UNKNOWN；原记录只能凭状态证明完成，不能重做副作用。
SDK 对稳定 ID 请求取消隐式换号重试，以免 worker journal 与实际请求脱节。
新客户端要求服务公布 `container-rpc-v1`，不向旧服务静默退回宿主 Docker。
配置归属与升级边界见[安装配置](../guides/DSEC_HOST_CONFIGURATION.md#container-runtime-ownership)，
[本地 RPC 故障回归](../../tests/unit/test_container_edge.py)不启动真实 Docker。

本节点完整本地回归 236 项：229 通过、7 项因 Linux 条件跳过。
独立核心 wheel 脱离源码目录通过 14 项 RPC/故障检查、6 个 CLI help、旧导入身份及
可选应用缺失预检查；安装 TB2.1 应用后评分器注册通过。容器资源采样使用服务返回的
身份，不再依赖客户端内部后端对象。源码归档、文档链接和 wheel 边界检查通过。

尚需完成节点账本与作业/API 限额分离、生命周期管理器拆分、session/storage 接口收敛，
以及剩余 TB2 manifest/API 和 adapter 规则迁移。容器 inventory/TTL、生产 VM 隔离仍是
缺失能力；本节点不声称已提供这些机制。R3 真实 Linux/KVM、Docker 升级与短训练待验收。


### R2：节点资源、作业配额与 API 限额分离节点

`contracts.resources` 保留原 `ResourceDemand` / `ResourceBudget` 的字段和默认值，
另提供节点需求、节点预算和 API 限额的显式投影。
`runtime.resources.NodeResourceLedger` 只接收物理资源需求，持有按租约 ID 组织的
预留；汇总由租约派生，返回副本。节点需求与节点预算均不含 API 字段。
Linux `/proc` 采样归节点模块；采样语义不变。

`rollout.scheduler.WorkScheduler` 负责队列、依赖、作业统计与组合准入，
`rollout.quotas` 分别管理 episode 槽及外部 API 的 inflight/RPM/TPM。
原 `work_scheduler` 和 `runtime.scheduler` 转发同一实现，旧资源摘要、worker journal、
报表字段与指标名称保留。新 `resource_scopes` 给出分项视图，兼容 `reserved` 只汇总，
不再执行第二次预留。节点和 episode 取得资源之间不 await；后者失败回滚本次节点租约。
UNKNOWN 恢复即使超过当前预算也保留预留；评分完成和 API 结束均不释放节点资源。

API 并发与速率额度改为调用前同时取得，解决旧实现先扣 RPM/TPM 后等待并发槽的问题。
取消排队请求不占额度；已发起但取消或响应不确定的请求保留速率预留。
一个进程内可显式共享节点账本或 API 限流器；预算不匹配直接拒绝。
这不是跨进程的节点准入或全局 API 额度实现，重启后 API 窗口也不持久化。
预算在构造时配置；直接替换旧调度器 budget 属性会报错，防止只改报表而账本仍按旧限额执行。
原 prepared-fork 测试改为构造时传入两份沙盒的预算，测试内容仍覆盖真实 worker 的基线分叉流程。

本节点完整本地回归 245 项：238 通过、7 项因 Linux 条件跳过。
新核心 wheel 在独立 venv、源码目录之外通过 24 项调度/worker 恢复检查；
未安装任务应用，两个服务入口 help 与三条调度器导入路径的模块身份检查通过。
wheel 包含 119 个运行时 Python 模块，源码归档与文档链接检查通过。
真实内核资源限制、KVM、存储及短 RL 不属于这些本地检查。

本节点完成模块职责分离，**尚未完成 Edge 账本所有权迁移**：
worker 的既有持久 rollout 记录仍负责恢复预留，服务端准入检查仍确认 worker 授权。
后续迁移必须让 Edge 成为唯一持久节点租约入口，并删除旧节点预留路径，不能同时启用两份账本。
生命周期管理器拆分、session/storage 接口及剩余任务规则迁移仍待完成；R3 真实宿主验收未执行。


### R2：Edge 持久节点租约节点

节点租约已迁入 `runtime.node_admission`，正式 `dsec-host` 部署不再由 worker
保管物理预留。Edge 在宿主资源副作用前持久化租约，绑定物理沙箱身份；直接 SDK、
多个连接同一 Edge 的 worker、基线分叉与 ready 池使用同一账本。
worker 只持有作业并发和 API 限额，重启不再次恢复物理预留。

ready VM checkout 在原租约上补足活跃需求，创建、待机与活跃预留分别表达。
guest 虚拟内存上限、物理准入预留与实测 PSS 不互相替代。待机默认降低 CPU/网络
预留，内存/磁盘仍保守保留；默认估计需实验机校准，不能据此宣称池密度或性能收益。
节点预算作用域是一个 Edge 实例，不是整机跨实例全局配额；模型仍归外部框架管理。

TTL 与直接 SDK stop 在实际清理完成后释放节点租约，不依赖 worker 回执。
STOPPED 但清理未完成的实例保留预留，重试继续清理；重启后 UNKNOWN、超预算及缺失
registry 的已绑定租约也保守保留。Docker 查询失败不等于容器消失。
资源不足且确定没有沙箱副作用时返回 `NodeAdmissionBusy`，worker 记录等待原因和时间，
使用原 ID 重试；创建或租约提交不确定时维持 UNKNOWN，不重放副作用。

既有 request/journal 摘要、字段与变更操作集合保留，物理资源 hint 使用额外 envelope；
旧准入 socket 与新 Edge 预算禁止同时启用。升级保留原实例目录及 journal。
[主机配置指南](../guides/DSEC_HOST_CONFIGURATION.md)说明作用域、参数与迁移顺序。

本节点完整本地回归 260 项：253 通过、7 项因环境条件跳过，无失败或错误。
核心 wheel 在源码目录外的独立 venv 通过 53 项节点/容器/调度恢复检查，
未安装 TB2.1/MBPP 应用包；3 个安装后的 CLI 帮助及 wheel/文档边界检查通过。

本节点完成资源所有权迁移，尚需生命周期管理器拆分、session/storage 接口收敛与剩余
任务规则迁出；R3 的真实 Linux/KVM、Docker 升级及应用短训练仍待验收。
本地 RPC 故障检查中的 VMM/Docker 副作用有替身，不能替代真实内核及性能验证。


### R2：生命周期状态转换组件节点

`runtime.transitions.LifecycleController` 已接管既有 OverlayBD 设备核对/释放、
故障处理、TTL 检查、快照暂停、恢复与停止回收，共 9 个方法。
`Sandbox` 仍是唯一运行状态及持久化对象，controller 只保存文件操作依赖，
通过组合使用原有 Driver、Storage、锁和租约；不创建第二份 registry 或执行 journal。
`DurableManager` 重启构造出的 Sandbox 沿用同一入口，不要求给旧记录补新组件字段。

旧 Sandbox 方法保留薄委托，异常身份移入 `contracts.errors` 并由旧路径重导出。
文件操作在 Edge 组合处注入，兼容既有模块级故障注入；controller 不反向导入
lifecycle façade、registry、SDK、worker 或任务模块。9 个方法相对于 `ce09444`
经参数名称与注入调用归一化后 AST 一致：快照 schema、发布/清理顺序、UNKNOWN、
基线分叉读者等待、节点租约释放条件均沿用现有实现，没有新增状态机或存储格式。

本节点完整本地回归 261 项：254 通过、7 项因环境条件跳过，无失败或错误。
核心 wheel 在源码目录外的独立 venv 通过 41 项快照、分叉、节点租约与创建回收检查，
未安装 TB2.1/MBPP 应用包；3 个 CLI 帮助及 wheel/文档边界检查通过。

创建/ready 池调度及 registry 组装仍在兼容管理器中；执行通道和统一 Storage 接口
尚待收敛。本节点只是状态转换职责拆分，不声称生命周期拆分或 R2 已全部完成。
真实 VMM、内核和存储的升级验收仍归 R3；本地检查使用既有故障替身。


### R2：创建、ready 池与持久 registry 组合节点

`runtime.provisioning.SandboxProvisioner` 接管冷创建、准备基线的分叉入口与预热；
`runtime.pool.ReadyPool` 接管 FIFO checkout、前台等待和空闲补位。
组件只持有注入的操作依赖，实例表、状态、锁、条件变量及节点租约仍由同一个 Edge 管理。
既有管理器入口保留薄委托，不增加第二份池或资源账本。

`runtime.registry_store.RegistryRecords` 处理原 v1 记录、恢复核对和未提交快照清理；
Linux 身份核对、pidfd 及原子写入通过 `RegistryOperations` 注入。
`SandboxRegistry` 在创建和恢复 Edge 前取得实例目录独占锁。
正式服务使用 `runtime.edge.open_edge` 组合普通管理器与 registry；旧 `DurableManager`
构造器继续转入同一组合路径，旧导入身份和 `registry_lock` 属性保留。
实例目录、sandbox ID、记录字段和请求摘要未更换；UNKNOWN 不重放副作用。

15 项迁移检查相对于 `d51083c` 的 AST 经参数/注入操作归一化后相同，覆盖池辅助方法、
冷创建/预热、记录处理、创建前置校验与 checkout 分支。
另有三项显式安全改进，不属于机械迁移：退役所有权后旧对象不能再操作由新 Edge 接管的 VM；
服务组装失败只退役线程并关闭进程身份句柄，保留已知存活 VM 及恢复证据；
显式关闭 Edge 仅在沙箱回收成功后释放目录所有权，回收失败可重试。

`detach` 退役控制线程和所有权，保存记录并关闭本进程句柄，保留沙箱；
`close` 由当前所有者停止沙箱并清理资源。两者不混用。
新恢复回归使用真实临时记录及目录锁，覆盖运行态/暂停态接管、所有权冲突、
中断请求 UNKNOWN、进程身份不符、启动/清理失败和旧对象失效；VMM 与进程身份操作有替身。
这不替代 Linux `/proc`、真实 pidfd、KVM、网络及存储验收。

本节点完整本地回归 272 项：265 通过、7 项因环境条件跳过，无失败或错误。
核心 wheel 在源码目录外的独立 venv 通过 51 项快照、创建、分叉、节点及 Edge 恢复检查，
未安装 TB2.1/MBPP 应用包；3 个 CLI help、旧模块身份、wheel/源码归档及文档边界检查通过。
wheel 包含 126 个运行时 Python 模块。执行/session 接口、统一 Storage 接口以及剩余
TB2 manifest/API 和 adapter 规则迁移仍待完成；R2 未全部完成，R3 尚未执行。


### R2：执行契约、dispatcher 与 channel 节点

`contracts.execution` 给出只含值的 `ShellRequest`、原结果字段的 `ShellResult` 和
`CommandChannel` 接口。请求值不持有连接、沙箱状态或 journal；Edge 仍按后端能力校验。
`runtime.sessions.dispatcher.ShellDispatcher` 接管原 Sandbox 的命令/代理/范围校验、自动恢复、
命令派发与失败处理，继续使用原 Sandbox 锁和生命周期入口。
`VsockCommandChannel` 负责原 guest 的一次连接一次命令交换；
`DockerCommandChannel` 负责既有 Docker shell、带请求 ID 的命令日志及结果查询。
MicroVM、容器和旧 SDK 方法保留薄入口；模块级故障注入仍在组装处解析。

命令 bytes、guest 头部、合并输出和返回字段未改动；超时与截断证据原样返回。
容器旧路径缺少 `timed_out` 时不补成 false；UNKNOWN 与命令已完成但退出失败分别处理。
microVM guest 不支持命令级 request ID，仍由 Edge journal 持有原 ID，channel 不假装具备
guest 去重。容器使用原请求 ID/参数及查询证明。两者都不因丢回复或超时自动重试。
`RequestOutcomeUnknown` 移入共享异常契约，SDK 旧路径重导出同一类，runtime 不反向依赖客户端。

相对于 `3763e1a` 的 6 项 AST 归一化检查覆盖 dispatcher、两类通信、查询和容器前置校验。
新增 22 项回归覆盖真实本地 Unix 字节流、UTF-8 长度、分片/断线/拒绝/过大回复、
代理展开后的边界、agent/verifier 预算、原锁串行、命令超时与容器日志 UNKNOWN/冲突。
Docker 执行与 VMM 使用替身，不能替代真实内核和 guest 验收。

本节点完整本地回归 295 项：288 通过、7 项因环境条件跳过，无失败或错误。
核心 wheel 在源码目录外的独立 venv 通过 88 项命令通信、Edge/容器 RPC、日志、快照、
创建及节点回归；未安装任务应用，旧模块/异常身份与 3 个 CLI help 通过。wheel 包含
130 个运行时 Python 模块，源码归档、文档链接及包边界通过。
此处完成执行接口职责收敛，不实现交互式 session、
后台进程、流式 stdout/stderr 或完整 aether/chronus；这些按既有后续机制里程碑推进。
统一 Storage 接口与剩余任务规则迁移仍属 R2；真实 Linux/KVM、Docker 与短 RL 验收仍属 R3。


### R2：运行时 Storage 接口节点

`contracts.storage.DiskStorage` 明确环境准备、私有写盘、磁盘检查点、恢复、验证与释放接口；
`DiskPaths` 只表达目录/root/work 路径，既有设备 ID、daemon 身份和清理状态继续由 Sandbox
持有并写入原 registry。`storage.service.RuntimeStorage` 组合现有 catalog、文件操作和
OverlayBD RootStore，不持有第二份沙箱状态、租约、journal 或引用账本，也不导入 runtime。

创建、恢复、分叉和 registry 的通用环境准备经过同一入口，仍由现有 catalog 校验
local/3FS 来源、EROFS 顺序、内核、DAX 设备索引与 VMM 摘要。准备不复制远端数据。
私有 ext4 和 OverlayBD＋ublk 保留各自机制，checkpoint/restack、稀疏复制、校验及回收
通过同一存储服务。稀疏文件和快照哈希原语已移入 `storage.snapshots`；旧名称为兼容桥，
文件操作与 hash fault hook 在 Edge 组装处晚绑定。

运行时仍协调 guest sync/VMM pause/stop、快照发布与 registry commit，之后才释放节点租约。
分叉读者等待、共享 CAS 引用和 prepared snapshot 发布语义未更换。
设备创建返回的 ID/runtime 先赋给 Sandbox，再进行可能失败的 daemon 身份查询；
身份查询失败保留清理句柄，避免新抽象把已经创建的块设备遗失。
设备删除仍核对 daemon 身份，消失的设备仅清理私有 runtime，不能删除新 daemon 复用的 ID。

12 项限定的 AST 对照相对于 `18342b8` 覆盖哈希/复制原语、snapshot manifest、registry
persist/load 及四处设备获取顺序；不宣称整个生命周期 AST 不变。
13 项新增存储回归覆盖本地 DAX pin、远端层不预读、私有盘隔离、root/work checkpoint
完整性、OverlayBD 层集/内容校验、丢失 restack 回复不重试、daemon 换代与获取后失败清理。
实际 ublk、3FS mount 和 VMM 有替身；本地文件与完整性检查实际执行。

存储能力仅表达对应磁盘机制，runtime 仍核对隔离、quiescence 和配置兼容性。
完整 `publish_diff`/`pack_diff` 仍未支持；本节点不添加虚假的发布成功路径或改变既有 SDK 能力表。
容器只读层继续使用现有 catalog/guest 挂载组件，不由此宣称容器快照或内存卸载已完成。

本节点完整本地回归 309 项：302 通过、7 项因环境条件跳过，无失败或错误。
核心 wheel 在源码目录外的独立 venv 通过 101 项存储、快照、分叉、Edge、命令及容器 RPC
回归；未安装任务应用，旧模块/文件原语身份与 3 个 CLI help 通过。wheel 包含 133 个
运行时 Python 模块，源码归档、文档链接及包边界检查通过。
剩余任务 manifest/API/adapter 规则迁出仍属 R2，
真实 Linux/KVM、Docker、3FS/ublk 与应用短验收仍属 R3。
