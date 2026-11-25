<a id="terminal-bench-21-application-for-dsec"></a>

# 在 DSec 上运行 Terminal-Bench 2.1

[English](README.md) · **简体中文**

这个可选应用与 DSec 控制面单独安装，提供任务适配器、清单校验、制品准备工具及 worker 端验证器插件。任务数据、镜像、模型权重和 OpenEnv 服务需另行准备。请显式指定可信的代码检出目录、本地镜像和工具，以下命令均不会自动下载。

```sh
# First install the DSec control plane, then this optional application.
python -m pip install ./apps/tb21
dsec-tb21 stage --repo /data/terminal-bench-2-1 \
  --tasks openssl-selfsigned-cert --out /data/tb21-case
dsec-tb21 check --suite /data/tb21-case
```

请检出包含 89 个源任务的修订版 `7131e4375048a0e408a8fb404b5f499d726b695b`。可以只准备指定任务，或用 `--all` 显式选择全部任务。所选任务的源文件须处于干净状态。准备时会排除参考解的 `solution` 目录，并固定每个复制文件的内容，包括任务指令、配置及官方验证器。文件改动、缺失、多余、出现符号链接或混入参考解，都会导致检查失败。

<a id="use-existing-prepared-artifacts"></a>

## 注册已有制品

```sh
dsec-tb21 register --suite /data/tb21-case \
  --catalog /data/prepared-environments.json --out /data/tb21-catalog.json
```

源目录中每个环境的名字形如 `tb2-<task-id>`。注册时会校验所选任务的资源和本地制品哈希，生成仅包含这些任务的新目录。此步骤无需复制磁盘，支持核心已有的 file-ext4 和 OverlayBD/ublk 配方，包括预先合层或启用 DAX 的配方。源目录及运行中的服务均保持原状。

为专用 `dsec-host` 实例配置生成的环境目录、`worker.tb2_tasks_dir=/data/tb21-case/tasks` 和 `sandbox.tb2_manifest=/data/tb21-case/manifest.json`。通过宿主配置指定验证器工具盘和清单。联网 guest 还需实例专用的网络 helper。TB2.1 使用各任务的官方 `tests/test.sh` 评分，由 worker 保存输出。在沙盒守护进程和 worker 的 Python 环境中，分别从与核心相同的代码版本安装此应用。守护进程会将旧版 `tb2_*` 清单验证交给应用，缺少应用会在创建资源前报错。通用核心本身无需安装 TB2。只有配置 `worker.tb2_tasks_dir` 后，worker 才会加载应用并注册 `tb21-canonical-v1`；缺少应用时，在分配 worker 状态前即报错。旧的 `tb2_evaluate` 请求仍可用。通用调度 SDK 也可调用 `evaluate("tb21-canonical-v1", timeout_s=12100)`。该评分器使用官方二元奖励，要求持久保存证据，不接受替换验证器。[worker 评分契约](../../docs/architecture/AGENT_ENVIRONMENT_CONTRACT.zh-CN.md#worker-评分插件)

<a id="prepare-a-new-image"></a>

## 准备新镜像

```sh
dsec-tb21 prepare-image --suite /data/tb21-case --task openssl-selfsigned-cert \
  --image sha256:<local-task-image-id> --tools-image sha256:<local-tools-image-id> \
  --kernel /opt/dsec/vmlinux-erofs --agent-source ./guest_agent.c \
  --busybox /usr/bin/busybox --network --output /data/openssl-prepared
```

需要 Linux x86_64、Docker、GCC 和静态 libc、`mkfs.ext4`、带有 mount/chroot/ip 功能的静态 BusyBox，以及支持 EROFS 的 guest 内核。固定版本的工具镜像须提供支持 `--tar=f`、`--aufs`、LZ4 和压缩 tar 的 `mkfs.erofs`。两个镜像参数都须填写精确的本地镜像 ID；任务声明的 Docker 镜像必须能在本地解析到所提供的任务镜像 ID。该命令只使用现有的镜像和软件。

构建工具核验 OCI diff ID，保留 whiteout 删除标记和层顺序，构建私有启动模板并生成已校验的环境目录。转换前会留出磁盘余量，失败原因记入 `prepare-result.json`。独立构建目前生成 **file-ext4 可写根盘**，最多直连 12 层 EROFS，为其他 VirtIO 设备留出位置。输入超过上限会在转换前被拒绝。需要离线合层、OverlayBD 根盘或外部验证器工具盘时，应提供已准备和验证过的目录。即使层数在上限内，仍需检查具体 guest 的设备布局。OverlayBD 准备态复用有单独的验收记录。

<a id="agent-and-training-entry"></a>

## 接入 Agent 和训练框架

应用使用已安装的 `TB2DSecEnvironment` 和 `DSecAgentEnvironment`，通过 `reset`、可持久恢复的 `step`、`evaluate` 和 `stop` 管理任务。使用 API 的 Agent 负责模型调用和回复解析。Miles 通过包内适配器和以下环境变量接入。

```sh
export DSEC_TB2_TASKS_DIR=/data/tb21-case/tasks
export DSEC_TB2_ENVIRONMENT_CATALOG=/data/tb21-catalog.json
export DSEC_API_CONFIG=/run/dsec/api-client.json
```

先部署 [API 和节点 worker 桥接服务](../../docs/guides/CLUSTER_DEPLOYMENT.zh-CN.md#remote-native-sdk)。客户端配置包含 API 地址、项目以及私有 token 和 CA 文件的引用。训练框架只需访问 API。任务适配器读取已准备的任务和指令，运行时分层及验证器依赖则留在节点上。为 `DSecAgentEnvironment` 配置 `RolloutAPIClient`。回复丢失时，通过另一 API 副本按原项目和 rollout ID 找回结果。

将任务 ID 作为元数据传给训练框架。模型采样、token、logprobs、训练和 GPU 集成补丁由训练框架负责。安装此应用不会启动模型或训练进程。核心包独立安装，使用 TB2.1 时再安装应用并准备资源。

<a id="compatibility-and-package-boundary"></a>

## 兼容性与包边界

历史导入路径 `dsec_adapters.tb2_*` 和 `tb2_verifier_artifact` 均指向 `dsec_tb21_case` 中的实现。任务规则和诊断缓存元数据仅随可选应用分发，核心包不包含这些 JSON 清单。使用旧导入路径前请先安装应用，缺少依赖时会收到明确提示。

通用 Miles 策略会话封装位于 `dsec_adapters.miles_session`。历史别名 `dsec_adapters.openenv_agent_function` 指向该封装，并把显式使用的 OpenEnv/TB API 调用转交给应用。标准 DSec 循环通过 DSec worker 运行，无需部署 OpenEnv 服务。保留的 Miles 派生代码附带完整的 Apache-2.0 许可证，项目自有代码使用 MIT 许可证。
