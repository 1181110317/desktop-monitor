# 桌面性能监视器

Windows 任务栏上的实时性能条。

## 功能

- 嵌进任务栏：CPU / 内存 / 上传 / 下载，一秒刷新
- 点击展开详情：GPU、温度、网速曲线、Top 进程、磁盘、电池
- 三套样式（素净 / 胶囊 / 描边）+ 四套主题（深色 / 浅色 / 简约 / 玻璃）
- 进程列表可展开到 8 行，设置里可调刷新间隔和不透明度
- 全屏时自动隐藏；纯 Python + Pillow，无服务无联网

## 使用

下载 [Releases](https://github.com/1181110317/desktop-monitor/releases/latest) 里的 `DesktopMonitor.exe`，放到纯英文路径下双击即可。

源码运行：

```bash
pip install pillow
python monitor.pyw
```

## 实现

单文件 `monitor.pyw`。采样走 Win32 API（`GetSystemTimes`、`GetIfTable2`、`GetExtendedTcpTable` 等），GPU 读 `nvidia-smi`；任务栏那层用 `WS_EX_LAYERED` + `UpdateLayeredWindow` 做逐像素透明，界面由 Pillow 渲染。

## 许可

MIT © 1181110317
