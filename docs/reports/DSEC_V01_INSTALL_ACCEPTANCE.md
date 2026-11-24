# 单机 v0.1 安装与部署验收

## 2026-10-08：模块化 R3 实机回归

本节对应重构基线 `775b5be` 加本轮持久化/忙碌拒绝修复，不覆盖下面历史 r5
证据的版本归属。核心安装 wheel SHA-256：
`63839e2000c72045ed662e4b66558b16e9ad4ea546cb496f694cf53701010f0e`。
独立 venv 使用 installed wheel 和 Python isolated mode，TB2.1/MBPP 为分别
安装的可选应用。复用已有实验机及固定外部制品，不等同于第二台全新主机验收。

| 验收 | 本轮结果 | 原始证据文件 |
| --- | --- | --- |
| Linux 发行回归 | 323 项：322 通过，1 项因发布包不含实验目录别名跳过，无失败/错误 | `installed-linux-busy.json` / `.log` |
| 非 TB 生命周期 | 自动监督、同一 VMM 恢复、暂停重启、动作去重、评分 1、最终回收 | `counter-recovery-fixed.json` |
| 旧 VM 升级 | 一台 RUNNING、一台 PAUSED，原 registry、身份、私有文件与动作记录保留，新 Edge 接管两条租约 | `live-old-to-new-fixed.json` |
| 旧容器升级 | 旧安装版客户端及旧 standalone agent 创建的容器，由新 Edge 接管；容器 ID 不变、动作不重复、最终租约归零 | `container-old-to-new.json` |
| Container Edge 重启 | 真实 EROFS/OverlayFS、network=none，容器/租约 ID 不变，私有写入保留并正常清理 | `container-restart.json` |
| SDK/worker 共享准入 | SDK 占满 CPU 预留后，worker 按 cpu_budget 等待；释放后准入，最终无租约 | `shared-admission-fixed.json` |
| ready 租约交接 | 同一 VM、同一节点租约；CPU 预留 0.05→1，内存估计保持 512 MiB；客户端池命中 45.68 ms | `ready-handoff.json` |
| TB2.1 官方验证 | openssl-selfsigned-cert，经 live/paused 重启、动作去重，官方 verifier 评分 1 | `tb21-openssl-final.json` |
| 准备态分叉 | 同一基线的两个顺序 episode，内存计数分别从 8→9、私有写入隔离、源删除后恢复、最终 CAS 回收 | `tb21-prepared-fork-fixed.json` |
| MBPP 固定候选 | 64/64 符合预期：正确/隔离为 1，错误/超时/提前退出为 0；实际节点租约最高 16 | `mbpp-execution.json` / `mbpp-receipts/` |
| 原生短 GRPO | Qwen3.5-2B + verl/SGLang，n=8，四步、非零梯度、模型/优化器检查点和逐条证据链接通过 | `rl-four-acceptance.json` |
| 真实 3FS 来源 | 双副本就绪，48,201,728 字节 EROFS 对象发布并校验；两台 VMM 的实际文件句柄指向 3FS，写隔离及一次暂停恢复通过，最终无租约 | `threefs-ready.json` / `threefs-erofs-acceptance-final.json` |
| 新旧版本固定动作开销 | 同宿主、同只读制品缓存、同声明资源，每版 4 台新建 file-ext4 VM，创建/执行均完成并回收 | `paired-revision.json` |

创建墙钟中位数：旧版 782.35 ms、新版 792.02 ms；固定动作执行中位数：
旧版 47.80 ms、新版 42.80 ms。样本小、固定顺序，包含调度/RPC，不足以作
统计显著性或吞吐优势声明。ready 的 45.68 ms 只对应一次已启动 VM 的取出，
不是冷创建、分叉恢复、完整 episode 或规模下的 p95。声明 CPU/内存需求是
调度预留，不是 CPU 利用率或实测 RSS；两者不互换。MBPP 的 32 请求并发受
16 槽节点预算约束，不能称为 32 台 VM 同时执行。

短 GRPO 保持 Non-Thinking、8192 回复上限、LoRA rank/alpha 8/16、MLP targets、
temperature 0.7、top_p 0.8、top_k 20、min_p 0、presence_penalty 1.5、seed 42。
训练四步分别评分 8/16、15/16、8/16、0/16；最终小规模验证为 8/16。
第 2 步存在混合奖励，梯度范数 0.255859375；其他步骤的零/极小梯度不都代表
任务学习信号。80 条生成中 72 次沙盒执行、8 次明确标注的模型格式零分；
停止原因 74 `stop`、6 `length`，不以动态 batch clip_ratio 判定截断。所有
真实执行已清理；TITO、mask/logprob 长度与不可变生成/回执链接一致。此轮
只验收重构后的原生训练接入，不构成完整 MBPP 成功率或训练前后收益对照。
先前单步 `rl-acceptance.json` 的两组全 1/全 0，梯度为零，单独不能证明有效更新。

本轮实机暴露并修复两项核心缺陷：并发发布预算共用固定 `.tmp`，以及短暂锁
竞争被永久提交成固定 request ID 的忙碌失败。分别采用独立临时 inode 和
明确未接纳时取消 journal intent；新 capability 限定 SDK 的同 ID 有界等待，
UNKNOWN 仍禁止重放。服务就绪工具允许 120 秒，覆盖启动时的大制品完整性校验。
验证不以扩大执行超时掩盖模型或 verifier 问题。

首次失败均保留：错误 smoke guest、测试编排的准入等待、旧代理地址导致联网
失败、服务就绪上限不足、分叉时操作锁竞争，以及测试目录继承 0775 被权限
预检拒绝。后者只修正本轮创建的专用目录，不放宽宿主权限检查；升级现有部署
仍需操作员核对目录模式，见 [主机配置](../guides/DSEC_HOST_CONFIGURATION.md)。

证据保存在实验机的 `dsec-r3-20261008/evidence/`，本地镜像归档在外层工作区
`.runtime/r3-20261008/remote-evidence/evidence/`，完整短训练 TITO/回执保存在
`.runtime/r3-20261008/rl-four-evidence/`；不将机器配置、凭据、模型或生成状态
放进开源源码目录。宿主 Docker/Grafana 未重启，专用测试容器已按 SDK 回收。

R3 的计划内功能关卡已通过。3FS 回归使用停止后备份的独立副本，保持原备份
不变；管理员恢复原配置的 `dsec3fs0` / `198.18.73.1/32`、Soft-RoCE 和专用
挂载传播范围。克隆服务在 123.85 秒后达到双副本 SERVING-UPTODATE 和 FUSE
就绪。旧运行镜像缺少备份中 metadata/storage 二进制依赖的 jemalloc，已从
Ubuntu 签名 APT 提取 `libjemalloc2 5.2.1-4ubuntu1`，只放进测试依赖目录，
没有安装宿主包；四个主要程序的动态库检查均通过，版本/哈希单独归档。
该依赖存在于原备份二进制中，不是重构新增。旧容器记录没有额外库路径或库挂载，
但没有保留可确认 jemalloc 安装来源的记录，不能断言来自旧容器可写层。
外部服务部署需固定完整运行依赖，并在目标运行镜像内检查动态库闭包。

两台 VM 读取同一个 3FS 上的 base EROFS 对象，其余三个层仍为本地来源。
宿主 VMM 文件描述符直接确认该来源，没有以 storage 标签代替实际证据。
第一次采样身份与 VMM 组不同，被 proc 权限限制；换成已授予的 kvm 组后两次
采样均无错误。一次 VM 暂停恢复保持私有文件，下一 VM 不继承该文件，最终
节点租约为零。发布时的完整哈希校验已预热该对象，因此本轮不证明冷缓存
读取字节量、按需性能收益、全部层均来自 3FS 或分布式存储行为。

3FS 设备识别、旧专用 NIC、缺失动态库、挂载多行解析和制品目录写权限的首次
问题也保留在测试记录中；这些是外部服务恢复/验收编排问题，本轮没有因此修改
DSec 的底层存储机制。临时存储服务在验收后正常停止；管理员准备的专用网络
设备、私有 bind 挂载及测试副本保留，便于后续复验，不声称它们已删除。

后续 r5 安装候选通过 49 项核心回归、工具盘真实完整性检查及 15 次
固定轨迹评分；最终 43 条记录 STOPPED，无租约/pending/netns/活动设备
或进程引用，宿主无干扰。冷解析与成本结果见
[r5 报告](DSEC_VERITY_PILOT_REPORT.md)。下面先前功能与 GRPO 证据的
版本归属不变；r5 随后已完成独立短 GRPO、跨 episode 分叉隔离及重启
回归，最终 50 条记录停止、无资源残留，见
[最新训练验收](DSEC_V01_GRPO_ACCEPTANCE.md)。

日期：2026-10-06。发行入口阶段完成；完整开源交付仍按
[两天计划](../../ROADMAP.md)继续。此处是功能验收，不是吞吐或
Docker 成本优势报告。

冻结开发 wheel：`dsec_reproduce-0.1.0.dev0-py3-none-any.whl`，183697 字节，
SHA-256 `53b0891380d0c57b4bca87adcdaa69eb5325773fdc6fe907abc8f1e864f2ce09`。
打包边界检查通过：51 个 Python 模块、3 个插件数据文件及发行元数据；
第三方 Miles 许可文本随包保留。核心没有第三方 Python 运行依赖，模型与
训练框架需要各自的依赖。

## 验收与证据

| 项目 | 实机结果 | 原始记录 |
| --- | --- | --- |
| 独立 venv、离开源码目录、禁用源码路径 | 安装与插件导入通过，54 个运行文件逐字节匹配 wheel | `.runtime/release-v01-pilot/pilot-final-audit.json` |
| 非 TB 任务 | 创建、动作去重、暂停恢复、评分 1、停止与租约释放通过 | `.runtime/release-v01-pilot/acceptance-supervisor-r8.json` |
| 自动监督 | 杀死隔离实例 launcher 后自动恢复；同一 worker 与原 VMM 保留 | 同上，`automatic_supervision` / `live_restart` |
| 暂停后双服务重启 | 原私有文件、PAUSED 状态与租约恢复，通过评分与最终释放 | 同上，`paused_restart` |
| TB2.1 安装版 | openssl-selfsigned-cert，官方 tests/test.sh 评分 1 | `.runtime/release-v01-pilot/tb21-acceptance-r8.json` |
| TB2.1 双服务重启 | 同一 VMM、已提交动作不重放；暂停恢复后官方评分 1 | `.runtime/release-v01-pilot/tb21-acceptance-restart-r7.json` |
| 官方证据 | reward.txt、verifier.log、ctrf.json、manifest.json 已归档 | `.runtime/release-v01-pilot/evidence/` |
| 冻结版本短 GRPO | 两组 × 两样本，官方评分 `[0,1]` / `[0,0]`；两次训练切换、4 次真实快照恢复、TITO 和回收通过 | `.runtime/release-v01-pilot/grpo-evidence-r8/installed-grpo-audit.json` |
| 安装版准备态复用 | 同一基线生成两个新 episode；官方评分 1、重启、源删除后恢复与 CAS 回收通过 | `.runtime/release-v01-pilot/installed-fork-r8.json` |
| 最终回收审计 | 两个当前临时实例的 15 条沙箱记录全部 STOPPED，无待执行动作、租约或 netns；临时服务关闭 | `.runtime/release-v01-pilot/post-grpo-fork-audit-r8.json` |

本地 57 项相关回归通过，包括任务插件、对话、worker、封存/分叉、网络、
发行边界与新部署入口。随后在 Linux 实验机用 Python isolated mode 加载
测试、只导入已安装的 wheel，另有 31 项资源预算、准入拒绝、队列恢复、
SDK 和共享分叉边界回归全部通过，无跳过；记录为
`.runtime/release-v01-pilot/resource-tests-r8.json`。TB2.1 保持任务修订
`7131e4375048a0e408a8fb404b5f499d726b695b`。评分轨迹是固定解题命令，
不代表模型的解题成功率，也不代表全部 89 个任务通过。

TB2.1 实例使用真实 EROFS 分层、OverlayBD＋ublk 私有根盘、独立 netns
和已配置 Mac 临时代理；普通 Firecracker 由新安装的受限助手按固定哈希
启动。两槽地址段为 4096–4097，与既有实例分离。常驻 ublk 服务仍沿用
先前系统部署，临时实例存储位于其已授权根下的独立子目录。

## 本轮关闭的部署缺口

任务实现迁入可安装的 `dsec_adapters`，旧 import 仅作别名；worker 官方
verifier 直接使用安装包。主机配置集中描述制品、调度和网络，不依赖实验
源码目录。服务启动使用 Python isolated mode。

ublk 服务需要存储目录的组搜索权限及其 systemd 写目录授权。控制目录为
仅组搜索的 2710，worker 记录保持私有；不能只靠 chmod 绕过服务的挂载
命名空间。已有 TB2.1 guest 制品启动时要求虚拟网卡，因此无网配置不能
作为这些制品的有效部署。两项首次失败均保存记录并释放租约。

旧部署还通过独立环境变量固定 verifier manifest；新部署遗漏它会错误地
比较旧工具盘哈希。现已从同一个主机配置派生 daemon 与 worker 的默认/
按任务 verifier pins，新增回归检查。失败记录保留在 r4/r5/r6 文件中。

服务启停检查支持安装后的模块入口，保持相同组身份的 pidfd 检查；
ExecStop 明确停止 daemon。`Restart=always` 处理 sg 子进程返回码语义，
worker 使用弱依赖保持 daemon 自动恢复期间的任务和租约。

## 交付边界

这验证了实验机上的独立安装实例，复用了已有模型、镜像、内核、ublk 和
只读制品。全新 Linux 主机从零部署、全部可选存储条件、
完整后端 Docker 对照仍是后续门槛；源码交付审查结果见下文。训练内存补丁只在
显式 training-experiment 包中，不计作 DSec 发行功能。

## 冻结运行时短 GRPO

Qwen3.5-4B thinking、32768 回复上限、两组 × 两样本、并发 2，474.759 秒
退出码 0。运行时 54 个文件逐字节匹配上述冻结 wheel；训练侧使用显式实验
包，任务实际运行在安装后的独立 daemon/worker。

两组官方评分为 `[0,1]` / `[0,0]`。第一步梯度范数 0.256775，第二步为 0；
两个 checkpoint 之间 96 个 adapter 张量不同且均有限。第二组奖励相同，
不能据此宣称第二步具有有效学习信号或模型解题能力提高。

四个 episode 的 response 分别为 4603、4967、4449、4220 token，logprobs、
loss mask 与 TITO 一致。每条轨迹在第一次工具调用后实际 PAUSED、退出原
VMM，再由后续工具调用恢复到新 VMM，沙箱身份和步骤连续；四条独立写盘
使用相同的 6 个 EROFS 源层。此项证明组内独立 episode 与每条 episode 内
恢复，不替代跨 episode 准备态分叉验收。

三个 0 分轨迹的官方 CTRF 均定位到 `test_python_verification_script` 断言
失败，其中一条 agent 提前因 `invalid_format` 结束；不是缺失 verifier 评分。
正负分的官方日志、原始回复、TITO、worker 记录、checkpoint 和资源采样已
保存。GPU 采样峰值 14406 MiB，训练容器 memory peak 20.525 GiB，两者只
描述本次训练实验，不能作完整 DSec 后端成本。四条记录最终 STOPPED、
无 pending、无租约；先前安装回收审计仍只对应 GRPO 启动前的时点。

## 安装版跨 episode 准备态复用

`tools/verify_installed_fork.py` 通过同一个安装 venv、统一主机配置、正式 SDK
和任务插件验证。两槽实例采用一个封存基线加一个分支，两条 episode 顺序
创建；它不替代此前四分支的并发性能结果。

基线中准备的 HTTP 服务保留真实内存计数，新分支均从计数 8 和新的 policy
history 开始；第一分支的私有文件和答案没有进入第二分支。第一分支官方
评分 1。封存后双服务重启仍可分叉；运行分支重启保持原 VMM，重复已提交
动作返回相同观察。第二分支存活时停止源基线，共享 lower inode 保留；
该分支随后暂停恢复，私有文件和应用计数继续。最后三个 rollout 全部
STOPPED、无 pending、无租约，最后一个持有者释放后共享对象删除。

最终审计再次核对 54 个安装文件与冻结 wheel 匹配，两个当前临时实例共
15 条沙箱记录均停止，实例 netns 为空，四个临时服务关闭；原两个正式服务
正常且队列为空。GPU 空闲采样 35 MiB / 0%，根盘剩余约 97.6 GiB。

## MIT 候选包

用户选择项目自身代码与文档为 MIT。Miles 派生模块保留 Apache-2.0，wheel
组合许可表达式为 `MIT AND Apache-2.0`，随包包含两份许可与第三方来源说明。
新的开发候选包 184823 字节，SHA-256
`38fd9bb7602aad10b0bcbc816bf71e8d422624de41cb6d9659a8d0566f6f4a7e`。

许可和 README 元数据调整没有改变 54 个运行文件；与上述功能冻结 wheel
逐字节相同。在实验机另一个全新 venv 安装候选包，通过 isolated import、
许可元数据和全部运行文件核对。记录为
`.runtime/release-v01-mit/acceptance.json`。新版 wheel 边界检查为 51 个模块、
62 个条目，许可文件内容也逐字节检查；两种运行包的许可包含检查通过。
历史功能证据继续保留原 wheel 哈希，不将元数据变化冒充重新运行的训练。

## 从源码重建与可选应用安装验收

白名单源码候选 r2 含 96 个文件（包括清单），251944 字节，SHA-256
`eba9161c2e4abfbbac7117795df66dec9dbb0ee44a1b1f3e949f4c6c57f06528`。
每个文件在 `SOURCE_MANIFEST.json` 固定摘要；归档可重复生成相同字节。
源码不含任务数据、实验结果、镜像、模型、密钥或 GPU 训练补丁。
实验机使用固定 setuptools 80.9.0 / wheel 0.45.1 从该归档离线构建：

| 包 | 字节 | SHA-256 |
| --- | ---: | --- |
| 核心 | 185063 | `dc86af4068679aca8ea5aa2abd591ee6cb21857d15e776f751e77257ad41e54a` |
| 可选 TB2.1 应用 | 13460 | `f486c6c92e415f5033d9263eab28dd64cf90d39f5a592d3340bf87cb8dceefc4` |

新的 venv 先只安装核心；确认应用包不存在，并核对全部 54 个运行文件
与源码清单相同。Linux 上 33 项核心回归通过，无跳过。用归档中的 C agent
和本地固定镜像新建 64 MiB 非 TB guest，运行评分 1、自动监督、动作去重、
运行态及暂停态恢复、租约释放全部通过；未复用旧 guest 模板。

随后显式安装应用包，安装版 5 项应用回归通过。应用校验全部 89 个任务
文件的修订与哈希；这项不是 89 题运行或评分通过。代表任务
`openssl-selfsigned-cert` 的现有 OverlayBD 注册路径和新转换六层 EROFS
加 file-ext4 路径均完成相同轨迹及官方评分 1，原始 CTRF/reward/log 已保存。
新制品路径创建 1.463 s、verifier 25.742 s，仅为功能验收的单次计时。
同一安装版的 OverlayBD 准备态分叉验证两条 episode 的 history/私有文件/
内存隔离、源删除后恢复以及最后共享对象释放通过；此轮没有额外重启
TB 服务，前述重启证据仍对应原冻结版本。

新制品启动暴露父目录 setgid 继承问题。先保留配置不匹配的拒绝记录，
随后发现 file-ext4 沙箱目录继承为 2700，被限定 700/2770 的启动器拒绝。
创建时将私有目录明确设为 700，OverlayBD 仍按需设为 2770；没有放宽
root helper 或目录共享范围。该修复是本轮相对原冻结版本唯一运行代码
变化（`sandbox_sdk.py`），新增两项 Linux 权限回归。此前 GRPO 结果未重跑。

最终审计包含两个源码实例各 1 条、共享 TB 验收实例 19 条沙箱记录，均为
STOPPED；worker 的历史 FAILED 记录保留，但没有 pending 或 lease。
实例 netns 为空，6 个相关临时服务 inactive，两个正式服务 active 且队列为空，
根盘剩余约 97.26 GiB。原始验收、官方日志和失败部署记录保留在实验机
`v01-pilot/source-release{,-r2}` 与本地 `.runtime/release-source-acceptance/`。
最后交付归档允许纳入本次文档更新，另以交付清单固定最终摘要；它的核心
与应用代码必须与上述已测源码逐字节一致，不将文档变化视为新增运行测试。

## 宿主隔离与权限修复候选 r3

2026-10-06，新源码归档含 98 个文件，255752 字节，SHA-256
`d4070cfd4cbe726f9e957b251169761dcc23a6556d393933a217cd809df21cf3`。
实验机离线重建的核心 wheel SHA-256 为
`ee370cb1ae15b736479eac53dc8e510f08b2e2d8ffdfc5237f0618283f1e9b6a`。
新 venv 的 40 项核心回归通过，全部 54 个运行文件匹配归档；之后显式安装
原可选 TB2.1 应用包。guest 模板与既有任务制品复用，没有冒充重新构建。

两个核心改动：创建 OverlayBD 根盘前检查 root daemon 的真实 umask，拒绝
不能保留组权限的配置；网络 helper 在状态目录、锁和网络操作之前核对
宿主 mount/net 上下文。上游 Rust 创建不存在运行目录的协议没有改变。
实验启动器另将私有 Docker 的 mount/net 均隔离；DSec 服务仍在宿主管理
可持久化的 guest netns 句柄。r1/r2/r3 失败实验入口退役，证据保留。

管理员安装精确 helper 修复并移除一个已确认失效的空句柄后，正常 SDK
恢复并停止原失败 rollout。r4 使用新包实测两种错误 helper 上下文的拒绝，
真实 root ublk daemon 的 0027 umask 被非 root 客户端在 RPC/设备/运行目录
分配前拒绝。随后 Docker 6 次、DSec 新建 6 次、准备态分叉 3 次均评分 1。
宿主服务与原容器 PID、docker0 和 Grafana 健康状态保持不变。最终私有
实例 33 条记录全部 STOPPED，无租约、pending、所属 netns、服务 socket
或遗留设备引用。26 项安装版实验/协议/权限回归也全部通过，无跳过。

证据位于 `.runtime/release-pair-r4/`；完整成本报告见
`.runtime/release-pair-r4/report/REPORT.md`。这次不是短 GRPO 重跑，也不代表
全部 89 个任务评分通过或所有生产崩溃模式已经覆盖。成本结果未显示整体
优于 Docker：启动全文校验 verifier 工具盘造成大量宿主文件驻留；此问题
与 guest/VMM 匿名内存区别记录，不通过改变统计口径隐藏。

## 原生会话、文件与流式 SDK 实机验收（2026-10-10）

候选基于 `ccd61f0`，补充 Linux GCC 路径缓冲区修复、容器原生 socket
路径长度部署校验与可复用验收入口
`tools/verify_native_sdk.py`。两种 C agent 均以静态链接、`-Wall -Wextra
-Werror` 编译通过。干净源码归档构建的核心 wheel 含 141 个运行模块，
149 个条目。含 socket 路径校验的最终核心 wheel SHA-256 为
`f75011957988275b89923b9ffab191e2c2c2ea09c72335ee9d67e4146b3e1089`。
安装在独立 venv，未替换旧服务、旧 guest 或用户 Docker 网络。

真实非联网 microVM 完成 10 项检查，四层 EROFS Python microVM 完成
同样 10 项，分层容器完成其中 8 项。覆盖持久 cwd/env、不同会话隔离与
重叠执行、160012 字节分块二进制文件、跨 sandbox 私有文件隔离、重复
请求去重与冲突拒绝、stdout/stderr 流式重连、精确取消、连续输出限额
与超时。microVM 还验证执行中暂停拒绝发生在副作用前，以及空闲会话
随内存快照恢复后保留 cwd/env 和文件。容器不支持内存快照。

另三项真实恢复检查验证：空闲 Edge 重启后采用原 VMM 与 shell；暂停
状态重启后首次原生调用自动恢复；执行中 SIGKILL Edge 后请求为 UNKNOWN，
原 VMM 停止，重复提交被拒绝。另一个真实分层容器在 Edge 重启后保留
原容器 ID、shell cwd/env 和文件；重复命令没有再次写入。
新 Edge 对旧 Python guest 的原生调用明确
返回 UnsupportedCapability，随后旧单次 shell 仍可用。

应用回归使用既有单次命令入口，不将训练器改造成原生会话使用者：

- MBPP 固定代码 32 个 episode，正确/错误/超时/提前退出评分均符合预期，
  所有收据保存，结束后节点租约为空。这不是模型 benchmark 成功率。
- TB2.1 `openssl-selfsigned-cert`，任务修订
  `7131e4375048a0e408a8fb404b5f499d726b695b`，官方 `tests/test.sh` 评分 1。
  保留 CTRF、reward、testsh/verifier 日志，verifier 约 8.15 秒。
  本次显式使用 file-ext4 引导盘、原六层 EROFS 与离线 verifier 工具盘；
  未将它描述成新的 OverlayBD/3FS 组合验收。
- Qwen3.5-2B + verl/SGLang，Non-Thinking、LoRA rank 8/alpha 16、GRPO
  每题 8 个 rollout、response 上限 8192，完成四步训练与训练后验证。
  前两步组内奖励相同，优势/梯度为 0；后两步梯度范数分别约 0.01263、
  0.00001287，验证参数更新路径真实执行。64 个训练 episode、16 个验证
  episode 都保存评分收据；其中 76 条进入 sandbox 后正常停止，4 条在
  模型格式校验阶段得到 0 分，没有创建 sandbox。最终验证样本平均评分
  0.5；仅两道验证
  题，不能据此宣称模型能力提升或替代原有完整 MBPP 评测。

首次 TB 部署沿用强制配置 eth0 的旧引导盘，却没有配置网卡，PID 1 因
`ip: SIOCGIFFLAGS: No such device` 退出。保留失败记录后，使用现有
`dsec_image.boot` 生成匹配的无网启动盘；任务层和官方 tests 没有改写。
该失败提醒使用者将制品的网络 recipe 与宿主配置配对。

额外容器重启检查暴露了较长部署目录使 native.sock 路径达到 111 字节
的问题，原能力探测把操作系统的 AF_UNIX 路径错误误报成缺少 guest 功能。
现在 opt-in 原生容器在创建 backend 前检查完整路径不超过 107 字节，
ASCII 和 UTF-8 边界有回归覆盖；未启用原生功能的旧容器不受新增限制。

最终 Linux 全套回归运行 323 项：322 通过、1 项预期的发布边界跳过。
中途全套回归发现一次 SIGTERM 到达停止标志检查与阻塞 accept 之间的
关闭竞争，导致原生 agent 未退出。保留失败记录后，将监听 accept 改为
非阻塞、结合有界 poll，新增 12 次真实进程关闭检查，并回收那一个测试
残留进程。连接 accept 后显式恢复阻塞模式，兼容 macOS 与 Linux 对 socket
标志继承的差异；macOS 的 18 项原生回归也通过。测试 setup 失败时同样
保证停止测试进程。修复后的 C agent 重新完成分层 microVM、容器和真实崩溃恢复
验收。应用 GRPO/TB 回归发生在该关闭修复之前；该修复未改变应用使用的
旧单次命令执行路径。本轮未重新跑完整模型 benchmark。
证据保留在实验机 `/home/xiaoxiaohu/dsec-native-20261010/evidence/`。
最终审计无本轮运行 VM、容器或节点租约；临时 Edge/worker 已停止，原有
TB2.1/MBPP 服务及 Docker/Grafana/Prometheus 等五个容器保持运行。GPU
回到约 1 MiB 占用，磁盘余量约 74.66 GiB。训练 checkpoint 和失败证据保留。
本轮是单宿主功能验收，不证明全部 TB2.1 任务、生产多租户安全、并发
性能、所有存储路径组合，或 DSec 论文的性能指标。

## 2026-10-25：三条禁止联网执行路径

本轮先完成框架执行路径，未启动 SWE-Gym、模型推理或 RL。FnCall 使用 QEMU/KVM
内预创建的 Docker 容器；Container 使用 QEMU/KVM 内 Docker 和原生 aether/chronus；
MicroVM 保留 Firecracker。所有公开操作经过已安装 SDK→HTTPS API→IAM/Placement→
mTLS Node gateway→私有 Edge，不以宿主 Docker 冒充 QEMU 路径。

最终安装版 wheel SHA-256：
`e1b581cca785a473fb83d12063785730b8956021d9860476eddc47b7e5317e70`。
版本仍为 `0.1.0.dev0`，Edge 持久化契约提升到 **2**。自动升级/回滚不能跨越契约 1/2；
旧普通 Container 请求字节与 digest 保持不变，不代表旧 Edge 可以接管新状态。

验收使用一台 Linux Node、一个专用单成员 etcd 容器、独立普通用户 venv 和本轮 API/
网关/Edge 进程；最多同时运行两台 VM，不使用 GPU。它验证真实执行与请求/回收边界，
不重复证明既往 HA 控制面关卡，也不声称跨主机高可用或完整多租户生产安全。

| 路径/边界 | 实际检查 |
| --- | --- |
| FnCall | 同一容器 ID 的多个调用之间无上次文件或后台进程；固定 ID 去重；超时后清理并可继续独立调用 |
| Container | 工作区、shell 状态和二进制文件保持；真实内层原生 agent；无宿主 Docker socket/KVM 设备暴露 |
| 并行隔离 | 两台独立 QEMU guest 的 CID 不同，工作区互不可见 |
| MicroVM | Firecracker 命令和原生会话；暂停/快照/恢复后文件保持 |
| Edge 重启 | 接管原 QEMU/容器 ID；已完成写命令不重复执行；停止后仍可查询动作证据 |
| 资源准入 | 只申报内层 512 MiB 而未覆盖外层 1024 MiB 的创建被拒绝，无 VMM/物理租约副作用 |
| 故障取证 | 在已持久化调用意图后，按 PID/启动时间/argv 核验并终止本轮自有 VMM；API 返回 UNKNOWN 的命令与原因，不重放；退役后证据仍可查询 |
| 回收 | IAM 占用、物理租约和 CID 均归零，受管 QEMU 私有写盘实际删除；日志和请求证据保留 |
| 宿主连续性 | 五个原容器的 ID、PID、启动时间及网络保持完全一致；专用测试服务和 etcd 已停止 |

本地干净源码回归 **549 项：540 通过、9 跳过**；Linux 安装版关键回归 **84 项全部通过**，
导入确认为 venv 内的已安装包。52 组有限模型/预期反例校验通过；FnCall 的 3 个源码负向
变异与集群的 12 个负向变异均被对应回归捕获。有限状态检查和受控测试不构成自动实现精化
或无限状态的活性证明。原生并发轨迹校验另保留独立报告。

单次样例的 FnCall/Container 冷创建约 4.1–4.3 秒，包含模板复制和 guest 内 Docker 准备；
不作为分布统计、暖调用性能或 60 ms 启动指标。新 QEMU 路径使用私有 ext4 和内层 VFS，
未宣称完成 EROFS/OverlayBD 镜像共享。MicroVM 既有 EROFS/DAX/OverlayBD/3FS 机制
没有改变；本轮没有重新测试所有存储路径组合。

实测促成的修复包括：guest `/dev` 重复挂载；无网络 guest 的 userland-proxy 缺省配置；
containerd/classic Docker 对镜像 ID 的不同表示；旧终态 CID 记录对新 token 的处理；失败
创建证明后的配额归还；UNKNOWN 的空 response 映射；实际私有盘删除前不确认资源回收。
验收工具在阶段切换时只读等待 Watcher 容量刷新，不重发未知创建。一次测试预算误把
loopback 控制面流量限制为 10 Mbps，引发正常的 `network_saturation` 拒绝；最终修正测试
预算，没有为跑过验收而关闭系统的资源守卫。

早期另有一次控制台建立前的 `RuntimeError`，旧记录未保存具体消息，**未能追溯根因，
不计作已定位修复**；停止和 CID 回收已有证明。现已持久化启动错误类别/消息，以及不确定
动作的参数和原因，最终轮未重现。首次失败记录保留，未用后续成功覆盖。

原始最终证据在实验机 `/home/xiaoxiaohu/de25/api-r7/`；本地副本位于
`.runtime/execution-paths-20261025/linux-final/`，完整回归、模型及代码变异报告位于同级目录。
构建过程中已停止的 13 个原型裸盘副本已按白名单和归属检查清理，保留摘要、日志、状态
记录及最终两个模板。完整生产隔离、host-user vsock 授权、外连策略、Container 内存
卸载、QEMU 共享镜像存储、更多部署拓扑与规模性能仍按 roadmap 单独验收。


## 2026-10-25：QEMU guest 通道认证补强

在上轮三路径闭环基础上，补上“宿主 CID 不是 Edge 身份”的缺口。Container/FnCall
要求每 VM 随机密钥、随机挑战和方向/端口绑定的双向 HMAC；5002 控制、5001 原生会话
及流式输出均在任何执行入口前认证。密钥通过私有 virtio-serial 通道一次注入，不写入
guest 文件或交给任务容器；Edge 重启使用保留的私有密钥，不重新注入或重放动作。
root 和 Edge 服务账户仍受信任，本轮没有验证抗连接洪泛或完整生产隔离。

安装版 wheel SHA-256：
`96c4cb96830c3abb1635c2db5c1968f3104cf3926d05393b66fea2dfa5988350`。
Edge 契约仍为 2；QEMU catalog/descriptor 改为 schema 2。旧 catalog 和无认证 guest
不降级兼容，需用原版排空旧实例后重建模板并启用新目录，不能只改 schema 数字。
Firecracker 和独立开发 QEMU primitive 的旧传输保持不变。

同一 Linux Node 上重新通过统一 HTTPS API 验收三条实际路径，并完成 **22 项**闭环检查：
错误 VM 密钥在两个端口被拒绝、控制证明不能用于 native、旧挑战不能重放、两个端口均
拒绝旧裸协议；合法控制、原生流式输出、任务内无 bootstrap 设备、秘密不出现在 argv/
描述符/控制台/VMM 日志均通过。随后 Edge 重启接管同一 VM/容器及动作证据，故障后
UNKNOWN 不重放、IAM/物理/CID 归零、私有盘删除均通过。五个原宿主容器 ID、PID、
启动时间和网络保持一致；本轮 API/网关/Edge 与专用单成员 etcd 已停止，无 GPU 工作。
这是单宿主验收，不新增控制面 HA 或完整论文性能结论。

干净源码完整回归 **561 项：552 通过、9 跳过**；Linux 安装版关键回归 **86 项全过**。
56 组有限模型及要求的反例全部通过，安全守卫源码变异由 5 项增加为 **8 项**并全部被
对应回归检出；FnCall 原有 3 项负向变异仍通过。认证模型将 HMAC 抽象为不可伪造的
绑定证明，不是密码学、随机数质量或完整运行时精化证明。

握手还采用两秒绝对总时限，逐字节发送不能刷新预算；确定性时钟测试验证该边界。

Linux 安装版回归首轮误列了一个不存在的测试模块，另有一次 sleep fixture 的身份读取
不一致，未进入预期的错误 argv 分支。保留首轮失败记录，改正测试清单并让测试子进程
显式应答就绪后再检查身份；运行时 pidfd/UID/exe/argv 守卫未放宽。此前上轮控制台建立前
的未定位 RuntimeError 仍不计作已修复问题。

原始证据：实验机 `/home/xiaoxiaohu/de25/api-auth-r2/`；非敏感报告本地副本位于
`.runtime/guest-auth-20261025/linux-final/`。模板、实例密钥和 API 凭据仍保留在宿主私有
目录，未拷入代码仓库或公开证据包。完整生产隔离、外连策略、Container 内存卸载和
QEMU 共享镜像仍独立按 roadmap 验收。


## 2026-10-25：QEMU 宿主隔离与无外网三路径实机验收通过

已实现 opt-in `host_isolation`：每 VMM 独立 cgroup、带提交/认领门控的普通账户 exec、
每实例 AppArmor broker，以及重启时的内核状态核验。运行时不使用线程内 `preexec_fn`，
不在 QEMU 执行后补挂资源限制，不以配置字段冒充实际 enforcement。1024 MiB guest
配置 256 MiB VMM 余量时，Node/IAM 最小准入为 1280 MiB，cgroup hard limit 同为
1280 MiB。没有启用该字段的旧开发预览仍明确返回无宿主隔离，生产标志仍为 false。

实验机专用委派用户服务完成 3 项真实内核检查：48 MiB 子进程组的 OOM kill、pids 上限
拒绝额外 fork、CPU quota 对忙循环限速。三组均为空后删除，专用服务正常退出；未启动
VM 或 GPU，不调用宿主 Docker，不改原有服务、网络、模块或其他 cgroup。该检查不等于
QEMU 已在 AppArmor 下通过运行验收。策略文本另通过 `apparmor_parser -Q -K` 离线解析。

完整干净源码回归 **576 项：567 通过、9 跳过**；Linux 新 venv 中的已安装 wheel
关键回归 **101 项全部通过**。61 组有限模型/指定反例全部通过；新增 3 项宿主隔离源码
变异被对应受控回归捕获，原有 8 项认证/轮换守卫变异仍通过。受控测试覆盖：认领后的
进程未死不能卸载策略；退役标记先于卸载、拒绝迟到认领；pidfd 尚活时不可把缺少 proc
细节视为死亡；提交失败的真实子进程拿不到 exec 权限；cgroup 不匹配与清理失败不返回
成功。有限模型和变异检查不构成无限状态或整个运行时的自动精化证明。

候选 wheel SHA-256：
`b3684e687bfa617cdfcdf6824a0bb87ceac7bc1d2bf7f532ac149729a38417c2`。
固定到本轮实例/UID/二进制及启动脚本摘要的管理员包位于实验机
`/home/xiaoxiaohu/dqh25/privileges-r3/`，只安装 helper、配置和 sudoers 三项目标，不启动
服务。安装脚本 SHA-256：
`a7455524975f758c7ea787375ca0c6d3b018dd66104f72b41cd823aea425dcfb`。
管理员已完成安装并核验三项目标的所有者与摘要。初次安装后的两轮验收失败，
修正与最终成功结果见下文。安装需要管理员，是因为 AppArmor
策略加载由内核管理，而当前 SSH 没有 sudo 认证；安装之后普通账户仅能调用固定实例 broker。

首次安装包生成揭示了标准 `user@UID.service` 路径字符校验问题，已修复并增加配置回归；
生成器回归也纠正了复用管理员预检时对 network_required 的替换，避免无关网络权限要求。
这些失败记录保留，未执行有问题的管理员包。最新原始证据在本地
`.runtime/qemu-host-20261025/`，Linux controller 报告位于实验机
`/home/xiaoxiaohu/de25/host-cgroup-kernel.json`，安装回归在
`/home/xiaoxiaohu/dqh25/installed-unit-final.log`。

安装后两轮真实 API 验收均在首个 FnCall 的 guest 启动前拒绝：第一轮报告
`VMM privilege/seccomp state mismatch`，暴露了 exec 后立即校验 seccomp 的时序问题。
现将完整核验移至 QMP/KVM 就绪后、`cont` 之前，并保留各内核字段的诊断数值；
不能据此推断第一轮所有字段的具体值。第二轮的 `vmm.log` 明确为
`Failed to create Unix socket: Permission denied`。原规则
`deny unix addr="@**"` 连同地址无关的创建操作一起拒绝。修正后只允许 stream 创建、
同 profile 的匿名内部通信和原有两条精确 pathname socket 权限，抽象 bind/connect
保持默认拒绝；要求内核具备 fine-grained AF_UNIX mediation，并去除不必要的
`attach_disconnected` 开发标志。

修正版源码回归仍为 **576 项：567 通过、9 跳过**，Linux 已安装版关键回归
**101 项全部通过**，3 项宿主守卫变异检查通过，
新策略通过 Linux `apparmor_parser -Q -K` 离线解析。隔离更新脚本通过首次替换、
幂等重跑、拒绝活跃记录、拒绝未知原版本 4 项模拟维护检查；只备份/替换固定 helper，
不修改配置或 sudoers。第二次失败后确认专用 cgroup 已移除、CID 租约为 0；两次运行
的原有五个宿主容器 ID/PID/启动时间及网络均未改变。旧失败证据保留在实验机
`/home/xiaoxiaohu/dqh25/api-r1/` 和 `api-r2/`，不计入成功验收。

修正版 wheel SHA-256：
`db67bfbad21b725d5614e126e1eef9bd2a7c23b7ca7ab06c3741c1858e921fe8`。
精确更新脚本 `/home/xiaoxiaohu/dqh25/update_profile.py` SHA-256：
`b7bc26adec007e71d2571044cd7a635ea159884eb6d0988e2bcf17b972bc88b3`。
管理员应用更新后，重新核验 root helper 的所有者、不可写权限和摘要
`defa8939ba2979bbd70511864b18b7dd6e869363375f8c4e122a36ed956716fe`，
使用上述修正版 wheel 通过完整实机验收：

- 统一 HTTPS API → IAM/Placement → mTLS Node → Edge 路径：**7 项 SDK 检查全部通过**。
  FnCall 在真实 QEMU/KVM 内清理复用、请求去重和超时后继续调用；Container 持久会话、
  文件读写、两个 VM 的独立 CID/工作目录；Firecracker MicroVM 的 shell/native/pause/resume。
- **24 项编排检查全部通过**，含内核实际 enforcing profile、独立 cgroup 限额、
  NoNewPrivs/seccomp/零有效与许可能力核验；AppArmor 拒绝读取实例外宿主私有文件；
  两端口错误密钥、跨端口 proof、重放与裸协议拒绝；正常认证流式执行。
- Edge 重启接管相同 QEMU/内层容器，已提交命令不重放，停止后仍可查询证据。
  VM 被杀时保留 UNKNOWN、命令及原因，退役后仍可查询；仅声明内层内存的请求在
  创建 VMM/物理租约前拒绝。
- 共 6 个 QEMU 实例均正常退役或完成故障回收，逐实例再次调用受限 broker 确认
  RETIRED 且无已加载策略。CID、物理租约、IAM concurrency 和私有可写磁盘均归零，
  专用委派 cgroup 已移除；无清理错误。原有五个宿主容器 ID、PID、启动时间和网络不变。

本轮 systemd 服务墙钟约 **67 秒**；这包含启动测试控制面、全部检查与回收，
不是单 VM 创建延迟。服务汇总内存值也不作为 VM 内存占用结论。
原始报告：实验机 `/home/xiaoxiaohu/dqh25/api-r3/report.json` 和 `sdk-report.json`；
本地证据副本位于 `.runtime/qemu-host-20261025/linux/api-r3-report.json` 与
`api-r3-sdk-report.json`。凭据、每实例密钥及模板未拷入仓库。

**本轮关闭的是 opt-in、无外网 QEMU/FnCall/Container 的宿主执行与回收验收。**
`production` 仍为 false；QEMU 外连、连接洪泛、多租户对抗、共享镜像、Container 内存卸载
及其他后端的全面生产隔离仍是独立待验收项，不能从本轮结果外推。

## 2026-10-25：QEMU 独立网络完成有限内核与联网验收

本轮为可选 `network=isolated` 环境补充独立 netns/TAP/veth、一次性 FD 交付、固定 TC eBPF
对象、guest IPv4 初始化和网络退役。QEMU 仍由原门控普通账户 launcher 执行，不增加
root VMM launcher；控制/native 继续使用认证 vsock。旧无网络 catalog 与模板无需改动。
root helper 只接收固定 SID/launcher 身份，不接收命令、宿主路径、程序 ID 或替代 BPF 对象。
IPv4 TCP/UDP 公网之外只允许固定 DNS 与可选 TCP 代理例外；不宣称 IPv6、ICMP、热更新、
带宽整形或抗洪泛已完成。

干净源码 **594 项：585 通过、9 跳过**；实验机新 venv 的已安装 wheel 关键回归
**119 项全部通过**。66 组有限模型/指定反例全部通过，其中新增网络模型的正常路径
与 4 类反例均匹配预期。宿主/网络守卫共 **8 项源码变异** 被对应受控回归捕获，包含
命令不继承锁的反例。测试真实杀死 wrapper，证明未结束的固定子命令仍持有文件描述符锁。
数据包测试执行的是 TC 同一份 C predicate，覆盖公网/DNS/代理、源 MAC/IP 伪造、私网与
metadata、ARP、IPv6/VLAN、分片和截断；不是另写 Python 判定器来验证自身。

实验机缺少 Clang，已把三个发行版编译器包解包到本轮私有目录供构建使用，未安装宿主包。
eBPF ELF 对象已编译，AppArmor 文本通过离线解析，sudoers 语法通过。联网版 FnCall 和
Container 模板分别构建并固定摘要，未修改旧模板。初次私有构建命令错误使用 `python -I`
屏蔽工具脚本的 sibling import，已纠正调用方式；失败日志保留，无宿主运行容器被启动。

候选 wheel SHA-256：
`5c84b7b5b69fa597df65355f33eb2cbb2a7b42aca337008dd2d9c3b1f827ad94`。
独立 qnet 实例权限包 `/home/xiaoxiaohu/dqn25/privileges-r2/` 固定六个 root 文件、
UID 1000、wlo1 与 `10.232.8.0/30` 内两个 /31 槽；安装只创建权限/策略文件和两个
私有状态目录，不建立网络或重启服务。安装脚本 SHA-256：
`5d89f6036bdc3c6ffb86d3d6b400e1bca6a1aa2e9e05c4106b32a2a3dc681770`。
两端摘要、源文件与 root 目标 allowlist 已复核。本地候选与证据在
`.runtime/qemu-network-20261025/`，Linux 安装回归在
`/home/xiaoxiaohu/dqn25/installed-unit-r2.log`。

上述权限包已由管理员安装；六个 root 文件的所有者、权限和摘要已复核。
初次安装时真实 Linux 网络尚未验收；计划使用正常 HTTPS API/SDK、最多两台小 VM，
验证实际附着程序的固定正反数据包、FnCall/Container DNS/HTTPS、宿主/metadata/私网拒绝、
Edge 接管和完整回收，并检查原 Docker/Grafana 连续性。原生 C、离线解析、模型和
成功编译不等于内核 verifier 接受，也不能作为生产出网隔离通过的结论。

首轮正常 SDK 验收在 QEMU 启动前失败：实际 `tc -j` 对单个 BPF filter 输出标题行和
程序行，代码按行数误报存在两个程序；同时 `ip link add ... alias` 没有实际写入 host
veth 的归属标签，导致随后的严格回收被阻挡。另建一个固定 SID 复现了原始 TC 错误。
两条残留网络均保持 DOWN、未进入接口启用阶段，没有启动 guest；原五个宿主容器
ID/PID/启动时间和网络保持不变。失败报告保留在实验机 `dqn25/api-net-r1/`。

候选修复只接受一个经 ID/tag/direct-action 核验的实际程序行，拒绝额外程序或异类
filter，并在 veth 创建后显式写入和核验标签。补齐启动原始错误与清理错误的独立持久化、
未返回创建的中断恢复及同一 Edge 内 STOPPING 清理重试；清理未证明时保留租约。
干净源码回归 **599 项：590 通过、9 跳过**；Linux 已安装 wheel 的关键回归
**124 项全部通过**（92 项执行/隔离/认证与网络检查，32 项 native 并发和 CID 检查）。
8 项宿主/网络守卫变异检查通过；网络/宿主有限模型未变，本轮未重复模型遍历。
候选 wheel SHA-256：
`fb03f5c27db400d4c0b861eafddf981efb105cfd5156044ae78f5244cdcca263`。

管理员修复脚本仅替换上述网络 helper，并在固定 SID、slot、namespace inode、veth
ifindex/MAC、归属、DOWN、无地址和无进程检查通过后退役两条残留网络；其余五个 root
文件与 BPF 对象不变。脚本的身份校验通过 1 个正常快照和 10 个拒绝分支检查；
摘要 `7218e9265ae1b4d514ef72bdf9dadb4e39e035d1d436632d3bad40ddbf13a2dc`。
上述管理员修复已执行，两条网络均 RETIRED。原 API/Node/Edge 的恢复检查 **12 项通过**：
未重放创建、未启动 guest，原物理租约、CID、私有盘和 IAM concurrency 全部归零，
中断/失败证据保留，原五个宿主容器不变。恢复报告位于实验机
`dqn25/api-net-r1/recovery-r4/report.json`；本地非秘密证据副本在
`.runtime/qemu-network-20261025/linux-recovery-r4/`。

该修复报告还暴露了 TC JSON 的第二处格式差异：内核程序身份位于 `options.prog`，
原代码读取的是 `options.id/tag`。已用报告中的真实 JSON 加入回归，兼容嵌套/平铺两种
格式，拒绝同时提供两个身份、缺失身份或非 direct-action 的输出。此时固定内核数据包
诊断仍是失败，不能把成功退役解释为出网策略通过。

最新候选的干净源码 **600 项：591 通过、9 跳过**，Linux 已安装版 **125 项全通过**，
8 项守卫变异检查通过。候选 wheel SHA-256：
`64eca4870885eced542502c9e28668397e1a6e5172a2a912f483c0d12109441a`。
单文件管理员更新脚本 `dqn25/update_tc_program.py` SHA-256：
`715c750a295222cc74b22c9b427e042af96ba7bef61de7e363e14c2d30fa2208`；
模拟维护验证替换、幂等和 4 类拒绝通过，只更新固定 helper，不操作网络或修改另五个
root 文件。此单文件更新已由管理员执行并核验摘要。普通账户调用受限 broker 的下一轮
预检已通过 TC 程序身份读取，但在 `default via host_ip dev vpeer` 处被内核拒绝网关。
测试 SID `b20462d1292648d2bc51ce8caa65d36e` 已由同一 broker 完整退役，无清理错误，
未启动 QEMU。该轮报告在实验机 `dqn25/kernel-policy-r5.json`。

随后通过正常 HTTPS API 启动一台 512 MiB、无宿主网络设备的 Firecracker MicroVM，
在 guest 内复现：veth 为 DOWN 时默认路由返回 Network unreachable；启用接口后，
相同地址和网关成功。该探针的 5 项编排/回收检查通过，物理租约与 IAM 清零，原宿主
容器不变；输出在 `dqn25/api-route-r1/report.json`。修复将默认路由移到接口启用之后、
发布 READY 之前；TC 与两侧防火墙仍先于任何接口启用。原受控顺序测试补充路由晚于
peer/host link-up 的约束，**14 项网络回归通过**；管理员维护模拟替换、幂等与 4 项拒绝通过。
固定子命令失败诊断同时保存命令和 stderr，避免再次丢失具体失败阶段。
修复后的 Linux 已安装版 **125 项关键回归全通过**，8 项守卫变异检查通过，wheel
SHA-256 为 `7e4ebd8fca64b46e4aac2f73aab0e24ff7df06d0467d7c904c76a40301a651dc`。
这次只重跑受影响的本地网络测试与 Linux 关键回归，未再次遍历全部源码测试和模型。

单文件修复脚本 `dqn25/update_network_route.py` SHA-256：
`23ccc58bd1d4cca79f7934a1a175075f0bb8bf6395bcfab9a3432556f6c922e9`。
上述路由顺序修复已由管理员执行并核验。单独的临时网络完成 READY、8 类内核实际
附着程序正反数据包测试和 RETIRED；报告为实验机 `dqn25/kernel-policy-r6.json`。
不得把 guest 中的路由复现、成功退役或候选回归计为宿主实际出网隔离通过；最终
真实出网与回收结果见下文。

完整 SDK 首次重跑发现 QEMU 不接受继承 `fd=` 时同时指定 `vnet_hdr=off`；错误和
VMM 日志均已持久化，创建失败完成清理。移除该冲突参数后，又发现历史失败记录的
严格 argv 校验阻挡 Edge 启动。增加精确兼容：只有该已知旧参数、STOPPED/FAILED、
无 VMM/launcher 身份且宿主策略与网络均已退役时允许读取；活跃实例及任意其他参数
差异仍拒绝接管。没有改写历史账本或删除失败证据，未修改管理员文件。

最终候选 wheel SHA-256：
`8c81dc7df31355f8d59665daeb1a99a65f523d86737a1a00a2a01d6a73028fb6`。
干净源码 **602 项：593 通过、9 跳过**，8 项宿主/网络守卫变异检查通过；
Linux 已安装版关键回归 **127 项通过**。原有限模型未改，本次未重复 TLC 遍历。

使用普通账户、已安装 wheel、正常 HTTPS API → IAM/Placement → mTLS Node → Edge：

- **7 项 SDK 检查全部通过**：真实 QEMU/KVM FnCall 的清理复用、去重、超时后继续；
  Container 持久 shell/文件、两个独立 guest/CID；Firecracker MicroVM 的 shell/native
  和 pause/resume。最多两台 VM 同时运行，不使用 GPU。
- **29 项编排检查全部通过**：实际 netns/TAP/TC 程序身份；8 类附着程序数据包诊断；
  Container DNS/公网 HTTPS 返回 200、读取 128 字节；宿主 SSH、metadata 和私网连接
  拒绝；FnCall DNS；内核宿主隔离证明、实例外文件拒绝、认证与流式执行；Edge 重启
  接管同一个 guest/容器且已提交命令不重放；VMM 被杀保留 UNKNOWN、命令与原因。
- 最终物理租约、CID、私有可写盘和 IAM concurrency 全部归零，无清理错误。额外
  内核收尾审计覆盖 9 个历史/本轮 QEMU 实例（含 2 个旧失败、7 个成功创建）：策略
  与网络均 RETIRED，无对应接口/netns，无专用 cgroup；原五个宿主容器
  ID/PID/启动时间/网络保持不变。服务墙钟 **94.551 秒**，不是单 VM 创建延迟。

原始报告为实验机 `dqn25/api-net-r4/report.json`、`sdk-report.json` 和
`kernel-retirement-audit.json`；本地非秘密副本在 `.runtime/qemu-network-20261025/linux-final/`。
旧失败报告 `api-net-r1`～`api-net-r3` 保留，不计成功样本。密钥、guest 凭据与模板未入仓库。

**关闭的是单个已准备 Node 上 opt-in QEMU/FnCall/Container 的有限 IPv4 联网验收。**
实际策略使用固定 DNS `192.168.0.1`、无代理；未验证实际代理例外、IPv6、ICMP、
热策略、带宽整形、洪泛、多租户对抗或规模。`production` 仍为 false。


## 2026-10-26：QEMU EROFS＋virtio-pmem/DAX 文件共享

本轮关闭的是 opt-in QEMU Container/FnCall 的本地只读层共享功能验收，
不是整机内存优势、冷启动速度、真实训练或生产规模验收。使用正常 HTTPS SDK →
API/IAM/Placement → mTLS Node → Edge，独立真实 etcd；不使用 GPU。
最多两台 VM 并发，之后顺序运行一台 FnCall。每台 guest RAM 仍为 1024 MiB、
VMM cgroup 上限仍为 1280 MiB、inner 限额仍为 512 MiB，没有放宽内存或隔离限制。

发布两个未压缩、无 inline regular-file data、128 MiB 对齐的 EROFS 层，
包含 32 MiB 固定数据及上层覆盖文件。最终 Container/FnCall 模板均由开源
`tools/build_docker_guest.py` 构建，使用固定的本地 Python/Docker-tools 镜像，
指定同一组 `readonly_layers` 和 `shared_mounts`。宿主只读共享 ROM 映射原始 inode；
guest 挂载 `ro,dax=always` 并检查实际文件的 `STATX_ATTR_DAX`，随后组成 private
ext4 upper 的 OverlayFS。任务容器不能访问 pmem 设备或 guest 管理目录。

最终 **32 项编排检查通过**，包括：

- 两台 VM 的 PID/CID 独立，实际 DAX 和层身份、顺序正确。
- 同时访问全部 8192 个数据页时，两个 VMM 对基础层的映射均为 `r--s`，
  device/inode 完全相同。每台 `RSS=32836 KiB`、`PSS=16418 KiB`、
  `Shared_Clean=32836 KiB`、`Private_Dirty=0`；两台该映射的 PSS 合计
  **32836 KiB，约 32.07 MiB**。RSS 直接相加会重复计算这些共享页。
- Container 修改原文件产生私有 copy-up，新建文件与删除产生的 whiteout 均不影响另一台。
- FnCall 可读 DAX 目录，写入返回 `Read-only file system`；连续调用清除 `/work` 和
  `/tmp` 的标记，同时保持原 VMM/容器身份，最终正常停止。
- 三台 VM 的 `memory.events` 均为 `oom=0, oom_kill=0`；cgroup 内核 peak 分别为
  `1072611328`、`1070272512`、`1069010944` 字节。这是整个 VMM cgroup 的 peak，
  与上面的共享文件映射 PSS 不是同一统计边界。
- 通过正常 API 回收后，物理/IAM/CID 租约归零，私有可写盘和专用 cgroup 消失，
  共享制品摘要不变，宿主/网络退役证明完成。原五个宿主容器的 ID/PID/启动时间/网络
  不变，原网络 helper 摘要不变。

候选 core wheel SHA-256：
`1beffe47aa4d97bdc2c77966866dff4f105b28a95a0e7aacb7705b46f44b6691`。
固定 launcher 摘要仍为
`80177075f4a3823175240999ba172c391bbc82a4bad912c362672e8d07b43bc3`。
本地干净导出 **623 项：614 通过、9 平台跳过**；Linux 定向 **84 项通过**，
本地/Linux 的 8 项既有宿主/网络守卫变异检查通过。管理员更新经过 4 项模拟检查，
只替换固定实例的 AppArmor helper 和配置，新增固定 publication root；不改 sudoers、
网络或服务。每 VM 将精确层身份写入原策略账本，不授予目录通配写权限；后续新增该
根目录内的层无需重装 helper。未改有限模型，未再次运行 TLC 遍历。

原始实验机证据：`dsec-pmem-20261026/api-r3/report.json`、`final-audit.json`、
`service-r3.log`，以及各实例退役前保存的原始内核计数器。本地非秘密副本位于
`.runtime/swegym-20261026/pmem-evidence/`。两次早期验收脚本失败保留：r1 的嵌套
Python 字符串转义错误；r2 已完成共享/隔离检查，但汇总时误把公开 handle 当成物理
目录名。它们不计最终成功轮，最终 r3 从正式模板重新运行并正常退出。

服务墙钟 140.993 秒包含控制面启动、历史实例恢复校验、三次创建及 20 秒受控映射
保持期，不是 VM 创建延迟。历史实例恢复还会反复全量校验旧模板；新层的运行时
SHA-256 校验也会读取文件内容，因此本轮不能证明冷缓存按需读取或创建速度。
应复用现有 fs-verity 发布凭据后再验证冷读取，不能降低完整性要求。

该结果只证明所测共享文件的数据页没有变成两份 guest 缓存；**不表示整个 VM 只有
32 MiB 内存，也不表示整机内存下降 50%**。尚未将 SWE-Gym 的仓库/依赖迁入该路径，
原 MBPP GRPO 和 SWE-Gym 成绩仍属于各自历史文件方案。压缩数据、OCI whiteout 发布、
大规模/跨节点负载、3FS 直接 DAX、真实应用的总内存收益及对 Docker 的优势仍需单独验证。

<a id="generality-linux-20261030"></a>

## 2026-10-30：通用环境能力与版本绑定 Linux 验收

本轮验证直接 SDK 创建的环境能力契约，不运行模型或训练，也不使用 GPU。
核心候选 wheel 在既有专用测试 venv 中临时安装，使用 Python isolated mode，
公开 HTTPS API → IAM/Placement → mTLS Node → 私有 Edge → 实际 guest。
独立 etcd 使用已有镜像，host networking 但仅监听 loopback 的两个固定测试端口；
不启动私有 dockerd，不创建 Docker 网桥，不新增管理员权限或变更 root helper。
固定 AppArmor launcher 摘要保持不变，QEMU 仍使用既有 cgroup/AppArmor 隔离路径。

最终候选 core wheel SHA-256：
`83fdb51b08aff638ab5fdb47fd6247d1f8afee7c1ea7718a0d29fe6da556a84a`。
wheel 边界检查通过：214 个运行模块、225 个条目。结果：

| 检查 | 结果 |
| --- | --- |
| 真实执行编排 | 27 项全部通过，无清理错误；最多一台业务 guest 同时运行 |
| 安装版 Linux 定向回归 | 77 项通过，0 跳过、0 失败、0 错误；环境、API、集群、Container Edge、制品发布五个模块 |
| MicroVM / Container 基础能力 | 按具体环境声明创建；二进制文件往返、持久 shell 会话、stdout/stderr/result 流、重连原会话并读取原流，副作用只发生一次 |
| 不同模式与兼容 | MicroVM 暂停/恢复保留文件；FnCall 两次调用不保留私有工作文件；未声明能力的 legacy guest 带需求被 NoPlacement 拒绝，空需求仍可执行 |
| API 已准入、Node 尚未派发 | 文件屏障暂停请求，替换本轮 Edge 的固定环境 revision，再释放屏障；AdmissionDenied，Edge NOT_FOUND，IAM RELEASED |
| Node 已检查、Edge 尚未收到创建 | 仅在实际 RPC 发送前设置屏障，替换本轮 Edge，再发送原绑定；Edge 守卫拒绝，未产生创建 intent，IAM RELEASED |
| 原证明与收尾 | 版本替换后原 MicroVM/Container DONE 创建证明仍可查询，配额不重新计费；Edge 全部租约和 IAM 五项使用量归零 |
| 宿主影响 | 原五个容器 ID/PID/启动时间/网络、docker0 地址和默认路由与基线一致；测试 API/Node/Edge/etcd 已停止 |

这次修复 `ContainerRuntime` 在已证明停止的状态查询、再次停止和恢复停止结果中
遗漏 `resource_cleanup_complete=true` 的返回字段；只有后端 `prove_stopped` 成功才
报告完整回收，证明异常仍返回错误。新增真实本地 RPC 回归检查该语义。修复后本地
Container/QEMU/集群 87 项通过，集群对应检查的 14 个守卫移除对照仍按预期失败。
原 EnvironmentBinding 有界模型未修改，本轮没有重复 TLC 遍历。

实机原始报告：`/home/xiaoxiaohu/dsec-generality-20261030/r5/report.json`，
安装版回归为 `tests-r2/regressions.json` / `.log`，最终归档/恢复证明为
`post-cleanup.json`、`install-after.json`。本地非秘密副本在
`.runtime/system-generality-20261030/linux-final/`；密钥和 guest 凭据未复制入仓库。
失败轮次全部保留且不计成功检查：首轮直接改变已有环境声明，被历史实例身份守卫
拒绝；之后改为保留原环境和显式固定 catalog history、添加新环境。另两轮修正了
验收脚本的 TIME_WAIT 端口误判与 API 启动等待；随后发现并修复上述返回字段遗漏。
没有放宽历史身份、能力守卫、隔离或资源回收条件。

三份本轮已退休 QEMU 记录和七条对应生命周期日志在核对 STOPPED、进程为空、CID
为空、私有盘删除与 cgroup 回收后，单独归档到证据目录；原八份历史实例不变。
借用的专用 venv 已恢复原 wheel，逐 Python 文件核验一致，不影响原环境的后续使用。
实验机剩余磁盘约 31 GiB，本轮未清理其他数据。

边界：只证明所测 native/stream 基础能力、四次正常创建及两处确定性版本变化窗口；
不证明所有 guest 的声明真实、HTTP/session 文件扩展、跨主机 HA、3FS/ublk、pmem/DAX
性能、网络隔离或 RL 训练效果。已有有限模型、先前隔离验收与本轮真实执行证据分别保留。

## 2026-10-30：TASK-007 正式 API 首次使用路径（本地阶段通过，Node 阶段受阻）

本次交付目标是让新用户从公开源码制品完成首次正式 API 用例。复用已有生成器、
API/IAM/Node、固定回复 Miles-compatible counter 验收工具，不增加运行时协议或
重新实现恢复。TASK-002 等扩展组合仍为候选；本节不把本地阶段计作最终 Node 验收。

从 allowlist 干净导出 521 个受摘要保护的文件（归档另含一份 manifest），源码归档 SHA-256：
`10bdeb0472723a7df4f07103c87947aa5e16fa5832914aaf0c14283617eeb9fd`。
在该目录构建核心 wheel，SHA-256：
`f522aed875efe0b6d99c3a3876634381cf68dc86ce37f6932a551dc12338618a`。
新建独立 venv，离线安装，确认导入来自 site-packages 且未安装 TB2.1/MBPP/SWE-Gym。
wheel 边界通过（214 运行模块、225 条目），已安装环境可运行 API 验收工具的帮助入口。
部署生成器按文档用普通 Python 运行；运行时和单文件 API 验收可使用 isolated mode。

采用公开 `deploy/cluster/prepare.py --plan` 后生成私有部署，用缓存中的固定 Python
与 etcd 镜像，实际执行公开 Compose 构建、三个 etcd 成员启动、认证/IAM 初始化、
两个 API 副本启动。未改模板来绕过 TLS、认证或动态成员管理。两个 API 分别经正式
HTTPS SDK 验证；quorum 有限运行在本机 Docker，不声称独立宿主故障域 HA。
Python 镜像为 `python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`，
etcd 为 `quay.io/coreos/etcd@sha256:3397341272b9e0a6f44d7e3fc7c321c6efe6cbe82ce866b9b01d0c704bfc5bf3`。

发现并修正一个公开首次使用断点：生成配置启用了 `node_lifecycle`，初始化器只建
IAM/配额，主部署步骤却没有串上初始 Node 的 join/activate；该操作原先埋在后面的
维护章节。现已在主部署流程明确 join → activate，并在 Quickstart 串上已有
`verify_rollout_api.py` 的默认非 TB 固定回复用例。真实 API 的 join 返回 JOINING、
version 1、held 0；对未激活节点的创建实际返回 NoPlacement。没有为跳过该步骤改动
准入实现，也没有把它误报为系统创建故障。

本地部署/台账/导出回归 **35 项通过**，使用独立 venv 中已安装核心。
随后对更新后的干净源码归档逐文件校验 521 项摘要，并确认运行时代码与上述核心
wheel 完全一致。在独立安装环境完成以下本地回归，测试不从工作树导入运行模块：

| 范围 | 总数 | 通过 | 跳过 |
| --- | --- | --- | --- |
| 核心可移植回归 | 680 | 670 | 10 |
| TB2.1 可选应用 | 5 | 5 | 0 |
| MBPP 可选应用 | 15 | 14 | 1 |
| SWE-Gym 可选应用 | 73 | 51 | 22 |
| OpenHands 适配器 | 54 | 26 | 28 |

核心和 MBPP 跳过项涉及 Linux 专属进程、权限和 guest 条件；SWE-Gym/OpenHands
还包含本环境未安装的可选上游 SDK、官方 harness 与 PyTorch/verl 条件。这些不是
通过项，不代替真实 guest、上游完整集成或训练验收。OpenHands 首轮因测试环境
遗漏 CI 指定的 `pexpect` 而产生 9 个导入错误；补齐 `pexpect==4.9.0` 和
`ptyprocess==0.7.0` 后同一套测试通过，初始失败日志保留，未改产品依赖。

四个可选应用均从干净源码构建并通过各自 wheel 边界检查，随后以 `--no-index
--no-deps` 安装到独立环境；安装前已确认核心不自动带入这些应用。制品 SHA-256：

| 可选包 | SHA-256 |
| --- | --- |
| dsec-tb21-case | `372c669e8d5f2e9ea9fb97d168dff069befe939ad7aaa3807f5dfa44d129d4b3` |
| dsec-mbpp-case | `1747d4e63755747bef3ebaa5bf0b8e8c5778ee4d9061ace97aa79e81b4fafb31` |
| dsec-swegym-case | `479d3a96406945aaf282837699c87e02afdda4b7f2f8e75afad682583380a12d` |
| dsec-openhands | `77b032a0cd64c65ab1e81b9f592aa9fc18a783adaf1c2451c9e9d385ad989363` |

在源码目录外，16 个已安装命令的 `--help` 全部成功；此项只验证入口可加载。

实验机 `192.168.0.110` 先出现 SSH 超时，复核为 No route to host；用户随后确认
机器暂不可用、仅继续本地工作，此后未访问远端，也没有启动该 Node、VM、模型或训练。激活后 counter 评分及 Node 回收证据尚未取得；TASK-007
保持受阻，不能关闭或声称已完成从零全流程，也不能代替第二台新 Linux 宿主验收。

停栈前真实 API 证明 root 项目五项使用量全为 0，admissions 为空，JOINING Node
held 为 0。本轮两个 API、三个 etcd 容器及专用网络正常停止/回收；保留专用私有
配置和 etcd 卷以便恢复同一入群状态。没有修改远端服务或既有宿主容器。
原始本地证据位于 `.runtime/release-entry-20261030/`：`controller-startup.json`、
`membership-check.json`、`pre-stop-audit.json`、`compose-services.json`、`local-regressions-r2.json`、`installed-entrypoints-r2.json`
和 API/回归日志；
私有部署目录包含凭据，不进入源码或 wheel。

## 2026-11-04：TASK-008 正式 API 的无网 Linux FullVM

在实验机普通账户上安装独立核心 wheel，并从已有 Python 基础镜像离线构建
512 MiB ext4 启动盘。Docker 仅用于离线导出根文件系统与临时 loopback etcd；
FullVM 运行时不启动内层 Docker。验收经 HTTPS SDK → API/IAM/Placement →
mTLS Node → 私有 Edge → QEMU/KVM。使用先前验证的 Linux 6.1.186 内核、
现有共享 CID 池、1 vCPU/512 MiB；同时最多一台 VM，未使用 GPU 或 sudo。

- 核心 wheel SHA-256：`823817cc8947d299b773cbf80aca52e2060a4d103d1fa749ea83c9e4a74411d8`。
- 最终模板 SHA-256：`b3b333d3171e4025b6db7613971972c4c8c84aed94141788fb6e83922938e0fe`。
- API 四项验收：错误后端/固定资源修改拒绝；独立 FullVM 路由与创建收据；
  guest UID 1000 命令、原请求 ID 去重、重连及会话和二进制文件；干净新盘和停止证明。
- 两次顺序启动后，私有盘都已删除，QEMU 进程、CID、物理租约和 IAM 用量均归零。
  临时控制服务与 etcd 已停止，原五个 Docker 容器的身份、PID 和启动时间保持不变。
- 本地可移植核心回归 681 项，671 通过、10 跳过；未重跑原三条路径的实机矩阵。

构建验收发现私有 umask 会限制导出根文件系统与 Python 库权限。最终构建器保留
固定基础镜像的文件模式，明确设置新加入的 guest 文件权限；任务仍以非 root、
`NoNewPrivs` 执行。失败轮日志保留；最终原始证据在实验机
`/home/xiaoxiaohu/dvf04/f/report.json` 和 `api-report.json`。
这是无网、无显示的 Linux 执行预览。没有验收桌面、OSWorld、外网、FullVM
快照、GPU 或生产租户隔离。

## 2026-11-04：TASK-009 FullVM 2D 桌面与 OSWorld 单题

通过独立安装版走 HTTPS API → mTLS Node → Edge → QEMU/KVM。Ubuntu
22.04.5/Xfce guest 使用 Ubuntu 7.0.0-38 内核和对应 virtio 驱动、1 vCPU、
1024 MiB RAM、1024 MiB 私有盘；无网，无物理 GPU 或 3D 加速。截图与
键鼠走私有 QMP，未暴露 VNC 或 guest TCP 端口。首轮启动参数和 Ubuntu
Python 3.10 的类型导入错误均修正；失败记录保留。

- 运行 wheel SHA-256：`3408596769fbe8dc02bc21d8daa03f19d82616920829feafdb34a18d98a5b5d6`。
  guest 使用兼容 Python 3.10 的 `execution.py`，模板摘要
  `612f258c4a83b626e0753347bdfca9d6f4e76ce18621be43e44e82a7edb98f46`。
- Ubuntu/Xfce 截图为 1024×768 PNG；鼠标目标和实测均为 (140,248)；
  点击后通过 virtio 键盘在 xterm 输入命令并产生文件。相同鼠标请求 ID
  可取回原证明；本地回归另验证 QMP 丢回复时不重放。
- OSWorld 固定提交 `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`，任务
  `e0df059f-28a6-4169-924f-b9623e7184cc`，把桌面目录 Jan_1 重命名为
  Jan_2。DSec guest 用普通用户重建同样的初始目录，保留任务的验证命令
  和原版 `exact_match` 函数。固定动作在终端先输入 `cd`，再输入
  `mv Desktop/todo_list_Jan_1 Desktop/todo_list_Jan_2`。动作前 **0.0**，
  动作后 **1.0**。首跑由于终端初始 cwd 不同得到 0，截图证实按键完整到达；
  修正固定动作后用新实例重跑，未修改任务或评分规则。
- 通过轮为实验机 `~/dvd04/d/api-report.json`（桌面）和
  `~/dvd04/e/osworld-report.json`、`report.json`（单题及回收）。
  后者确认 VM、CID、私有盘、物理和 IAM 租约全部回收，原宿主容器未变。

首轮未运行官方 Docker provider 基线。官方镜像 ZIP64 目录记录显示
`Ubuntu.qcow2` 解压后为 24,460,197,888 字节，超过实验机可用空间；
压缩包另需 12,273,896,463 字节，运行时还要预留写层。仅请求了 ZIP 尾部
64 KiB，未下载整盘。这一阶段的 1.0 仅覆盖 DSec 执行和官方评分函数；后续同题对照见下。

### 2026-11-04：OSWorld 官方 Docker provider 单题对照完成

实验机清理后约有 134 GiB 可用。续跑使用相同 OSWorld 提交、任务文件
SHA-256 `a507ec6c35284c1413efe0d8a325b41a1626522221efc14f153742a865f1c04d`、
相同的两条终端输入和上游 `exact_match` 函数。官方侧沿用上游
DockerProvider 启停和原版 Ubuntu QCOW2，使用任务的官方初始化与命令结果 getter。
唯一的 provider 启动适配是将映射端口限制在 loopback 并加入回收标签。
观察到 QEMU 使用 KVM；未使用 GPU。

| Provider | 输入动作前 | 输入动作后 |
| --- | ---: | ---: |
| DSec FullVM | 0.0 | 1.0 |
| OSWorld 官方 Docker | 0.0 | 1.0 |

官方磁盘来自 Hugging Face 提交 `a5d9c3eaae98eebf6e3a0beb84e7e47cf72ae133`。
下载的 ZIP 摘要与官方 SHA-256 一致；解包后 QCOW2 SHA-256
`6bf667a852b3c307f61d9f09c42559351f45e0607e428b4997becf534cf4d313`，
官方运行镜像 `happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9`。
首轮通过截图就绪检查后，因本次验收脚本的 Docker 进程查询漏了 PID 字段而退出；
已完整回收，未评分。最终轮取得有效分数、保存前后截图并移除临时容器，
宿主现有容器 ID、PID 和启动时间不变。

原始官方证据位于实验机 `~/dvd04/official-run-r2/`。本地对照清单
`.runtime/fullvm-desktop-20261104/evidence-final/osworld-comparison.json`
记录双方分数、动作和截图摘要。只证明这一个任务在两种桌面环境下评分一致；
资源配置和桌面基底不同，本轮不比较性能，也不推断全量 OSWorld 兼容。

## 2026-11-04：TASK-010 GUI-Owl 2B 单题视觉 rollout

沿用 TASK-009 的目录重命名任务、Ubuntu 22.04.5/Xfce FullVM 和官方
`exact_match` 评分器。模型为 `mPLUG/GUI-Owl-1.5-2B-Instruct`，
版本 `528ceaec795bbfbe6103bd79e03db849feadfb24`。外部 harness 使用上游
提示和动作格式，模型观察为任务说明、1024×768 截图及历史动作。动作只走
DSec 桌面 API。初始化和评分在 harness 侧执行，评分结果不进入模型上下文。

- 官方得分 **0.0**。模型在 16 步内交替生成 8 次右键和 8 次 Delete。
  第一次右键目标落在目录图标内。随后的截图未显示菜单，模型继续按
  Delete，且始终没有发起重命名或输入新名字。动作解析和 SDK 调用均成功，
  右键菜单未出现在截图中的原因仍待确认。
- RTX 4080 上用 Transformers BF16 贪心解码，每次最多生成 256 token。
  16 次推理合计 12.33 秒，rollout runner 用时 28.88 秒，含模型加载、
  VM 启停和评分。PyTorch 峰值分配 4626 MiB，峰值预留 4778 MiB。
- 首轮误将截图路径以 `file://` URI 传给当前 Transformers，模型加载后
  在读图时退出，无动作和得分。修正路径后用新 VM 按同一配置重跑。
- 完成轮退出后 GPU 回到 35 MiB 基线，IAM、物理租约和 CID 均归零，
  私有盘已回收，宿主原有容器身份、PID 和启动时间一致。

实验机 `~/dvd04/owl2-r2/` 保存 `osworld-report.json`、`report.json`、
`trace.jsonl` 和每步截图。首轮失败日志在 `~/dvd04/owl2-r1/inference.log`。
本轮验收覆盖单条可评分轨迹和资源回收。模型任务本身失败，未重跑官方 provider。

## 2026-11-04：TASK-011 修正鼠标点击并补齐桌面文件管理服务

在同一个临时 FullVM 上对照了三种右键输入。鼠标实测到达 (76,322)，
位于目标图标内。同一 API 请求中的按下和松开没有弹出菜单。拆成两个
请求、相隔约 80 ms 后，右键菜单可见，guest 内 `xdotool click 3`
正控也打开了同样的菜单。外部 harness 随后把鼠标移动、按下、松开
分别提交，保留每个请求的独立 ID。原始对照在 `~/dvd04/owl-rightclick-r1/`。

| 条件 | 官方评分 | 观察到的行为 |
| --- | ---: | --- |
| 原 harness、原桌面 | 0.0 | 右键后看不到菜单，模型交替右键和 Delete |
| 拆分点击、原桌面 | 0.0 | 第 2 步点击 Rename，Xfce 报缺少文件管理服务 |
| 拆分点击、补齐 Thunar | 1.0 | 第 4 步改名成功，模型继续操作至第 16 步上限 |

修正轮继续使用原核心 wheel、模型版本、官方任务、截图尺寸、
贪心解码与 16 步上限。Thunar `4.16.10-1` 安装在独立的 guest
构建镜像内。新模板 SHA-256 为
`f08c91676caa170eb41cd8bfba178920ae0ed991e483a735a3d05c10a14f8db2`。
第 5 张截图确认新目录名。最终官方评分命令确认新目录存在、旧目录不存在。
最终轮 runner 用时 33.20 秒，其中模型推理共 13.11 秒。模型未输出
`terminate`，成功完成任务后继续操作的问题可从原始轨迹复核。

原桌面重跑和最终轮分别在 `~/dvd04/owl2-r3/` 与 `~/dvd04/owl2-r4/`。
三次验收均归还 VM、CID、IAM 和物理租约，回收私有盘，原宿主容器未变。
最终 GPU 回到 35 MiB 基线。验收范围为单题视觉轨迹。

## 2026-11-04：TASK-012 GUI-Owl 停止行为复测

使用 TASK-011 的固定模型、原上游 prompt、贪心解码、16 步上限、
修正后的鼠标事件和 Thunar 模板。依次启动四台全新 FullVM，每台一条轨迹。
这是同配置实机重跑，未改变解码随机性或做 prompt 对照。每条的第 5 张
截图均显示第 4 步已完成改名。官方 verifier 仍只在整条轨迹结束后运行。

| 有效轨迹 | 第 4 步完成改名 | 主动终止 | 停止步数 | 官方最终得分 |
| --- | --- | --- | ---: | ---: |
| `owl-stop-r01` | 是 | 否 | 16 | 0.0 |
| `osrp02` | 是 | 否 | 16 | 1.0 |
| `osrp03` | 是 | 否 | 16 | 1.0 |
| `osrp04` | 是 | 否 | 16 | 1.0 |

四条轨迹的第 5 步回复都提及正确的新目录名，仍选择继续操作。首条在
第 15 步输入 `todo_list_Jan_3`，第 16 步确认，最终截图验证了这一改动。
另一次尝试 `owl-stop-r02` 在 VM 启动阶段失败，未产生模型动作；
日志未给出具体启动原因，已确认 CID、VM 和私有盘退役，该尝试不计入样本。
四条有效轨迹均回收临时资源，GPU 回到 35 MiB，原宿主容器未变。
本地证据在 `.runtime/fullvm-desktop-20261104/evidence-owl-stop-repeat/`，
实验机目录名见表。

## 2026-11-10：TASK-013 QEMU 逐步差分辅助 CUA harness 修复

沿用 TASK-011 的 QEMU/KVM、Thunar FullVM 模板、同一 OSWorld 任务与官方
`exact_match` 评分器。各用一台新 VM 重放同一右键。原实现与参考实现的
API 回执均成功且指针同位，首次分歧为右键后是否显示 XFCE 菜单。原实现
得 0 分，参考实现得 1 分并通过在不同位置重命名另一个目录的回归。

两组使用同一离线 Qwen2.5-3B-Instruct、CPU 8 线程、temperature 0.6、
top-p 0.9、96 个生成 token 上限和最多 3 轮。四个配对随机种子，组顺序
AB/BA/AB/BA。两组均得到源码和两条原始事件及窗口观测，B 组额外得到
自动对齐的首次分歧描述。截图留存审计，文本模型读取截图对应的 X11
窗口观测。候选限定于鼠标事件分组和间隔，以 JSON 表达并记录成对应源码
改动。每个候选在新 VM 实测，评分和留出回归由外部固定执行器控制。

| 组 | 3 轮内修复 | 首次合格轮次 | 候选 VM 数 | 单会话耗时中位数 |
| --- | ---: | --- | ---: | ---: |
| A 原始证据 | 4/4 | 2, 2, 2, 2 | 8 | 26.22 s |
| B 原始证据加对齐报告 | 4/4 | 2, 2, 2, 2 | 8 | 26.56 s |

八个会话第一轮都把 down/up 放在同一批，第二轮都将两者拆开。复核原始
结果时发现 B4 两轮都使用 0 ms 间隔，其余会话两轮均使用 80 ms。
所有合格候选均通过官方评分和留出回归。耗时包括每轮 CPU 推理与
实机评测，提取差分约 12 微秒，双方共享控制轨迹的生成成本约 12.77 秒。
记录的是单次实机试验，微小耗时差异不据此作性能判断。

在这个单故障和窄编辑空间中，额外的对齐报告未减少尝试次数。两组已
能从原始日志直接看见批次区别，报告增加的是定位提示。该对照不能
外推到多步、长轨迹、多故障或开放源码编辑。实验机 `~/dvd04/cdiff1/`
保留逐轮报告与截图。18 台 VM 的私有盘已回收，IAM、物理及 CID
租约均归零，提案进程已退出，宿主已有容器保持原状态。

## 2026-11-10：TASK-014 CUA 差分修复 RTX 4080 复测

在修复 NVIDIA 驱动后，沿用 TASK-013 的模型、FP32、输入格式、四个
配对种子、运行顺序、候选上限和固定 VM 评测，将推理移至 RTX 4080。
CUDA 关闭 matmul TF32，峰值分配 11954.2 MiB。两组同样读窗口观测，
原始截图留作证据。CPU 与 CUDA 的采样序列不保证逐 token 相同。

| 组 | 修复成功 | 首次合格轮次 | 每会话模型推理中位数 | 每会话总耗时中位数 |
| --- | ---: | --- | ---: | ---: |
| A 原始证据 | 4/4 | 2, 2, 2, 2 | 1.24 s | 13.81 s |
| B 加差分报告 | 4/4 | 2, 2, 2, 2 | 1.26 s | 13.88 s |

合格候选均拆分 down/up 并保留 80 ms，官方评分与留出回归通过。
GPU 轮显著缩短了模型推理时间，本轮依旧未观察到差分报告减少轮数。
耗时口径为每会话各轮的推理与 VM 验证之和，公共模型加载和对照准备
在会话计时之外。

一次 VM 创建失败且 SDK 证明清理，按实验脚本策略重试后成功。
18 台评测 VM 和 1 台启动失败 VM 的私有盘均退役，物理及 IAM 租约、
CID 归零，宿主容器未变。提案进程退出后 GPU 回到 1 MiB。
证据位于实验机 `~/dvd04/cdiffg1/`。

## 2026-11-10：TASK-015 两题 FullVM Calc 回放与故障定位

使用固定 OSWorld 提交 `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`、
原版 `compare_table` 和固定工作簿摘要。Calc 7.3.7 安装在专用 Ubuntu/Xfce
模板，镜像 SHA-256
`92fed010ba910cbc218b661c0ca23e067d4bdb10d5f5e0e386db1219504f58db`。
工作簿经正式 SDK 原生文件通道传入、读回，应用启动和窗口探针只用于
准备与观测，任务操作全部经公开桌面输入 API。回放明确选择折线图的
Lines Only 子类型。XLSX 保存动作走虚拟键盘并经 GUI 确认，原版评分
规则在宿主隔离评分环境读取结果。

| 回放条件 | 次数 | 官方得分 | 首次分歧 |
| --- | ---: | ---: | --- |
| BoomerangSales 健康，24 步 | 2 | 1.0 | 无 |
| WeeklySales 健康，16 步 | 2 | 1.0 | 留出任务评分 |
| 拖拽提前释放 | 2 | 1.0 | 第 7 步，选区 |
| 组合键缺少 Shift | 2 | 0.0 | 第 3 步，选区 |
| 图表向导返回旧帧 | 2 | 1.0 | 第 17 步，向导画面 |

原始截图与请求、回执通过动作序号关联。逐步差分对三个预设语义检查点
比较窗口名与裁剪区域，避免全屏时钟和光标噪声。拖拽和旧帧故障的
官方分数仍为 1.0，原因分别是后续重设选区，以及固定动作可在实际
guest 状态正确时继续执行。两者均被独立检查点拒绝。该验收限定于
固定 UI 和已声明检查点。

首个固定矩阵遇到 Calc 未及时就绪，补充 Xfce 与 Calc 窗口就绪判断
后重新运行。通过轮十台 FullVM 的私有盘、CID、物理和 IAM 租约
全部回收，宿主原有容器未变。原始报告在 `~/dvd04/cmat2/`。
复用入口见 [OSWorld 桌面回归](../../apps/osworld/README.md)。

### 2026-11-10：TASK-015 长轨迹修复对照

健康与故障矩阵通过后，使用 Qwen2.5-3B-Instruct、RTX 4080 和固定
BF16 设置运行六对会话。首个 FP32 尝试在长输入预填充时显存不足，
未生成候选，保留在 `~/dvd04/cab1/`。BF16 峰值分配 6559.24 MiB，
两组使用相同温度 0.6、top-p 0.9、96 个生成 token 上限、每会话三轮
以及相同配对种子。A 和 B 都读取动作序列、回执、窗口和各检查点的
截图区域摘要。只有 B 获得自动对齐的首差异报告。提案范围限制为
外部 harness 中的一个输入或观测配置项，候选验收同时检查首差异消失、
主任务官方得分和留出题官方得分。

| 故障 | A 首次合格轮次 | B 首次合格轮次 |
| --- | --- | --- |
| 拖拽 | 1、未通过 | 未通过、未通过 |
| 组合键 | 未通过、3 | 2、3 |
| 旧帧 | 1、1 | 1、1 |

三轮内双方均修好 4/6。B 相对 A 一对更早，一对更晚，四对打平。
A 共 12 轮、13 次有效 VM 回放，每会话耗时中位数 57.58 秒；B
共 13 轮、16 次有效 VM 回放，每会话耗时中位数 88.22 秒。
会话计时包括每轮推理、实际回放验证和 B 组的差分生成，固定矩阵和
共同模型加载在会话计时之外。首轮报对步骤 A 3/6、B 6/6，
但 B 直接拿到步骤编号，因此单独统计首轮选对代码配置项，A 3/6、
B 2/6。该小样本没有观察到整体修复加速，结果限于合成故障、预设
检查点和有界配置补丁。

完整报告位于实验机 `~/dvd04/cab2/`。该轮共退役 29 份私有盘，
物理、IAM、CID 租约全部归零，宿主已有容器保持一致，模型退出后
GPU 回到 1 MiB。回放、评分与差分脚本已纳入源码发行清单，任务输入、
参考工作簿、模型与运行证据独立保存。

## 2026-11-10：TASK-016 FullVM 输入与帧来源差分

沿用 TASK-015 的两道 OSWorld 题、固定镜像、SDK 与动作序列。
`compare_trace.py --diagnose` 分开报告首次提交事件差异、按钮末态
差异、首次帧来源差异和首次可见检查点差异。按钮末态根据 SDK 已确认的
事件推导，未用于推断 guest 实际收到的事件。截图记录两个捕获 ID、
本机单调时钟返回时点和内容摘要。若 harness 交付的是动作前的帧，
实际捕获的动作后 PNG 另存。旧轨迹仍可比较输入和可见状态，帧来源
标为不可用。

两道题各两次健康回放均获官方 1.0。三个故障各两次，增强报告
6/6 给出正确步骤与边界：

| 故障 | 步骤 | 增强证据 |
| --- | ---: | --- |
| 拖拽提前释放 | 7 | 移到终点与左键释放的顺序互换，双方帧均来自动作后 |
| 组合键丢 Shift | 3 | 同一组合键意图，SDK 接受 6 个对 4 个事件，缺少 Shift 成对事件 |
| 图表向导旧帧 | 17 | 输入事件顺序一致，参考返回后帧，故障侧返回前帧并独立留存后帧 |

复验 TASK-015 的错误拖拽补丁时，旧可见比较没有分歧，增强报告
指出第 7–9 步仍有左键处于按下状态。健康和故障矩阵保存在实验机
`~/dvd04/cdm1/`，十台 FullVM 的私有盘和租约全部回收。

### 2026-11-10：TASK-016 增强证据 A/B 复测

沿用原题、三类故障、Qwen2.5-3B-Instruct BF16、配对种子、组内
执行顺序和三轮预算。两组共同看到带坐标、事件回执和帧捕获 ID 的原始
轨迹，并在每次实机候选验证后收到同一增强诊断。B 组独有首轮提案前
的对齐报告。新增按钮末态和帧来源检查适用于两组。与 TASK-015 的
跨轮比较同时存在原始证据、反馈和门槛变动，本轮组内配对用于判断
提前提供报告的增量。

| 故障 | A 首次合格轮次 | B 首次合格轮次 |
| --- | --- | --- |
| 拖拽 | 2、未通过 | 2、未通过 |
| 组合键 | 3、未通过 | 3、3 |
| 旧帧 | 1、未通过 | 未通过、未通过 |

两组均修好 3/6。B 一对更早，一对更晚，四对打平。A 共 15 轮、
13 次 VM 回放，每会话耗时中位数 63.31 秒；B 共 17 轮、14 次
VM 回放，每会话耗时中位数 85.53 秒。首轮选中正确配置项均为 2/6，
B 的两次均是旧帧题，且都把故障值 `before` 原样提交，因此在评测前
被拒绝。首轮报对步骤为 A 2/6、B 5/6，B 组预先获得步骤编号。

两组各有一条拖拽轨迹首轮改错帧来源，验证报告同时显示第 7 步的
原始输入差异和第 17 步新增的前帧，下一轮均改对事件顺序。组合键
出现同类回归，报告保留第 3 步原差异和第 17 步新增帧差异。
这些结果证明验证反馈能区分原故障和新回归。在此模型、受控故障和
小样本下，额外的首轮报告未减少修复轮数或耗时。

实机报告在 `~/dvd04/cda1/`。共 27 台候选评测 VM 完成退役，
物理、CID、IAM 租约均归零，宿主已有容器未变，GPU 回到 1 MiB。

## 2026-11-10：TASK-017 自主视觉 CUA 健康基线

本轮重新从健康桌面建立基线。此前固定动作的数据只用于 FullVM
回归，不作为自主 CUA 的故障或修复对照。八条轨迹每条使用全新
FullVM，模型读取任务指令和实时 PNG，自行选择每步键鼠动作。
真实动作通过 HTTPS SDK 执行。任务结束后才运行固定 OSWorld
任务所需的保存和上游 `compare_table` 评分。保存不到目标文件时仍
按实际导出的工作簿评分，并保留 postconfig 的窗口激活状态。

| 校准 | BoomerangSales | WeeklySales |
| --- | ---: | ---: |
| GUI-Owl-1.5-2B-Instruct，48 步 | 0.0 | 0.0 |
| Qwen3.5-4B 视觉 non-thinking，48 步 | 0.0 | 0.0 |
| GUI-Owl-1.5-2B-Instruct，预配置桌面，56 步 | 0.0 | 0.0 |
| GUI-Owl-1.5-4B-Instruct，预配置桌面，56 步 | 0.0 | 0.0 |

预配置只关闭 Calc 首次提示和 XLSX 格式确认，给模型通用的
避免重复、保存和结束提示，保留最近两帧历史。这些是独立校准轮，
不把不同模型和配置的结果作为单变量对照。4B 制品固定到
`3f061c2c562cc860c42bf32542a70e07a7ff4840`，两片权重完成整文件
SHA-256 校验，GPU 峰值分配约 9694 MiB。两条预配置 WeeklySales
轨迹的工作簿确有变化，仍未把要求的图表留在 Sheet2；4B 主任务
留在图表向导的数据系列页，未完成范围输入。八条轨迹均正常进入
官方评分，无工具解析失败，均触及步数上限。

健康基线的满分准入条件未达到，故障和修复 A/B 留待有可用的自主
基线后进行。证据位于实验机 `~/dvd04/{clive1,cqv1,cop2,cop4}/`。
八台 FullVM、私有盘、CID、物理及 IAM 租约均已回收，现有宿主容器
保持不变，GPU 回到 1 MiB。

## 2026-11-10：TASK-018 GUI-Owl 4B 自主扫描 OSWorld

在 RTX 4080 和 Ubuntu 22.04/Xfce、Calc 7.3.7 的 DSec FullVM 上，
固定 GUI-Owl-1.5-4B-Instruct `3f061c2c562cc860c42bf32542a70e07a7ff4840`，
使用 BF16、贪心生成和最近两帧历史。OSWorld 固定为
`8903ebeb0efdf74331bd7dc37ed91ccbbff22317`。按镜像现有应用选 8 道
题，每题在新 VM 上最多 32 个模型决策。模型读实时桌面画面和原题
指令，经正式 SDK 发出键鼠事件。成功题在另一台新 VM 上自主复跑。
上游评分函数和规则保持原样，来宾路径改为 `/work`，Calc 沿用
首次提示和格式确认的预配置及官方评分前的保存动作。

| 任务 ID 前缀 | 任务 | 首跑得分 | 模型步数 | 复跑 |
| --- | --- | ---: | ---: | ---: |
| `e0df059f` | 桌面目录改名 | 1.0 | 5 | 1.0（5 步） |
| `51b11269` | 按金额升序排序 | 1.0 | 3 | 1.0（3 步） |
| `1334ca3e` | 缩小工作表显示 | 0.0 | 7 | — |
| `4188d3a4` | 冻结表头范围 | 0.0 | 11 | — |
| `1273e544` | 复制 Revenue 列到新表 | 0.0 | 10 | — |
| `1e8df695` | 添加 Profit 列及公式 | 0.0 | 32 | — |
| `0cecd4f3` | 复制和重命名工作表 | 0.0 | 24 | — |
| `5ea617a3` | 从回收站恢复海报 | — | 0 | — |

七道有效评分题均从 0 分开始。两道满分题分别由模型完成右键改名，
以及通过 Calc 的 Data 菜单选择 Sort Ascending。四道零分题由
相同动作和相同帧的停滞守卫结束，Profit 题反复点击同一个位置但
截图哈希变化，触及步数上限。回收站题因镜像缺少 `gio` 在初始化
阶段退出。两次首次启动失败均清理完成并重试成功。

实验机证据在 `~/dvd04/scan1/`，含官方评分、原始回复、输入事件、
截图与终止原因。共 10 条任务运行、12 块私有盘退役（含两次启动
重试），IAM、物理和 CID 租约归零，宿主现有容器保持不变，GPU
回到 1 MiB。这是主动挑选的小样本健康基线，与 TASK-017 原题或
故障修复效果分开统计。

## 2026-11-11 TASK-019 GUI-Owl 4B 扫描 OSWorld 360 题

使用 OSWorld `8903ebeb0efdf74331bd7dc37ed91ccbbff22317` 的
`test_nogdrive.json`，覆盖十类应用、360 个唯一任务。模型沿用
TASK-018 的 GUI-Owl-1.5-4B-Instruct `3f061c2c562cc860c42bf32542a70e07a7ff4840`、
BF16、贪心生成和最近两帧历史。每题最多 32 次模型决策。
为覆盖桌面应用，使用 OSWorld 官方 Ubuntu 镜像的独立 QEMU/KVM
VM，1920×1080，4 vCPU 和 4 GiB 内存。此轮结果与 TASK-018 的
DSec FullVM 成绩分开记录。guest 无公网出口，622 份官方输入和
评分素材从宿主缓存提供，使用原版初始化和评分规则。

| 结果 | 题数 |
| --- | ---: |
| 首跑、全新 VM 复跑均 1.0，且无操作对照低于 1.0 | 73 |
| 首跑 1.0，复跑低于 1.0 | 18 |
| 首跑和复跑 1.0，无操作对照也为 1.0 | 1 |
| 首跑部分得分 | 6 |
| 首跑官方 0.0 | 192 |
| 当前隔离配置缺少代理或任务所需外网 | 64 |
| 初始化失败 | 3 |
| 输入链路异常 | 1 |
| 评分异常 | 2 |

92 道首跑满分题均已另建 VM 复跑，复跑满分后再另建 VM 做无操作
对照。73 道稳定满分覆盖全部十类应用，其中 Impress 17 道、OS 11 道、
Thunderbird 和 VS Code 各 7 道。新增例子包括 Calc 数据透视表、
GIMP 居中黄色三角形和从回收站恢复海报。Spotify 的原始试跑
虽然返回 0 分，但因缺少在线安装源，归为环境受限并保留原记录。
一轮独立异常复核后，仍有 6 道无有效评分，具体包含 3 道初始化异常、
1 道 X11 输入断开、2 道评分后处理 HTTP 500。

实验机原始报告、每步截图和输入证据位于
`~/dvd04/osw360/campaign/`。逐题清单和满分清单已对照官方任务 ID
校验。471 个 VM 运行记录全部确认回收，私有盘、QEMU 进程和活跃
episode 均归零，GPU 回到 1 MiB。

## 2026-11-11 TASK-020 DSec FullVM 启动 OSWorld 官方镜像

安装版 wheel SHA-256 为
`242efe82f37b4e5bc6ff490123b09ec1ec211cb5a09bc99997c789692573306a`。
OSWorld 官方 Ubuntu QCOW2 作为只读基盘，独立增量层仅负责准备
DSec guest agent 的启动环境。每次 SDK 创建都分配私有 QCOW2 写入层
和 UEFI 变量文件。两次验收后，官方基盘和 guest agent 层的
SHA-256 均与各自的启动前记录一致。

通过正式 HTTPS SDK、API、Node 和 Edge 启动两台全新 KVM VM。来宾
为 Ubuntu GNOME，1920×1080，4 vCPU 和 4 GiB RAM，无外网。SDK
认证后以 UID 1000 运行命令，确认 UEFI 已启动，上传并读回官方素材，
并执行原题的 `gio trash` 初始化。来宾端原路径及官方 `exact_match`
评分函数沿用 OSWorld 固定版本 `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`。

| 回收站恢复海报 `5ea617a3` | 初始评分 | 最终评分 | 模型步数 |
| --- | ---: | ---: | ---: |
| GUI-Owl-1.5-4B-Instruct 首跑 | 0.0 | 1.0 | 32 |
| 全新 FullVM 复跑 | 0.0 | 1.0 | 32 |

两条轨迹均由模型看图并通过 SDK 发出鼠标动作。恢复后的海报经 SDK
读回，SHA-256 与原始素材一致。两条轨迹都达到模型步数上限。验收中
根据 systemd 日志修复了固件启动期间过早发放认证密钥、原生 Unix
socket 目录的属主问题，并让评测适配器处理 1920×1080 以及通用
`click` 动作。

139 项 Linux 回归和 17 项 Node 预检回归通过。两台正式验收 VM 的
写入层和 UEFI 变量文件均已删除，CID、物理和 IAM 租约均归零。
逐步截图、原始模型输出、SDK 回执、官方评分和回收证明位于实验机
`~/dvd04/uf6/`。

## 2026-11-11 TASK-021 UEFI FullVM 宿主隔离

安装版 wheel SHA-256 为
`a2208af545d19047c60f68017ed04faeb226f6f8e2ae53bccfab1bf0bd6865e9`。
复用官方 Ubuntu 基盘与 guest agent 层。管理员安装独立
`dsec-ufiso` broker，Edge 在普通用户委派服务内启动两台全新离线
FullVM。经正式 HTTPS SDK 运行回收站题 `5ea617a3`，两次均从
0.0 到 1.0，均达到 32 步上限，恢复海报 SHA-256 与原始素材相同。

两台 VMM 均处于各自的 AppArmor enforce profile 和独立 cgroup。
内核审计记录每台 VMM 各一次实例外读拒绝和实例外截图创建拒绝。
实机文件描述符证明 OVMF 固件、官方 QCOW2 基盘和 guest agent
层以只读方式打开，实例 QCOW2 与 UEFI 变量文件以读写方式打开。
每台 VMM 的 cgroup 限制为 4 个 CPU 配额、5 GiB RAM、swap 0、
256 个 PID。本轮委派控制器为 CPU、内存和 PID，文件访问权限由
AppArmor 执行。独立委派服务已实测 CPU 限流、内存 OOM 和 PID
拒绝，另有 141 项 Linux 回归通过。

两台 VM 停机后，AppArmor 策略、每实例 cgroup、QCOW2 写入层、
UEFI 变量文件、CID、物理和 IAM 租约均已回收。基盘与 guest agent
层通过停机后的摘要复核。验收脚本原始状态因同期新增其他 Docker
容器而为失败。独立核对 Docker 事件确认原有 Grafana、Prometheus、
node-exporter 和 wandb-local 无生命周期事件，本次验收所属临时
etcd 已销毁。原始脚本结果与内核审计复核一并保留，实机证据位于
`~/dvd04/ui1-before-docker-check-fix/`。

## 2026-11-11 TASK-022 修正 OSWorld 验收的 Docker 检查

验收前记录宿主运行中容器的完整 ID 和启动时间。结束时只核对这些容器是否仍运行且启动时间未变。临时 etcd 使用本次运行独有的标签，回收后连同已停止的容器一起查询。模拟回归覆盖新增容器、原有容器停机、重启或移除，以及临时 etcd 残留。

沿用 TASK-021 的安装版和隔离配置，经正式 HTTPS SDK 在两台全新 FullVM 上运行回收站题 `5ea617a3`。两次官方评分均从 0.0 到 1.0，均达到 32 步上限，恢复海报的摘要与原始素材一致。

原始总报告为 `passed`，进程退出码为 0。4 个原有容器均保持运行且未重启，临时 etcd 已删除。两台 VM 的写入层、UEFI 变量文件、cgroup 和租约均已回收，IAM、物理和 CID 租约均归零。实机原始报告为 `~/dvd04/ui1/report.json`，SHA-256 为 `103084e4c5698adee02035a29bc87f77e3647a0879bf616113384629823eb546`。

## 2026-11-24 TASK-007 正式 API 首次使用实机验收

从工作树按发布白名单导出 530 个文件，归档 SHA-256 为
`1843a64daa863705976f77a3957c73c551d4d04259fda811055e826287c5a970`。
从归档离线构建唯一所需的核心 wheel，
SHA-256 为 `6f914048b6b6bed39706243e51340407aabf6d191ca757df24128f86b0b1bd70`。
Mac 客户端、两个容器化 API 和独立 Linux Node 使用同一 wheel。
按公开部署指南生成配置，初始化三成员 etcd，注册并激活 Node。

Linux 复用宿主 KVM 和已有 Alpine 3.20 镜像，重新取得 Firecracker
1.17.0 与官方 CI 内核 6.1.188，用发布包中的构建器生成离线 ext4 guest。
通过正式 HTTPS API 提交内置 counter 任务。两轮固定回复中，第一轮在
guest 写入计数值 3，第二轮结束任务。verifier 读取结果，评分 1.0。
rollout ID 为 `8f0499d019e0443ead3832678614ca7c`。该用例覆盖 API
部署和验证器执行，不采样模型。

沙盒与 rollout 的持久记录均为 STOPPED，物理租约为 RELEASED，Node
退役收据为 RETIRED、held=0。三个 Linux 用户服务均已停止，临时
Compose 容器、网络、etcd 卷和控制面镜像已回收。工作区证据在
`.runtime/release-entry-20261124/`，包含评分报告、Node 退役收据和回收日志。

## 2026-11-24 TASK-002 正式 API 下的会话文件与 HTTP

复用 TASK-007 安装的核心 wheel（SHA-256 前缀 `6f914048`）、Firecracker
1.17.0 和 Linux 6.1.188。经独立的双 API 控制面和一台 Linux Node
发布两个环境。测试 guest 内的 BusyBox HTTP 服务只监听回环地址。
客户端通过正式 HTTPS SDK 请求文件及 HTTP 两项能力。

只声明会话文件的环境返回 `NoPlacement`，查询项目准入清单得到空列表。
具备两项能力的环境创建了一台 MicroVM。在同一会话内写入并读回
69,888 字节，逐字节相同。对来宾回环服务发送含二进制正文的 POST，
响应状态 200、响应头符合预期、正文为前 16 字节且标为 truncated。
相同请求 ID 重发得到相同响应，服务端执行计数为 1。

沙盒停止，唯一物理租约释放，Node 以 held=0 退役。5 个原有宿主容器
保持运行和原启动时间。本轮 Node、Edge、Compose 服务、卷和网络均已回收。
工作区记录位于 `.runtime/session-http-20261124/run2/` 和同级回收收据。
早期尝试中，在 etcd 初始化完成前复制 Node 配置遗漏了签发的凭据。
按公开指南补齐后以原 ID 确认激活。拒绝用例的证明取自正式准入清单。
此次实机范围为无外网的单 MicroVM、回环 HTTP 和有界响应。

## 2026-11-24 TASK-003 共享 EROFS 层的重复校验

在 Linux 本地 ext4 上，用已验收的安装版 wheel（SHA-256 前缀
`6f914048`）发布两个 Firecracker 环境。二者共用启动盘、内核和一个
48,201,728 字节的 EROFS 层，各有一层独有数据。三轮均复用同一对象库，
每轮发布到新的目标目录清单。原接口会拒绝在同一清单重复注册同名环境。
下表只计这个共享层。逻辑读取量累计实际返回给校验代码的字节，底层
读取量取执行线程的 Linux `read_bytes` 增量，耗时累计各次读取及哈希区间。
首次发布和首次启动 Edge 前仅对实验副本调用 DONTNEED。

| 阶段 | 完整读取次数 | 逻辑读取 MiB | 底层读取 MiB | 共享层耗时 ms |
| --- | ---: | ---: | ---: | ---: |
| 首次发布 A，新对象 | 3 | 137.91 | 45.97 | 93.15 |
| 首次发布 B，命中 A 的对象 | 4 | 183.88 | 0 | 91.91 |
| 两个环境各再发布两次，每次 | 4 | 183.88 | 0 | 91–93 |
| 首次启动 Edge，加载两个环境 | 1 | 45.97 | 45.97 | 30.22 |
| 两次热重启 Edge，每次 | 1 | 45.97 | 0 | 22.89–22.97 |
| 每次重启后恢复两个已有 VM，合计 | 0 | 0 | 0 | — |

命中已有对象时，源目录 resolve 全读源层，随后 `_copy_object` 分别
哈希源层与已有目标对象，最后 `_atomic_catalog` 新建 catalog 并全读
目标对象。这四次均发生在发布进程。Edge 在 `load_configuration`
为新进程的 catalog 首次验证；第二个环境和随后 `registry_store.load`
调用的 `RuntimeStorage.prepare` 共用其校验缓存。两次重启后通过原
VM ID 读回各自的数据。对已缓存的测试目标以相同长度、不同内容的文件
原子替换后，两环境按旧 SHA-256 均拒绝。两台 VM 均已停止，测试副本
和实例写盘已回收。

决定优先在单次发布内复用同一对象的已验证身份与摘要，分别验证源和
目标，文件身份变化时重新校验。目标是把已有对象的四次全文读取降到
至多两次。跨进程凭据和 fs-verity 另行设计；本次通用 catalog 使用
普通 SHA-256。结果覆盖宿主校验，未计量 guest 工作负载或 3FS。
原始逐文件记录、调用栈、接管结果和清理收据在工作区
`.runtime/publication-read-20261124/`。

## 2026-11-24 TASK-023 本地发布校验复用

按[设计提案](../guides/ARTIFACT_PUBLICATION.md#task-023-设计提案--单次发布内复用本地校验)
实现发布内文件校验复用。沿用 TASK-003 的两个 Firecracker 环境、相同输入字节、
相同探针和页缓存处理方式，在同一台 Linux 主机上安装新 wheel
`59204ca6aa94288d3643b1079c48c227d3fd516342f39ec6fcaab2d7f3e0142e`。
每次发布在独立进程执行。共享层为 48,201,728 字节。

| 每次发布的指标 | 原版本 | 新版本 |
| --- | ---: | ---: |
| 首次创建对象，完整读取次数 | 3 | 3 |
| 命中已有对象，源层及目标全读次数 | 2 + 2 | 1 + 1 |
| 命中已有对象，共享层逻辑读取量 | 183.88 MiB | 91.94 MiB |
| 命中已有对象，共享层读取与哈希耗时中位数 | 91.66 ms | 45.54 ms |
| 命中已有共享层，整个环境发布耗时中位数 | 364.70 ms | 188.19 ms |

中位数分别来自两版各五次命中。此次命中的共享层均在页缓存中，两版的线程物理
读取计数都是 0 B。首次创建对象时，两版均从底层读取共享层 45.97 MiB。
整次发布包含启动盘、内核及任务独有层，耗时也包含采样开销。样本来自本地 ext4。

两台 VM 分别写入不同标记，两次 Edge 重启后均经原 ID 读回。每个新 Edge 进程
加载共享层时完整读取一次，接管阶段额外读取为零。以同长度、不同内容的文件
替换已核验目标后，两环境均拒绝旧摘要。两台 VM 的停止和写盘回收已验证，
本次输入与对象副本已删除。独立脏数据、发布阶段中途替换、并发硬链接提交
时校验实际胜出对象，以及连续两次发布分别校验，均有回归覆盖。
Linux 安装版发布器和目录测试 20 项通过。工作区实测证据在
`.runtime/publication-reuse-20261124/` 的 `trace.jsonl`、`recovery.json` 和
`cleanup.json`。跨进程凭据与远端 3FS 成本仍需独立验收。

## 2026-11-24 TASK-024 构建依赖的文件级增量发布

实验机使用核心 wheel `3c749c467a09f3ee5da72155892166ee0982ad2dbb066f23f0468fd81a11d9af`、
Firecracker 1.17、64 MiB Alpine 原生 ext4 父盘和 Linux e2fsprogs。
客户端通过带证书校验的回环 HTTPS API 调用公开 SDK，使用真实 etcd；
本次 Controller 与 NodeGateway 在同一进程，Edge 和 VM 单独运行。
每次只启动一台 VM。本次验证制品交接和恢复，不测多机放置。

1. Builder 写入可执行文件 `/opt/dsec-tools/answer` 和 `/tmp/reference-answer`，
   前者返回 42。清单只批准可执行文件的模式、大小和 SHA-256。
   SDK 捕获的增量共 349 B，解码后只有批准的文件和清单。
2. 替换预期父摘要后，发布被拒绝，既有环境目录字节保持原样。有效增量成功发布，
   父启动盘 SHA-256 与创建前一致。此次物化完整 64 MiB 子盘。
3. 第一台子 VM 可直接执行依赖并返回 42，构建临时文件不存在。修改其中的程序并
   停止 VM 后，新建父 VM 看不到程序或临时文件，第二台新子 VM 仍运行原程序返回 42。
4. 四个不同的沙盒 ID 均达到 STOPPED，资源回收标志均为 true。临时 etcd 容器、
   子对象库和 Edge socket 已回收。Linux 安装版 3 个针对性回归通过。
   原始结果见工作区 `.runtime/pack-diff-20261124/report.json`。

验收脚本的第一次启动沿用了修改父描述之前的能力声明，Edge 正确拒绝。第二次
缺少 Edge 准入预算，无法获得可放置 Node。验收配置移除旧声明并显式传入独立
实例的预算后完成上述检查。这两次尝试都未进入 VM 创建。

## 2026-11-24 TASK-025 OverlayBD 父层复用与子 VM 恢复

同一台 Linux 主机使用核心 wheel `5863e9e50d3db282389e150114b98c08918f0009ab6bb9f402d8a340ac49ea86`、
Firecracker 1.17 和发布版 Rust ublk daemon（SHA-256 前缀 `b78207e6`）。
四个实例依次由公开 SDK 通过回环 HTTPS API 创建，Controller 与 NodeGateway 同进程，etcd、Edge 和 VM 独立运行。
临时启动器最初把可执行文件放在挂载了 noexec 的 /run，systemd 拒绝启动。
改用 root 管理的 /usr/local/libexec 并核对 socket 对端 PID 后执行本轮验收。

- 用 64 MiB Alpine ext4 启动盘构造两层 OverlayBD 父镜像。构建实例写入批准的可执行文件和未获批准的临时文件，SDK 捕获的交接包为 349 字节。错误父摘要触发拒绝，目录内容未变。
- 发布子环境后，两个父 lower 的路径、顺序、inode 和 SHA-256 均保持一致。对象库仅新增 77,824 字节的 commit 和 441 字节的镜像描述文件。重启 Edge 加载已发布目录后创建新实例。
- 第一台子 VM 运行新增程序返回 42，临时文件不存在。在该实例覆盖程序后，新建父 VM 看不到程序及临时文件，第二台子 VM 仍返回 42。
- 四个独立实例均达到 STOPPED 并报告资源已回收。主机 ublk 块设备集合恢复到验收前，临时 etcd 和私有对象库已回收。原始结果保存在工作区 `.runtime/pack-overlaybd-20261124/report.json`。

本次结果覆盖单机本地对象库和一个小型新增依赖。增量大小包含 ext4 元数据和块层格式开销，不能外推到其他工作负载。

## 2026-11-24 TASK-026 两个 Node 的制品交付与实例恢复

核心 wheel SHA-256 为 `990d6638cad17782bdbba9dcd48cdcb472a3f93375976ec4e7e5cf1dce6d4076`。
单台 Linux/KVM 宿主运行两个独立 Edge 进程，各自有目录、对象和租约。两个 NodeGateway 与控制面同进程，使用真实 etcd 和回环 HTTPS SDK。

1. 用历史批准增量生成带 `/opt/dsec-tools/answer` 的 ext4 子环境。通过安装版发布器导出清单，在新路径导入并重建 B 的部署条目。部署摘要改变，制品版本一致。启动 Edge 前已移除源对象库和临时传输副本。
2. A 上线同名旧环境，B 上线所请求的新环境。不存在的版本返回 NoPlacement，IAM 中没有该请求的准入记录。正确版本仅在 B 创建，同一请求 ID 重试得到相同的沙盒 ID。较空闲旧节点的优先级通过确定性的放置回归单独检查。
3. 新 VM 运行批准程序返回 42，并写入来宾临时文件。公开 SDK 把实例暂停至 PAUSED，在 B 恢复，再次读到原文件内容并执行程序返回 42。
4. 实例达到 STOPPED 并报告物理清理完成，IAM 准入已释放，两个 Node 的物理租约均为空。两套临时 Edge、对象库和 etcd 已退役。初次验收探针过早访问尚未创建的 Edge socket，随后修正等待条件，未创建 VM 的临时目录已清理。

安装包最终结果在工作区 `.runtime/task026-20261124/final-report.json`。这轮实机使用本地直接 ext4 根盘。另有发布器回归验证 OverlayBD 多层顺序、能力声明重绑定、不同路径恢复与损坏拒绝。两个物理宿主、远程 3FS 惰性配置和运行中实例跨宿主迁移均超出本轮范围。
