# ai-env-clone

> 跨电脑备份与迁移你的 AI 编程环境——AI 开发者的"手机克隆"工具。
> Backup & migrate your AI coding environment across computers. Like a phone transfer tool for AI devs.

[![GitHub](https://img.shields.io/badge/mirror-GitHub-blue)](https://github.com/yinlichaoxi007/ai-env-clone)
[![Gitee](https://img.shields.io/badge/mirror-Gitee-red)](https://gitee.com/yinlichaoxi007/ai-env-clone)

## 简介

`ai-env-clone` 帮助你把本地 AI 编程工具（如 Qoder）积累的**记忆（memory）**、**会话历史（chat history）**、**代码索引**、**配置**等数据，打包成一个离线备份文件，方便在新电脑上完整恢复，避免重新训练和丢失上下文。

- 🖥️ **双形态**：同一套核心逻辑，既提供命令行（CLI）也提供图形界面（GUI，基于 Python 自带的 tkinter，无需额外安装）。
- 📦 **零第三方依赖**：运行时不依赖任何第三方库，仅用 Python 标准库。发布版的**单文件 exe 双击即用、无需安装 Python**；从源码运行（`python qoder_backup_tool.py` 或 `run.bat`）则需本机已装 Python 3.10+。
- 🌐 **跨平台**：Windows / macOS / Linux 均可运行（GUI 在三大平台均可用）。
- 🔌 **可扩展**：以"适配器（adapter）"模式设计，新增一种 AI 工具只需添加一个适配器模块。
- 🛡️ **安全可靠**：恢复前自动生成回滚快照；SQLite 数据库使用在线备份 API 一致性快照；内置 Zip Slip 路径穿越防护。

## 支持的工具

> 定位：**仅支持国内 AI 编程工具**。国外工具不在支持范围内（作者无法访问、无法测试）。
> 反过来，国内工具国外用户同样可以使用，因此本工具对海外用户也有价值。

| 工具 | 状态 |
| --- | --- |
| Qoder | ✅ 已支持 |
| 其他国内工具（如 Trae、豆包、通义灵码、CodeBuddy 等） | 🚧 规划中，欢迎贡献适配器 |

## 安装

### 方式一：下载发布版（推荐，无需 Python）

到 [Releases](https://github.com/yinlichaoxi007/ai-env-clone/releases) 下载对应平台的单文件程序：

- Windows：`QoderBackupTool.exe`（或未来的 `ai-env-clone.exe`），双击运行。
- macOS / Linux：下载可执行包，赋予执行权限后运行。

### 方式二：从源码运行（需 Python 3.10+）

```bash
git clone https://github.com/yinlichaoxi007/ai-env-clone.git
cd ai-env-clone
python qoder_backup_tool.py        # 启动 GUI
```

### 方式三：自行打包单文件 exe（Windows）

```bash
pip install -r requirements.txt
python build_exe.py                # 产物在 dist/ 目录下
```

## 使用

### GUI（图形界面）

```bash
python qoder_backup_tool.py
```

启动后：自动检测数据目录 → 勾选要备份的内容 → 点击「导出备份」生成 zip，或「还原备份包」恢复。

也可双击仓库内的 `run.bat`（Windows，需本机已装 Python 3.10+）一键启动。

### CLI（命令行）

> 当前 CLI 入口正在完善中，核心逻辑层 `qoder_backup_core.py` 已完全解耦，可独立调用 `export_backup` / `import_backup` / `inspect_backup`。

```bash
# 备份
python qoder_backup_tool.py --backup --out ./my-backup.zip

# 恢复
python qoder_backup_tool.py --restore --in ./my-backup.zip
```

（具体 CLI 参数以发布版本为准，请关注 Release Notes。）

### 备份包说明

- 备份产物是一个标准 `.zip` 文件，内含 `qoder_backup_manifest.json` 清单（记录创建时间、来源目录、包含模块、文件数等）。
- 可用「查看备份包」功能校验完整性，或直接解压查看内容。
- 恢复时会**自动覆盖**同名文件，并在覆盖前生成 `qoder_rollback_*.zip` 回滚快照，可随时还原到恢复前状态。

## 架构

```
ai_env_clone/
├── core.py            通用核心层（扫描/打包/校验/恢复/SQLite快照/ZipSlip防护），与具体工具解耦
└── adapters/
    ├── base.py        BaseAdapter 抽象接口 + 适配器注册表
    └── qoder.py       Qoder 适配器（参考实现）
qoder_backup_core.py   Qoder 兼容层（保留历史 API，委托 ai_env_clone 实现）
qoder_backup_tool.py   图形界面层（tkinter），负责交互与进度展示
build_exe.py           用 PyInstaller 打包成单文件 exe
```

**多工具扩展**：已采用统一的适配器接口（`detect_root()` / `build_items()` / `export()` / `restore()`）。每种 AI 工具对应一个适配器模块，新增工具无需改动主流程，详见 `docs/CONTRIBUTING.md`。

## 贡献

欢迎提交 Issue 与 Pull Request！尤其欢迎为更多**国内 AI 工具**贡献适配器。

- 仓库主站为 **GitHub**，Gitee 为只读镜像，**请到 GitHub 提交贡献与 Issue**（Gitee 不接收 PR）。
- 贡献规范与适配器接口说明见 `docs/CONTRIBUTING.md`。

## 许可证

[MIT](./LICENSE) —— 可自由使用、修改、分发，包括商业用途。

---

# ai-env-clone (English)

> Cross-PC backup & migration for memory, chat history and configs of **domestic (China-based) AI coding tools**. CLI + GUI, zero runtime dependencies, cross-platform.

[![GitHub](https://img.shields.io/badge/mirror-GitHub-blue)](https://github.com/yinlichaoxi007/ai-env-clone)
[![Gitee](https://img.shields.io/badge/mirror-Gitee-red)](https://gitee.com/yinlichaoxi007/ai-env-clone)

## Introduction

`ai-env-clone` helps you package the **memory**, **chat history**, **code index** and **settings** accumulated by your local AI coding tools (e.g. Qoder) into an offline backup archive, so you can fully restore them on a new machine without retraining or losing context.

- 🖥️ **Dual mode**: one core, both CLI and GUI (GUI via Python's built-in tkinter, no extra install).
- 📦 **Zero runtime dependencies**: standard library only at runtime; the packaged single-file exe runs by double-click.
- 🌐 **Cross-platform**: Windows / macOS / Linux.
- 🔌 **Extensible**: adapter-based design — adding a new AI tool means adding one adapter module.
- 🛡️ **Safe**: automatic rollback snapshot before restore; consistent SQLite online-backup snapshots; built-in Zip Slip protection.

## Supported Tools

> Scope: **domestic (China-based) AI coding tools only**. Foreign tools are out of scope (the author cannot access/test them).
> Conversely, domestic tools are usable by overseas users too, so this tool is also valuable internationally.

| Tool | Status |
| --- | --- |
| Qoder | ✅ Supported |
| Other domestic tools (Trae, Doubao, Tongyi Lingma, CodeBuddy, …) | 🚧 Planned — adapters welcome |

## Install

### Option A: Download release (recommended, no Python needed)

Get the single-file build from [Releases](https://github.com/yinlichaoxi007/ai-env-clone/releases):

- Windows: `QoderBackupTool.exe` (or future `ai-env-clone.exe`), double-click to run.
- macOS / Linux: download the executable, `chmod +x` then run.

### Option B: Run from source (Python 3.10+ required)

```bash
git clone https://github.com/yinlichaoxi007/ai-env-clone.git
cd ai-env-clone
python qoder_backup_tool.py        # launch GUI
```

### Option C: Build single-file exe yourself (Windows)

```bash
pip install -r requirements.txt
python build_exe.py                # output in dist/
```

## Usage

### GUI

```bash
python qoder_backup_tool.py
```

Auto-detect data dir → check items → "导出备份" (export) to make a zip, or "还原备份包" (restore) to recover.

On Windows you can also double-click `run.bat` (requires Python 3.10+ installed locally).

### CLI

> The CLI entry is being finalized. The core layer `qoder_backup_core.py` is fully decoupled and callable via `export_backup` / `import_backup` / `inspect_backup`.

```bash
python qoder_backup_tool.py --backup --out ./my-backup.zip
python qoder_backup_tool.py --restore --in ./my-backup.zip
```

(Exact CLI flags follow the released version; see Release Notes.)

### About the backup archive

- A standard `.zip` with a `qoder_backup_manifest.json` (creation time, source dir, modules, file count, …).
- Inspectable via the "查看备份包" feature or by unzipping directly.
- Restore **overwrites** existing files and auto-creates a `qoder_rollback_*.zip` snapshot beforehand, so you can revert anytime.

## Architecture

```
ai_env_clone/
├── core.py            generic core (scan / pack / verify / restore / SQLite snapshot / Zip Slip guard), tool-agnostic
└── adapters/
    ├── base.py        BaseAdapter interface + adapter registry
    └── qoder.py       Qoder adapter (reference implementation)
qoder_backup_core.py   Qoder compat layer (keeps legacy API, delegates to ai_env_clone)
qoder_backup_tool.py   GUI layer (tkinter), interaction & progress
build_exe.py           package into single-file exe via PyInstaller
```

**Multi-tool**: a unified adapter interface (`detect_root()` / `build_items()` / `export()` / `restore()`). Each AI tool maps to one adapter module; adding a tool never touches the main flow. See `docs/CONTRIBUTING.md`.

## Contributing

Issues and PRs are welcome — especially adapters for more **domestic AI tools**.

- Primary repo is **GitHub**; Gitee is a read-only mirror. **Please contribute and file issues on GitHub** (Gitee does not accept PRs).
- Adapter spec in `docs/CONTRIBUTING.md`.

## License

[MIT](./LICENSE) — free to use, modify and distribute, including commercially.

## About this project / 关于本项目

This project was designed and directed by the author, with code and documentation assisted by an AI coding assistant (vibe coding). All design decisions, architecture trade-offs and the release process are controlled by the author. Issues and PRs are welcome on GitHub.

本项目由作者主导设计，代码与文档借助 AI 编程助手（氛围编程 / vibe coding）辅助完成。所有设计决策、架构取舍与发布流程均由作者把控。欢迎在 GitHub 提 Issue / PR。
