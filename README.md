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

> 定位：**支持「国内能正常使用的 AI 编程工具」**。不限定工具的国别或厂商——只要该工具在**国内网络环境下可访问、可测试**，无论其来自国内还是国外产品，都可纳入支持范围。反之，国内工具海外用户同样可用，本工具对海内外用户均有价值。

| 工具 | 形态 | 测试版本 | 状态 |
| --- | --- | --- | --- |
| Qoder CN（前 Lingma，JetBrains 插件） | 桌面 IDE 插件 | **3.3.3** | ✅ 已支持 |
| Qoder CN IDE（独立桌面客户端） | 独立 IDE | **1.10.0** | ✅ 已支持 |
| 其他国内可用工具（如 Trae、豆包、通义灵码、CodeBuddy 等） | — | — | 🚧 规划中，欢迎贡献适配器 |

> ⚠️ **版本说明**：上表仅列出作者**实测通过**的版本。更高/更低版本（如 Qoder CN 插件 3.3.4）未经测试，数据结构可能变化，使用前请先在本机做一次「导出 → 校验」验证。
> Qoder CN 系列产品（原 Lingma 插件、Qoder CN IDE 等）共用同一 `~/.qoder-cn` 根目录，本工具按该目录统一备份，无需用户区分具体产品。

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

- 备份产物是一个标准 `.zip` 文件，文件名形如 `<工具名>_backup_<时间戳>.zip`（例如 `qoder_backup_20260805_095519.zip`）。包内含 `qoder_backup_manifest.json` 清单，记录 `kind`（类型）、`tool`（工具名）、`created_at`（创建时间）、`source_root`（来源目录）、`items`（包含模块）、文件数等。
- 在「备份浏览器」（点「还原备份包」打开）中可查看明细、校验完整性、选择还原。校验结果会显示在列表「完整性」列，切换选择后仍可见。
- 恢复时会**自动覆盖**同名文件，并在覆盖前在工具运行目录下的 `backup/<工具名>/`（例如 `QoderBackupTool.exe` 同级的 `backup/qoder/`，与备份文件同一目录）生成 `<工具名>_rollback_<时间戳>.zip` 回滚快照，可随时还原到恢复前状态，也方便按文件时间信息对比选择。备份默认同样导出到 `backup/<工具名>/`，随工具一起拷贝即可。
- **防误还原**：还原时以包内 manifest 的 `kind` 为准，仅改文件名无法骗过校验；若包内记录的 `source_root` 与当前还原目标不一致，会弹窗二次确认，防止覆盖错误目录的数据。备份与回滚快照均可还原。

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

## 测试

本仓库附带完整的单元测试，使用 Python 标准库 `unittest`，**无需安装任何第三方依赖**。

- **一键运行（Windows）**：双击 `run_tests.bat`，全部测试结果会写入 `test_result.txt` 并在窗口中展示。
- **手动运行**：
  ```bash
  # 运行全部测试（含无头 GUI 测试，不会弹出任何窗口）
  python -m unittest discover -s . -p "test_*.py"

  # 仅运行 GUI 无头测试类
  python -m unittest test_qoder_backup.TestGuiThreadSafety -v
  ```
- **关于 GUI 测试**：GUI 测试基于 tkinter 的 `withdraw()` 实现**无头（headless）运行**，控件、变量与事件回调均可正常创建与触发，不会弹出真实窗口、也不会依赖显示器。消息框（`messagebox`）在测试中被 mock，避免人工点击。真实 GUI 中备份/恢复完成后的成功提示弹窗是正常产品行为，与自动化测试无关。

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

> Scope: **AI coding tools that work in China**. Not limited by the tool's country or vendor — any tool that is reachable and testable under China's network environment (whether domestic or foreign) is in scope. Conversely, domestic tools are usable by overseas users too, so this tool is also valuable internationally.

| Tool | Form | Tested version | Status |
| --- | --- | --- | --- |
| Qoder CN (formerly Lingma, JetBrains plugin) | Desktop IDE plugin | **3.3.3** | ✅ Supported |
| Qoder CN IDE (standalone desktop client) | Standalone IDE | **1.10.0** | ✅ Supported |
| Other China-usable tools (Trae, Doubao, Tongyi Lingma, CodeBuddy, …) | — | — | 🚧 Planned — adapters welcome |

> ⚠️ **Version note**: only author-tested versions are listed above. Untested higher/lower versions (e.g. Qoder CN plugin 3.3.4) may have changed data layouts — do an Export→Verify on your machine first.
> All Qoder CN products (former Lingma plugin, Qoder CN IDE, etc.) share the same `~/.qoder-cn` root directory; the tool backs it up uniformly, so users need not distinguish which product they use.

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

- A standard `.zip` named `<tool>_backup_<timestamp>.zip`, with a `qoder_backup_manifest.json` (kind, tool, creation time, source dir, modules, file count, …).
- Viewable via the "备份浏览器" (open from "还原备份包"): inspect details, verify integrity, and choose what to restore.
- Restore **overwrites** existing files and auto-creates a `<tool>_rollback_<timestamp>.zip` snapshot beforehand, so you can revert anytime. Type is verified against the manifest to prevent accidental restore of a misnamed file.

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

## Testing

The repo ships full unit tests built on the Python stdlib `unittest`, **no third-party dependencies**.

- **One-click (Windows)**: double-click `run_tests.bat`; results are written to `test_result.txt` and shown in the window.
- **Manual**:
  ```bash
  # Run all tests (incl. headless GUI tests — no window pops up)
  python -m unittest discover -s . -p "test_*.py"

  # Run only the headless GUI test class
  python -m unittest test_qoder_backup.TestGuiThreadSafety -v
  ```
- **About GUI tests**: GUI tests run **headless** via tkinter's `withdraw()` — widgets, variables and event callbacks are created and fired normally, with no real window and no display needed. `messagebox` is mocked during tests to avoid manual clicks. The success popup after a real backup/restore in the actual GUI is normal product behavior and unrelated to automated tests.

## Contributing

Issues and PRs are welcome — especially adapters for more **domestic AI tools**.

- Primary repo is **GitHub**; Gitee is a read-only mirror. **Please contribute and file issues on GitHub** (Gitee does not accept PRs).
- Adapter spec in `docs/CONTRIBUTING.md`.

## License

[MIT](./LICENSE) — free to use, modify and distribute, including commercially.

## About this project / 关于本项目

This project was designed and directed by the author, with code and documentation assisted by an AI coding assistant (vibe coding). All design decisions, architecture trade-offs and the release process are controlled by the author. Issues and PRs are welcome on GitHub.

本项目由作者主导设计，代码与文档借助 AI 编程助手（氛围编程 / vibe coding）辅助完成。所有设计决策、架构取舍与发布流程均由作者把控。欢迎在 GitHub 提 Issue / PR。
