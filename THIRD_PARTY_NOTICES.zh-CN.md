<a id="third-party-code-and-external-runtime-artifacts"></a>

# 第三方代码与外部运行制品

[English](THIRD_PARTY_NOTICES.md) · **简体中文**

这是 DSec 的独立复现。项目自有代码和文档使用项目所有者于 2026-10-06 选定的 MIT 许可证，见 `LICENSE`。下文源自 Miles 的模块保留 Apache-2.0 许可证及署名。wheel 同时包含这两类文件，许可证表达式为 `MIT AND Apache-2.0`。项目自有文件仍按 MIT 授权。

<a id="miles-agent-loop"></a>

## Miles 的 Agent 循环

`dsec_adapters/miles_session.py` 和 `apps/tb21/src/dsec_tb21_case/openenv_agent_function.py` 源自 [radixark/miles](https://github.com/radixark/miles/tree/98942681e85ec38a312091771e04c1736ec583c3/examples/experimental/openenv) 中的 `examples/experimental/openenv/openenv_agent_function.py`。

- 已审阅的上游提交为 `98942681e85ec38a312091771e04c1736ec583c3`。
- 已审阅源码的 SHA-256 为 `7822b9db51672e2e5fd4f3943ccbc451b5794bc7469f09fb5e657094a7921976`。
- 上游许可证为 Apache License 2.0，Copyright 2025 Zhipu AI。
- 上游许可证全文见 `licenses/miles-APACHE-2.0.txt`。
- 本地改动包括可选的独立策略服务 URL、显式的策略调用钩子、评分器选择及判定诊断。DSec 的 episode 接入使用自身调度的 worker，无需 OpenEnv 服务。
- 历史模块 `dsec_adapters.openenv_agent_function` 是通用 Miles 会话实现的导入别名。任务专属的 OpenEnv 属性在使用时才转交给可选 TB2.1 应用。实验兼容模块仍作为导入别名保留，两个包都保留完整的 Apache-2.0 文本。

以上记录指明已审阅的参考版本。派生实现含有本地修改，后续改动须保留这些声明。

<a id="external-dependencies-and-artifacts"></a>

## 外部依赖与制品

Firecracker、guest Linux 内核、EROFS 工具、Rust OverlayBD/ublk、3FS、Docker、Miles 和模型权重均为独立依赖。开发版 wheel 不包含其二进制、仓库或权重。制作完整部署镜像时，请遵守各自固定源码版本的许可证。控制面源码声明不授予这些依赖的再分发权利。

Terminal-Bench 2.1 任务也是独立数据。固定验收轨迹使用的任务版本为 `7131e4375048a0e408a8fb404b5f499d726b695b`。wheel 不再分发任务集、容器镜像或验证器数据。诊断缓存清单记录哈希及兼容性元数据，不随之包含所引用的 wheel 内容。

源码发布时必须排除 `.runtime/`、生成结果、下载的仓库、凭据以及虚拟机和模型制品。`tools/check_wheel.py` 按显式列出的源码和数据检查 wheel。发布前仍需对源码树进行最终审阅，另行审计完整的依赖许可证。
