<a id="osworld-fullvm-desktop-regression"></a>

# OSWorld FullVM 桌面回归

[English](README.md) · **简体中文**

这套可选回归测试通过已安装的 DSec SDK 连接 HTTPS API Server。SDK 须支持 `DSecFullVMRunArgs`、原生文件传输、`screenshot()` 和 `desktop_input()`。测试通过截图、键盘和鼠标操作完成两道 LibreOffice Calc 表格任务，回放过程无需模型。

使用固定版本的 Ubuntu/Xfce FullVM，安装 Thunar 和 LibreOffice Calc，选择 1024×768 的 virtio-2d 显示，并新建 Calc 用户配置。回放坐标在 Calc 7.3.7 的默认英文界面上验证过。更换桌面镜像、语言、DPI 或应用布局后，需要重新验证录制的动作。测试使用[已安装 SDK 的验收配置](../../docs/guides/DSEC_HOST_CONFIGURATION.zh-CN.md)中的 `client`、`token_file` 和 `paths.fullvm` 字段。

1. 检出 OSWorld 提交 `8903ebeb0efdf74331bd7dc37ed91ccbbff22317`。从 `evaluation_examples/examples/libreoffice_calc` 找到两道题的清单，按清单中的 URL 下载各自的输入和参考工作簿。对照 [workbook-sha256.json](workbook-sha256.json) 校验输入，把参考工作簿保存在 guest 外。
2. 通过已安装的 SDK 回放录制的动作。每次运行使用新的输出目录。

   ```bash
   python apps/osworld/desktop_regression.py \
     --config fullvm-acceptance.json --workbook BoomerangSales.xlsx \
     --actions apps/osworld/boomerang.json --output out/boomerang
   python apps/osworld/desktop_regression.py \
     --config fullvm-acceptance.json --workbook WeeklySales.xlsx \
     --actions apps/osworld/weekly-sales.json --output out/weekly-sales
   ```

3. 在独立的评分环境中安装 `openpyxl pandas rapidfuzz formulas lxml cssselect xmltodict tldextract`，运行固定版本的上游 `compare_table` 指标。回放在桌面上保存工作簿并确认 XLSX 格式，随后 SDK 下载结果。

   ```bash
   python apps/osworld/score_calc.py --upstream /path/to/OSWorld \
     --task /path/to/OSWorld/evaluation_examples/examples/libreoffice_calc/3a7c8185-25c1-4941-bd7b-96e823c9f21f.json \
     --result out/boomerang/BoomerangSales.xlsx --expected 6_BoomerangSales_gt1.xlsx
   python apps/osworld/score_calc.py --upstream /path/to/OSWorld \
     --task /path/to/OSWorld/evaluation_examples/examples/libreoffice_calc/12382c62-0cd1-4bf2-bdc8-1d20bf9b2371.json \
     --result out/weekly-sales/WeeklySales.xlsx --expected 13_WeeklySales_gt1.xlsx
   ```

4. 用 Pillow 比较两次运行 `boomerang.json` 的结果。此文件声明截图检查点。Weekly Sales 单独验证官方评分。

   ```bash
   python apps/osworld/compare_trace.py out/reference out/candidate
   python apps/osworld/compare_trace.py --diagnose out/reference out/candidate
   ```

比较工具根据活动窗口名称和当前动作指定的截图区域，报告最早发生变化的检查点。请求 ID、已提交事件、回执、截图哈希和 PNG 均保留，可用于诊断。输入回执证明 API 接受了操作。查看检查点和官方评分才能判断操作在桌面上产生了什么效果。

诊断报告分别给出首个提交事件差异、首个帧来源差异、按钮的最终状态差异，以及首个可见的检查点差异。按钮状态根据已确认的输入事件推断；回执不完整时标为未知。SDK 端无法仅凭这些证据确认 guest 收到了事件。帧来源记录了运行器返回哪次 SDK 截图，以及返回时的本地单调时钟读数。如果返回的是动作前的帧，动作后的 PNG 会单独保留。这个时间戳不能衡量 guest 的渲染耗时。旧轨迹仍可比较图像和输入，帧来源会显示为不可用。

<a id="official-ubuntu-image-on-dsec-fullvm"></a>

## 在 DSec FullVM 中使用官方 Ubuntu 镜像

准备固定版本的 OSWorld Ubuntu QCOW2 镜像及其 SHA-256，并选用配套的 OVMF 固件代码镜像和变量镜像。在 Linux KVM 宿主机上，用现有的 DSec guest agent 和 systemd 服务制作只读派生镜像。

```bash
python apps/osworld/prepare_uefi_guest.py \
  --base /path/to/Ubuntu.qcow2 --base-sha256 <official-image-sha256> \
  --source . --code /path/to/OVMF_CODE_4M.fd \
  --variables /path/to/OVMF_VARS_4M.fd --out /path/to/osworld-agent.qcow2
```

准备工具需要 Python Requests、QEMU/KVM、qemu-img 和支持静态链接的 C 工具链。制作镜像时，通过回环端口访问 OSWorld 镜像自带的 guest 服务，guest 的出站网络被阻断。派生镜像继续引用官方镜像作为基盘。

在 Edge 的 QEMU 执行目录中，配置离线 `fullvm` 环境，设置 `display: "virtio-2d"`，并在 `template` 中固定派生镜像。选择 `uefi` 启动配置，省去直接引导使用的内核字段。用 `{"path": "...", "sha256": "..."}` 分别在 `code`、`variables`、`image_tool` 和 `base` 中固定 OVMF 代码、初始 OVMF 变量、qemu-img 和官方 Ubuntu 镜像。将 `disk_mb` 设为镜像的虚拟容量（MiB），本版本为 51200。节点预算还要覆盖 QCOW2 元数据和固件变量。SDK 仍通过 `run_fullvm` 启动，传入新的环境 ID 即可。每次运行都有独立的 QCOW2 写入层和固件变量，停止后回收。

要启用 AppArmor 和 cgroup 限制，应基于同一份执行目录生成 broker 配置，再在具备资源控制委派权限的服务内启动 Edge。管理员审批实际使用的只读 OVMF 代码和基盘。[QEMU 宿主隔离指南](../../docs/guides/DSEC_HOST_CONFIGURATION.zh-CN.md#qemu-host-confinement)
