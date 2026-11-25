<a id="contributing"></a>

# 贡献指南

[English](CONTRIBUTING.md) · **简体中文**

本项目独立复现 DSec 机制。请从[路线图](ROADMAP.zh-CN.md)、[快速入门指南](docs/guides/QUICKSTART.zh-CN.md)和[比较协议](docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md)开始。实际运行需要 Linux 及显式准备的虚拟化和存储组件。分布式 API 部署仍是开发预览，具体隔离和故障验收边界见路线图。

提 issue 时请说明问题、受支持的配置、预期与实际行为，并提供最小复现。对于规划工作，请附路线图工作编号（如 `RM-103`）及验收条件。所附日志须移除凭据和私有任务数据。

<a id="work-management"></a>

## 工作管理

提议或继续一项工作前请先读[当前能力/任务状态](docs/PROJECT_STATUS.md)。[project/registry.json](project/registry.json) 是唯一人工维护的当前状态台账。Markdown 状态页从台账生成，ROADMAP 定义方向和里程碑，架构文档记录设计决策，报告保留带版本的历史证据。GitHub issue 和 PR 引用台账 ID，其讨论不单独维护另一套状态。

请同步维护中英文文档。在[文档清单](docs/languages.json)中按原文语言登记主文件路径，并将该路径加入[源码发布清单](tools/package_release_source.py)。修改台账时，在[状态页译文表](project/status-translations.json)中补上对应译文，再运行 `python tools/check_project.py --write` 生成两种语言的状态页。

开始执行前，创建或选择一个 `TASK-NNN`，并记录以下内容。

- 类别为 `feature`、`bugfix`、`regression`、`integration_acceptance`、`performance` 或 `maintenance`。缺少组合测试不能据此重开已实现的功能。
- 关联的 `CAP-*`、通过能力关联的路线图编号、负责人、已有实现与测试，以及开工前审阅的 `EV-*`。
- 具体触发原因、本次增加的行为或证据、复用计划及受影响的边界。若已验收能力上新增功能，须说明边界变化。
- 有限的验收检查、所需本地/Linux/GPU/管理员资源、依赖和停止条件。明确说明什么结果会结束实验。

任务状态为 `candidate`（候选，尚未承诺）、`ready`、`in_progress`、`blocked`、`done` 或 `cancelled`。包括实验和管理工作在内，同一时间只能有一项 `in_progress`。受阻任务须指出缺少的条件；不要静默切换到可选工作。先核对候选增量能否推进用户当前目标，再明确选定执行。常规可逆实现决定无需重新审批。

已完成的任务保持关闭。后续确需重开时，在 `reopens` 中引用原 ID，审阅已有证据并指明 `changed_boundary`。有限回归或新的能力组合单独建项，保留原验收结论和边界。不能通过拆成多个新 ID 绕开这项审查。

能力状态包括**在记录范围内** `accepted`、`partial`、`implemented`（待补证据）及 `planned`。证据区分本地测试、真实 Linux 运行、应用与形式模型。`date_kind` 区分实际运行日期与之后的索引复核日期。每项证据指向报告或测试，并保留制品、配置标识或其位置。失败记录留在报告中，不能用于关闭验收项。导入的历史记录不能认证当前工作树。`delivery` 单独登记 `working_tree`、`committed` 或 `released`，测试通过不等于已发布。

相关代码、协议、guest 制品、配置或部署边界改变，出现可复现缺陷，或明确规定发布验收时，触发重新验证。时间久远或最新报告未提及不触发重新开发。优先运行已有测试，只有明确指出缺少的保证时才新增用例。以下 CLI 可建议受影响的能力，不会变更状态。

```sh
python tools/check_project.py --base HEAD
python tools/check_project.py --changed src/dsec/sdk/native.py
```

关闭任务时记录改动、验证结果、证据及剩余限制。若使用真实环境，通过正常入口停止或核对所拥有的资源。`done` 任务须有证据且依赖均已完成。设计变更更新已有 GEN/CPH 决策。更新台账并重新生成、检查两种语言的视图。

```sh
python tools/check_project.py --write
python tools/check_project.py
python tools/check_docs.py
```

CI 拒绝过期视图、失效引用、未知能力或证据 ID、缺少关闭证据或重开理由、依赖环及过多并行执行项。CI 检查记录一致性；仍由审阅者判断工作是否重复、范围是否合理、证据是否支持结论。进度更新使用任务 ID，说明新发现、剩余验收条件和实验终点。

每次改动聚焦一种行为。PR 说明对用户的影响、实现选择、验证和限制。保留项目 MIT 许可证和 Miles 派生代码的 Apache-2.0 声明。

添加新的运行器或 helper 前先扩展既有实现，保留一个权威来源；兼容入口委托给它。部署差异用配置表达，避免复制启动脚本。指南和契约描述当前行为，验收报告记录有边界的证据。调试流水账不写入运维指南。训练器兼容补丁保持显式可选，独立于核心包。

<a id="source-ownership-and-refactoring"></a>

## 源码归属与重构

| 路径 | 职责 |
| --- | --- |
| `src/dsec/` | 沙盒契约、SDK、控制面、运行时、存储和 rollout 服务 |
| `src/dsec_adapters/` | 轻量框架与任务适配，不含训练算法 |
| `src/dsec_image/` | 通用镜像准备 |
| `compat/python/` | 已有顶层导入和历史启动模块 |
| `apps/` | 可选基准测试数据、评分器和训练配方 |
| `native/`, `guest_agent.c`, `guest_native.c` | guest 执行机制 |
| `deploy/`, `tools/` | 部署模板和经过审查的构建、验收命令 |
| `tests/`, `verification/` | 可移植回归和有界并发模型 |

新实现放在负责该职责的包中。保留兼容别名时维持模块身份。三个历史模块（`tb2_backend`、`service_admin` 和 `firecracker_smoke`）仍含旧逻辑；提取这些逻辑须另行审阅依赖及部署影响，避免继续扩展旧模块。

worker 指进程角色。episode 协调归 `dsec.rollout.episodes.EpisodeService`，只有其锁和日志顺序有权决定状态变更。动作提交、对话视图生成和评分器调用由独立组件完成，但不各自维护生命周期状态。旧 worker 构造器只适配配置并继承该实现。RPC 消息封装和进程启动留在 `rollout.server`，任务规则放在可选应用。模型、token 和训练状态位于 episode 服务之外。

控制面和 Host CLI 重构遵守已批准的[CPH-001 至 CPH-011 设计决策](docs/architecture/MODULAR_REFACTOR_DESIGN.md#control-host-decisions)。审查中引用适用的决策 ID 和兼容性证据。变更决策时，在原记录内说明原因和迁移影响，同步更新受影响的依赖检查及协议/模型映射。不要复制成另一份检查清单或日志。

Host 配置和组合属于只读准备：`host.configuration` 归一化复制的数据，`host.composition` 构建服务计划。只有 `host.services` 可以实际修改目录、环境、预算或进程，明确的部署输出由 `host.deployment` 生成。CLI 负责解析和路由；其他模块不得导入 CLI。共享验证应归入契约层，避免依赖运行时。

部署也是重构验收的一部分。拓扑/制品的只读规划与密钥和文件渲染、管理员配置、服务启动要分开。集群生成器的 `--plan` 必须无需 OpenSSL、Docker 或已安装的运行时，且不写文件。异构节点复用角色配置；GPU 和模型管理留在沙盒系统外。单节点参数和批量 `--nodes` 归一为同一计划，所有角色使用 `deploy/cluster/configuration.py`。节点证书/token 和按宿主导出的范围须与控制器路由一致。创建新部署目录会生成新身份，不能用它给原部署加节点或升级。只有经过明确的故障及新宿主验收，才可把批量加入、维护、升级和回滚列为受支持的部署流程。

集群决策逻辑位于 `control.cluster_api`、`rollout_api`、`iam`、`cluster_inventory`、`placement`、`watcher`、`node_gateway` 和 `rollout_gateway`。它们使用注入的接口，不依赖 Host/SDK 或具体网络、存储客户端。Node RPC 消息封装在 `host.node_transport`，HTTP/TLS 在 `host.cluster_http`，二者都不决定准入或释放。`host.cluster` 将其与实际 etcd 及私有 UDS 客户端组合。共享 RPC 错误位于 `contracts.rpc`，SDK 中旧导入保留为别名。历史 Host 网关构造器继承权威决策方法，兼容分派层不得添加策略。模型变异对照和标准回归须针对生产使用的控制模块。

运行脚本前以可编辑方式安装仓库。仓库目录布局不构成 API。除非另有明确审阅过的迁移，已安装导入名、CLI 参数及持久状态格式保持兼容。单独移动包不能证明运行时、隔离或训练验收通过。

<a id="local-package-checks"></a>

## 本地包检查

按执行要求组织测试：

| 目录 | 用途 | 是否自动发现 |
| --- | --- | --- |
| `tests/unit/` | 核心调度、存储、生命周期与恢复回归 | 是。非 Linux 环境跳过 Linux 专项检查 |
| `tests/contracts/` | 冻结的 v0.1 接口、旧版导入及模块依赖边界 | 是 |
| `tests/adapters/` | 任务、Miles 消息/奖励及验证器保护条件 | 是。旧 3FS 发布器检查需要实验工作区 |
| `tests/packaging/` | 源码导出及可选组件包的边界 | 是。旧组件包检查需要实验工作区 |
| `tests/benchmarks/` | 对照实验的计量及宿主隔离保护 | 在实验工作区显式运行 |
| `tests/training/` | 可选训练器兼容性及 GRPO 探针 | 在实验工作区显式运行 |
| `tests/integration/` | 手动执行的 Linux/KVM、快照故障及任务验收 | 否，文件命名为 `check_*.py` |

审查宿主路径和先决条件后，以模块运行手动检查，例如 `python -m tests.integration.tb21.check_tb2_trainer_rejoin --help`。部分检查要求独立服务、权限或已准备的制品，不属于常规 CI。源码发布包括上表四个可移植套件。CI 和本地检查独立于发行导出器，均使用 unittest 自动发现。四个套件中的新增测试无需逐一登记文件名即可被发现和导出。扩展实验套件不在公开仓库内。

在虚拟环境中使用 Python 3.11 或更新版本：

```sh
python -m pip install 'setuptools>=77' wheel build
python -m pip install --no-deps -e . -e apps/tb21
python -m build --wheel --no-isolation --outdir dist/core .
python tools/check_wheel.py dist/core/*.whl
python -m unittest discover -s tests -t . -v
python tools/check_docs.py
python -m build --wheel --no-isolation --outdir dist/tb21 apps/tb21
python -m pip install dist/core/*.whl dist/tb21/*.whl
python -m unittest discover -s apps/tb21/tests -v
```

`tools/package_release_source.py` 的允许清单定义可导出的运行时、回归工具及应用边界。运行时模块按包元数据导出，可移植测试按四个指定目录导出。必需的发布工具或文档加入显式清单；生成制品及实验状态不得混入。GitHub CI 构建两个 wheel 并运行选定回归，无需配置 KVM、Docker、GPU 模型或 TB2.1 任务。真实沙盒和全新宿主另行验收。

可移植 TB2 验证器测试需安装可选 `apps/tb21` 包，使用测试数据而无需联网下载任务。仅安装核心时仍可导入并构造通用 worker。新增评分器须测试正常结果、无效判定、中断及已完成记录的恢复。基础设施故障不可静默计为模型零分。

变更生命周期、存储、权限或恢复行为，还须在隔离实例中执行对应 Linux 实机验收。记录精确修订、配置、原始证据和回收结果，退役服务前先通过 SDK 停止自己创建的沙盒。单看 VM 的 PSS 不足以证明完整后端的资源优势，模型失败和基础设施故障也应分别统计。

生命周期和原生并发逻辑的变更还需执行以下有界 TLA+ 检查。

```sh
python tools/check_concurrency_model.py --fetch --out /tmp/dsec-model-check-unique
python tools/check_shutdown_refinement.py --fetch --out /tmp/dsec-shutdown-check-unique
python tools/check_native_race_refinement.py --fetch --out /tmp/dsec-native-race-check-unique
python tools/check_cluster_refinement.py --root /tmp/dsec-cluster-check-unique
```

每次使用新的输出目录，保留日志及反例。Java 11+ 和实现对应检查使用的 C 编译器仅为开发依赖。`--fetch` 显式下载固定且经过 SHA-256 核验的 TLC，离线使用 `--jar /path/to/tla2tools.jar`。CI 须验证安全模型穷举通过，并在不安全变体中观察到预期的不变量违反。解析失败、缺工具或搜索不完整都算失败。见[模型边界与代码映射](docs/architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md#formal-concurrency-model)。模型检查验证指定的有限协议，仍需单独验证实现与模型对应。将代码的准入、提交、隔离旧操作和清理边界映射到模型，并将反例转为受控实现测试。节点管理变更须保持 `NodeLifecycle` 映射；移除守卫时，三个规定的负对照须失败：准入与排空竞争、还有工作时退役、退役后的过期转换。运行 `tests/unit/test_node_lifecycle.py` 中的标准受控交错测试；随机重复不能替代它们。发布变更还需将 `NodeUpgrade` 映射到 `NodeLifecycle._transition` 的待处理计划守卫和实际加载版本证明。两种移除守卫的对照须能发现未证明完成和升级待定时恢复的问题。兼容契约改动按迁移审阅，仅有版本标签不能证明格式兼容。安全变更须保持 CPH-011 对应关系：`CredentialRotation` 映射 IAM CAS 及新旧身份完成/取消证明，`TransportRotation` 映射发布代际守卫及受理请求时的信任检查。运行 `python tools/check_security_refinement.py --root /tmp/dsec-security-unique`，移除五个守卫须分别导致指定回归失败。`_credentials`、`_security_files` 和 `_tls` 应独立于 Host、控制面及运行时，共享依赖检查只允许标准库和契约。TLS 行为改变时需要真实 TLS 测试及受影响的安装版服务轮换验收；有限模型通过不能证明 PKI 正确。停止逻辑对应检查会编译带探针的真实 C agent，在指定边界注入信号，并让同一 TLA+ 模型接受或拒绝观察到的状态序列。它必须拒绝历史赋值错误的变异版本。改变边界时应审阅并更新映射，不要用 Python 复制模型转移规则或削弱安全性质。原生竞争检查运行 `tests/unit/test_native_races.py`，实际调用日志、生命周期方法及本地 C agent 命令。同步屏障复现排队取消、迟到回调、实例代际替换及容器停止与准入的顺序。四项规定代码变异须使指定回归失败；真实轨迹应符合不安全规格、违反其不变量，并被安全规格拒绝。标记代码片段改变、缺少轨迹、跳过测试、配置或清理出错，以及 TLC 非预期失败，都会使映射检查失败。源码快照及哈希将结果绑定到实现。这些检查使用带探针的 Docker/KVM 驱动。真实 Linux 验收使用 `tools/verify_native_races.py` 和全新专用 `--root`，固定 `--binary`、`--kernel`、`--template`、`--native-agent`，可选 `--vm-catalog`/`--vm-environment`，以及 `--container-catalog`/`--container-environment`。通过 `--source-manifest-sha256` 提供受测源码清单的 SHA。一次最多启动两台 VM 和一个容器，不用 GPU 或网络，结束时停止所创建的资源。预备磁盘须已包含受测 agent，该工具不会修改不可变模板或重启现有服务。用六条实机轨迹再次检查同一组模型：

```sh
python tools/check_native_race_refinement.py --fetch --observed /path/to/private-root/report.json --out /tmp/dsec-linux-trace-check-unique
```

行为或清理报告失败时不能作为通过证据。记录源码、wheel、agent 的身份标识，完整原始证据及宿主服务连续性。仅测试夹具通过不满足此验收。

变更已映射的并发边界时，审阅状态转换和不变量映射，必要时更新受控回归与轨迹投影，并运行相关的集群、安全、停止和原生执行对应工具。源码变异不再匹配时须审阅原因，不能跳过。生命周期行为变更另需相应的 Linux 实机验收。CI 将源码快照、观测、检查日志和反例保存为 `concurrency-evidence` 制品，保留 14 天；不包含 TLC 临时状态目录。

受测制品标识见发布说明。路线图中的未来事项在附上验收证据前保持计划状态，不得用不同修订的结果覆盖历史或冻结证据。

节点权限检查应先于创建安装目标、网关状态或协调服务连接。`host/node_probe.py` 必须独立且仅依赖标准库；已审阅的管理员安装器会嵌入它，并重新核查实际平台与固定运行身份，不得信任缓存的预检结论。复用实例范围的 helper 生成器，禁止新增通用特权命令入口。受控回归覆盖设备缺失、设备权限被拒、能力边界、命名空间重映射，以及写入前阻止初始化。容器或供应商名称不能决定资格。测试夹具成功不代表真实 Linux 启动或隔离验收通过。
