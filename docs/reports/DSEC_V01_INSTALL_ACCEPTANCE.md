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
