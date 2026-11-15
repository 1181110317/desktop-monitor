# 桌面性能监视器

一个轻量的 Windows 性能监视器，在任务栏或桌面随时查看电脑状态。Python + Pillow 绘制，Win32 原生采样，监控数据在本机处理。

## 功能

- 四种显示方式：任务栏条、悬浮卡片、紧凑条、仅托盘。
- CPU 与内存占用、GPU / 显存 / 温度、上下行网速与趋势、磁盘余量、电池及运行时长。
- 进程按 CPU 排序、合并同名进程，支持展开到 8 行，查看 CPU、工作集内存和 TCP 连接数。
- 六种任务栏外观：融入、雾面、分舱、细线、能量、霓虹；首版、微型、双行、横排、圆环、趋势六种排版。
- 四组任务栏指标：均衡（CPU / 内存 / 上下行）、硬件（含 GPU 和温度）、网络（含累计流量）、续航（含电池和运行时长）。
- 默认恢复首版 CPU / RAM 与上下行双行条，沿用首版清晰字号；“内容间距”只收紧留白，不缩小文字。保留五种强调色与实际大小预览。
- 四套面板主题：深空、云白、素纸、雾蓝。任务栏 / 面板 / 行为 / 关于四个设置页签，支持刷新间隔、透明度、层级、位置锁定、开机启动和全屏隐藏。
- DPI 缩放、工作区边界适配；配置自动保存，重复启动不会叠加实例。

## 使用

已发布版本见 [Releases](https://github.com/1181110317/desktop-monitor/releases)。最新代码的可执行文件可从 [Windows 构建](https://github.com/1181110317/desktop-monitor/actions/workflows/windows.yml) 下载 `DesktopMonitor-Windows-x64` 产物。

新版提供 `DesktopMonitor-Windows-x64.zip`。完整解压后运行文件夹内的 `DesktopMonitor.exe`，无需安装 Python，支持中文路径。请将 exe 与 `_internal` 文件夹一起保留。升级前从托盘菜单退出旧版，再运行新版。

| 操作 | 效果 |
| --- | --- |
| 左键点击任务栏条 / 托盘图标 | 展开或收起详情 |
| 右键点击信息条 / 面板 / 托盘 | 打开菜单 |
| 菜单中的“样式与布局” | 预览并切换任务栏外观、排版、指标、强调色和尺寸 |
| 详情右上角设置按钮 | 打开偏好设置 |
| 悬浮模式下拖动 / 双击 | 移动 / 切换卡片与紧凑条 |
| 详情中的展开按钮 | 3 行 / 8 行进程列表 |
| Esc / Ctrl+Q | 关闭弹出窗口 / 退出监视器 |

## 源码运行与打包

Windows 10 / 11，Python 3.10+；当前在 Python 3.12 上验证。

```powershell
python -m pip install -r requirements.txt
python monitor.pyw
# 启动时打开样式设置
python monitor.pyw --settings
```

构建便携版：

```powershell
python -m pip install -r requirements-dev.txt
python tools/build.py
```

输出为 `dist/DesktopMonitor/DesktopMonitor.exe` 和 `dist/DesktopMonitor-Windows-x64.zip`，已包含图标、Pillow 与 Tk。`main` 分支推送后自动检查并构建；推送 `v*` 标签会发布对应 Release。

## 数据与配置

- CPU、内存、磁盘、网络使用 Windows API；网速汇总活动物理网卡，不计算 VPN、虚拟网卡与回环流量。速度单位为字节/秒。
- GPU 读取首块 NVIDIA 显卡的 `nvidia-smi`。AMD / Intel 或传感器不可用时显示占位，其余指标继续工作。
- CPU 温度优先读取已运行的 LibreHardwareMonitor / OpenHardwareMonitor；ACPI 热区会标注“热区”，可能不代表 CPU 核心温度。
- 进程内存为工作集；TCP 是非监听连接数，不代表进程实时网速。部分受保护进程无法读取明细。
- 打包版配置与错误日志位于 `%LOCALAPPDATA%\DesktopMonitor`，首次运行可迁移 exe 旁的旧配置；源码版保存在项目目录。可用 `DM_CONFIG_PATH` 指定独立配置路径。

## 验证

```powershell
python -m unittest discover -s tests -v
python tools/ui_selftest.py
python monitor.pyw --selftest
# 打包后验证原路径和中文路径
python tools/check_packaged.py
```

UI 自检使用临时配置，覆盖显示模式、主题、设置、全屏恢复、计时器与 GDI 资源释放，不改动现有偏好或自启动设置。完整改动见 [更新记录](CHANGELOG.md)。

任务栏原有的“素净 / 胶囊 / 描边”配置会自动迁移。推荐“融入 + 首版”；首版排版固定沿用原始字号，间距可单独调整。网速以 K / M / G 简写，完整单位见详情。电池、显卡或温度不可用时显示占位。

## 许可

MIT © 1181110317
