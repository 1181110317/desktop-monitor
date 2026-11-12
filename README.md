<div align="center">

<img src="docs/images/icon.png" width="88" alt="icon">

# 桌面性能监视器

**嵌在 Windows 任务栏里的一行实时性能数据**

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white)
![Deps](https://img.shields.io/badge/deps-Pillow-brightgreen)
![Release](https://img.shields.io/github/v/release/1181110317/desktop-monitor?color=2ea043)

<img src="docs/images/taskbar.png" width="560" alt="任务栏效果">

</div>

---

## 它能做什么

- **嵌在任务栏里**：贴在托盘左边，背景真透明，看不出是浮窗
- **点一下看详情**：CPU / GPU / 内存 / 上下行 / **Top 进程**（CPU、内存、活跃连接数）/ 温度 / 磁盘 / 电池，弹在条的正上方
- **三套样式 + 四套主题**：素净 / 胶囊 / 描边；详情卡支持深色 / 浅色 / 简约 / 玻璃
- **零服务零联网**：采样全走 Windows API，只有渲染用 Pillow
- **崩溃自愈**：看门狗每 3 分钟检查，进程没了自动拉起

<img src="docs/images/detail.png" width="420" alt="详情">

<table>
<tr>
<td><img src="docs/images/settings.png" width="360" alt="设置"></td>
<td>

**设置窗口**里可以改：

- 任务栏样式、字色主题、详情卡主题
- 刷新间隔（滑块 0.5–5 秒）
- 不透明度（滑块 30%–100%）
- 锁定位置、开机自启

入口：展开详情后点右上角 `···`

</td>
</tr>
</table>

## 快速开始

**直接下载**：到 [Releases](https://github.com/1181110317/desktop-monitor/releases/latest) 拿 `DesktopMonitor.exe`，放在**纯英文路径**（如 `%LOCALAPPDATA%\Programs\DesktopMonitor\`）下双击即可。

**从源码跑**：

```bash
pip install pillow
python monitor.pyw
```

**自己打包**（目录也用纯英文路径，否则 PyInstaller 会找不到图标）：

```bash
pyinstaller --noconfirm --onefile --windowed --icon icon.ico --name DesktopMonitor monitor.pyw
```

## 技术实现

单文件 `monitor.pyw`，约 2200 行：

| 部分 | 用的东西 |
| --- | --- |
| 采样 | `GetSystemTimes` `GlobalMemoryStatusEx` `GetIfTable2` `GetDiskFreeSpaceExW`，GPU 走 `nvidia-smi` |
| 进程 | `CreateToolhelp32Snapshot` + `GetProcessTimes` / `GetProcessMemoryInfo`，连接数来自 `GetExtendedTcpTable` |
| 任务栏停靠 | `SHAppBarMessage` + `EnumChildWindows` 找托盘位置 |
| 渲染 | Pillow 超采样，任务栏那层用 **WS_EX_LAYERED + UpdateLayeredWindow** 做逐像素透明 |
| 托盘 | `Shell_NotifyIconW` + 自定义 `WndProc` |

踩坑记录（都写在代码注释里）：`W` 后缀 API 不声明原型会在 64 位下越界崩溃、`MIB_IF_ROW2` 的 `OutOctets` 偏移与文档不符导致上传速度永远是 0、Tk 透明色键做不出半透明边缘……

## 许可

MIT © 1181110317
