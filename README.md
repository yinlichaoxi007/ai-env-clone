# ai-env-clone

> 跨电脑备份与迁移你的 AI 编程环境——AI 开发者的"手机克隆"工具。
> Backup & migrate your AI coding environment across computers. Like a phone transfer tool for AI devs.

[![GitHub](https://img.shields.io/badge/mirror-GitHub-blue)](https://github.com/yinlichaoxi007/ai-env-clone)
[![Gitee](https://img.shields.io/badge/mirror-Gitee-red)](https://gitee.com/yinlichaoxi007/ai-env-clone)

## 简介

`ai-env-clone` 帮助你把本地 AI 编程工具（如 Qoder）积累的**记忆（memory）**、**会话历史（chat history）**、**规则（rules）** 等用户核心数据，打包成一个离线备份文件，方便在新电脑上完整恢复，避免重新训练和丢失上下文。

- 🖥️ **双形态**：同一套核心逻辑，既提供命令行（CLI）也提供图形界面（GUI，基于 Python 自带的 tkinter，无需额外安装）。
- 📦 **零第三方依赖**：运行时不依赖任何第三方库，仅用 Python 标准库。发布版的**单文件 exe 双击即用、无需安装 Python**；从源码运行（`python -m ai_env_clone` 或 `run.bat`）则需本机已装 Python 3.10+。
- 🌐 **跨平台**：Windows / macOS / Linux 均可运行（GUI 在三大平台均可用）。
- 🔌 **可扩展**：以"适配器（adapter）"模式设计，新增一种 AI 工具只需添加一个适配器模块。
- 🛡️ **安全可靠**：恢复前自动生成回滚快照；SQLite 数据库使用在线备份 API 一致性快照；内置 Zip Slip 路径穿越防护。

## 支持的工具

> 定位：**支持「国内能正常使用的 AI 编程工具」**。不限定工具的国别或厂商——只要该工具在**国内网络环境下可访问、可测试**，无论其来自国内还是国外产品，都可纳入支持范围。反之，国内工具海外用户同样可用，本工具对海内外用户均有价值。

| 工具 | 形态 | 测试版本 | 状态 |
| --- | --- | --- | --- |
| Qoder CN（前 Lingma，JetBrains 插件） | 桌面 IDE 插件 | **3.3.3** | ✅ 已支持 |
| Qoder CN IDE（独立桌面客户端） | 独立 IDE | **1.10.0** | ✅ 已支持 |
| CodeBuddy | 桌面 IDE | **4.11.0** | ✅ 已支持 |
| Reasonix | 桌面 IDE | **1.21.5** | ✅ 已支持 |
| 其他国内可用工具 | — | — | 🚧 规划中，欢迎贡献适配器 |

> ⚠️ **版本说明**：上表仅列出作者**实测通过**的版本。其他更高/更低版本未经测试，数据结构可能变化，使用前请先在本机做一次「导出 → 校验」验证。
> 部分工具的不同形态（如桌面 IDE 与对应插件）可能共用同一套数据目录，本工具按各适配器探测到的数据根统一备份，无需用户区分具体产品形态。

## 备份范围与默认勾选策略

本工具统一按以下原则划分「备份内容」的默认勾选状态（各适配器一致，适用于所有已支持的工具）：

- ✅ **默认勾选（推荐项）**：**会话历史、记忆、规则** —— 这些是无法从零重复创建的用户核心数据，丢失即不可逆，故默认勾选，建议连同一起备份。
- ⬜ **默认不勾选（可选）**：**插件、扩展、MCP、skill、灵感、索引、设置** —— 这些属于程序配置或可由工具自身从零重建的内容，默认不勾，按需自行勾选。
- 🚫 **不列入备份选项**：**本地缓存、运行态记录、日志** —— 这些纯属程序自身的临时/运行数据，与用户数据无关，不生成备份条目，无论是否勾选都不会备份。

> 注：不同工具内部目录命名不同（如「设置」可能是 `argv.json`/`config.toml`/项目级 `settings.json`），但均按上述类别归入对应勾选状态。

## 安装

### 方式一：下载发布版（推荐，无需 Python）

到 [Releases](https://github.com/yinlichaoxi007/ai-env-clone/releases) 下载对应平台的单文件程序：

- **Windows (x64)**：`AiEnvClone-windows.exe`，双击运行。
- **macOS（Apple Silicon / Intel 通用）**：`AiEnvClone-macos-*.app.zip`，解压后将 `AiEnvClone.app` 拖入「应用程序」或右键打开。
  - 首次打开若提示「无法验证开发者」，右键 `AiEnvClone.app` →「打开」即可（本程序未购买 Apple 开发者签名证书，属正常提示）。
- **Linux (x64)**：`AiEnvClone-linux`，终端赋予执行权限后运行：
  ```bash
  chmod +x AiEnvClone-linux
  ./AiEnvClone-linux
  ```

> 各平台分发与运行方式均经打包流程验证可产出；**实际运行仅在 Windows x64 上实测过**，macOS / Linux 因暂无对应设备未做端到端实测，但代码路径与打包流程跨平台通用。

### 方式二：从源码运行（需 Python 3.10+）

```bash
git clone https://github.com/yinlichaoxi007/ai-env-clone.git
cd ai-env-clone
python -m ai_env_clone                    # 启动 GUI
```

### 方式三：自行打包各平台可执行程序

```bash
pip install -r requirements.txt
python build_exe.py --name AiEnvClone            # 在当前平台产出对应格式的可执行程序，位于 dist/
```

> 在 Windows 上产出 `AiEnvClone.exe`，macOS 上产出 `AiEnvClone.app`，Linux 上产出 `AiEnvClone`（无后缀）。CI 中的 `build-release.yml` 即在三平台（Windows / macOS / Linux）分别调用此脚本并汇总发布（macOS 按 arm64 与 x86_64 各产一份）。

- **Windows 一键打包**：双击仓库内的 `build.bat` 即可（自动检查 Python → 安装 `requirements.txt` 依赖 → 调用 `build_exe.py`，产物位于 `dist/AiEnvClone.exe`）。该 exe 可拷贝到任何无 Python 的 Windows 电脑双击运行。

## 使用

### GUI（图形界面）

```bash
python -m ai_env_clone
```

启动后：自动检测数据目录 → 勾选要备份的内容 → 点击「导出备份」生成 zip，或「还原备份包」恢复。

GUI 界面要点：

- **数据根识别状态区**：位于「数据目录」与「当前用户」之间，自动列出该工具在用户主目录下识别到的各个数据根目录（含完整相对路径），未找到的根以灰显 `✗` 标注；若完全未识别到数据目录会提示需手动指定。该区域最多显示约两行，根目录过多时显示竖向滚动条，避免把主窗口整体高度撑高。
- **备份内容路径可见**：每个备份项的说明文字后附带其相对于数据目录的具体路径，方便确认备份范围。
- **未找到项标红**：当数据目录未正确识别时，备份内容中找不到的每一项会标红并注明「（未找到）」，备份内容区右上角同时显示「N 项未找到」；已勾选项保持不变，仅作提示，不会自动取消勾选。
- **估算大小**：点击「估算大小」按钮可预估所选备份项打包后的体积。
- **高分屏适配**：窗口声明 DPI 感知（Per-Monitor v2），缩放系数超过 100% 时不会被系统虚化放大；备份内容区高度统一为固定值（与工具/项数无关），切换 Qoder / CodeBuddy / Reasonix 时内容区高度完全一致，内容过多时由滚动条承载。主窗口高度**自适应内容**（上限屏幕高度 92%、下限 460px），内容少的工具（如 Qoder）窗口更紧凑，底部不再有大面积空白。

也可双击仓库内的 `run.bat`（Windows，需本机已装 Python 3.10+）一键启动。

### CLI（命令行）

> 当前 CLI 入口正在完善中，核心逻辑层 `ai_env_clone.core` 已完全解耦，可独立调用 `export_backup` / `import_backup` / `inspect_backup`。

```bash
# 备份
python -m ai_env_clone --backup --out ./my-backup.zip

# 恢复
python -m ai_env_clone --restore --in ./my-backup.zip
```

（具体 CLI 参数以发布版本为准，请关注 Release Notes。）

### 备份包说明

- 备份产物是一个标准 `.zip` 文件，文件名形如 `<工具名>_backup_<时间戳>.zip`（例如 `qoder_backup_20260805_095519.zip`）。包内含 `qoder_backup_manifest.json` 清单，记录 `kind`（类型）、`tool`（工具名）、`created_at`（创建时间）、`source_root`（来源目录）、`items`（包含模块）、文件数等。
- 在「备份浏览器」（点「还原备份包」打开）中可查看明细、校验完整性、选择还原。校验结果会显示在列表「完整性」列，切换选择后仍可见。
- 恢复时会**自动覆盖**同名文件，并在覆盖前在备份目录下的 `backup/<工具名>/` 生成 `<工具名>_rollback_<时间戳>.zip` 回滚快照，可随时还原到恢复前状态，也方便按文件时间信息对比选择。备份默认同样导出到该 `backup/<工具名>/`。
- **备份目录位置**：与启动方式同级。
  - 源码模式（`python -m ai_env_clone` / `run.bat`）：`<仓库根>/backup/<工具名>/`。
  - 打包模式（单文件 exe / app / 二进制）：可执行程序是独立分发物，备份目录放在 **exe 同级**的 `backup/<工具名>/`（如 `dist/backup/qoder/`），让程序与它的备份数据在一起，便于随程序一起拷贝/迁移。重打包（`build_exe.py` / `build.bat`）只会覆盖 exe 本身，不会清空 `backup/` 子目录，备份数据安全。
- **防误还原**：还原时以包内 manifest 的 `kind` 为准，仅改文件名无法骗过校验；若包内记录的 `source_root` 与当前还原目标不一致，会弹窗二次确认，防止覆盖错误目录的数据。备份与回滚快照均可还原。

### 跨电脑还原：CodeBuddy 会话 UUID 自动重映射

CodeBuddy 的集中会话/检查点按**登录用户 UUID** 分目录存放（`CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/`）。备份把该 UUID 固化进归档相对路径，直接按原路径还原到新电脑会写进一个**当前登录用户读不到的「死目录」**——表现为「历史会话列表看得到、点开却没内容」。

还原时本工具会自动把旧 UUID 重映射为本机**当前登录用户 UUID**（启发式取 `Data` 下最近活动的 UID），确保会话落到本机 CodeBuddy 实际读取的目录。若本机从未登录过 CodeBuddy（取不到 UUID），则保持原路径、不做重写，至少不破坏备份。

### 深层会话消息长路径修复（Windows）

CodeBuddy 会话消息文件层级深（`history/<ws>/<sid>/messages/<id>.json`），其绝对路径常超过 Windows 的 **260 字符 MAX_PATH** 限制。旧版在扫描/`getsize`/`open` 时因 `WinError 3`（系统找不到指定的路径）**静默丢弃全部 `messages/` 文件**——表现为「历史会话列表看得到、点开却没内容」。**当前版本已对所有文件操作加 `\\?\` 长路径前缀**（`scan_items`、`export_backup`、`import_backup` 全程），深层会话消息可完整备份与还原。

## 架构

```
ai_env_clone/                包（import 名 ai_env_clone，产品名 AiEnvClone）
├── __init__.py        包初始化与 __version__
├── __main__.py        图形界面层（tkinter），统一入口，负责交互与进度展示
├── core.py            通用核心层（扫描/打包/校验/恢复/SQLite快照/ZipSlip防护），与具体工具解耦
├── compress_estimate.py  压缩体积预估（经验系数 + 可校准缓存）
├── adapters/
│   ├── base.py        BaseAdapter 抽象接口 + 适配器注册表
│   ├── qoder.py       Qoder 适配器（参考实现，自包含）
│   └── codebuddy.py   CodeBuddy 适配器（用户级/全局数据，公共根为用户主目录）
│   └── reasonix.py    Reasonix 适配器（配置/数据在 AppData/Roaming/reasonix，缓存在 AppData/Local/reasonix）
└── backup/            备份/恢复执行与回滚快照
build_exe.py           用 PyInstaller 跨平台打包（Windows / macOS arm64 / macOS x86_64 / Linux 可执行程序）
.github/workflows/     build-release.yml（打 tag 自动构建多平台可执行程序并发布 Release）+ mirror-to-gitee.yml（镜像到 Gitee）
```

**多工具扩展**：已采用统一的适配器接口（`detect_root()` / `detect_data_roots()` / `build_items()` / `export()` / `restore()`）。每种 AI 工具对应一个适配器模块，新增工具无需改动主流程，详见 `docs/CONTRIBUTING.md`。

## 支持的平台与架构

| 平台 | 架构 | 分发格式 | 编译/打包 | 运行实测 |
| --- | --- | --- | --- | --- |
| Windows | x64 | `AiEnvClone-windows.exe`（单文件） | ✅ CI 自动构建 | ✅ 已实测 |
| macOS | Apple Silicon / Intel（arm64 + x86_64） | `AiEnvClone-macos-*.app.zip` | ✅ CI 自动构建 | ⚠️ 暂无设备，未实测 |
| Linux | x64 | `AiEnvClone-linux`（单文件） | ✅ CI 自动构建 | ⚠️ 暂无设备，未实测 |

- **编译与打包**：三个平台的产物均由 GitHub Actions 在对应系统（Windows / macOS / Ubuntu）上由 PyInstaller 跨平台打包产出，流程已验证可正常产出。
- **运行实测**：目前仅在 **Windows x64** 上做过完整端到端运行验证；macOS 与 Linux 因暂无对应设备，未做真机实测。代码路径（路径解析、缓存目录、适配器检测均使用 `os.path.expanduser("~")` 等跨平台写法）与打包流程本身跨平台通用，但首次在这些平台上运行如遇问题，欢迎反馈 Issue。
- **适配器（被备份的工具）的平台支持**：取决于各工具自身提供的平台。例如 CatPaw 目前仅提供 Windows / macOS，无 Linux 版，因此在 Linux 上该适配器会检测不到数据目录而跳过；这不影响本工具在 Linux 上备份其它已支持的工具。

## 测试

本仓库附带完整的单元测试，使用 Python 标准库 `unittest`，**无需安装任何第三方依赖**。

- **一键运行（Windows）**：双击 `run_tests.bat`，全部测试结果会写入 `test_result.txt` 并在窗口中展示。
- **手动运行**：
  ```bash
  # 运行全部测试（含无头 GUI 测试，不会弹出任何窗口）
  python -m unittest discover -s tests -p "test_*.py"

  # 仅运行 GUI 无头测试类
  python -m unittest tests.test_qoder.TestGuiThreadSafety -v
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

`ai-env-clone` helps you package the **memory**, **chat history** and **rules** accumulated by your local AI coding tools (e.g. Qoder) — the user's core, irreproducible data — into an offline backup archive, so you can fully restore them on a new machine without retraining or losing context.

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
| CodeBuddy | Desktop IDE | **4.11.0** | ✅ Supported |
| Reasonix | Desktop IDE | **1.21.5** | ✅ Supported |
| Other China-usable tools | — | — | 🚧 Planned — adapters welcome |

> ⚠️ **Version note**: only author-tested versions are listed above. Untested higher/lower versions may have changed data layouts — do an Export→Verify on your machine first.
> Some tools may ship multiple forms (e.g. a desktop IDE and its plugin) that share one data directory; each adapter backs up whatever data roots it detects, so users need not distinguish product forms.

## Backup Scope & Default Selection Policy

All adapters follow the same policy for which backup items are checked by default:

- ✅ **Checked by default (recommended)**: **chat history, memory, rules** — user core data that cannot be recreated from scratch; losing it is irreversible, so these are checked and recommended for backup.
- ⬜ **Unchecked by default (optional)**: **plugins, extensions, MCP, skills, inspiration, index, settings** — program configuration or content that the tool can rebuild from scratch; unchecked by default, tick as needed.
- 🚫 **Not listed as a backup item**: **local cache, runtime/state records, logs** — purely the program's own temporary/runtime data, unrelated to user data; no item is generated and it is never backed up regardless of selection.

> Note: different tools name their directories differently (e.g. "settings" may be `argv.json` / `config.toml` / project-level `settings.json`), but each is mapped to the appropriate selection state by category above.

## Supported Platforms & Architectures

| Platform | Arch | Distribution | Built by CI | Runtime tested |
| --- | --- | --- | --- | --- |
| Windows | x64 | `AiEnvClone-windows.exe` (single-file) | ✅ automated | ✅ verified |
| macOS | Apple Silicon / Intel (arm64 + x86_64) | `AiEnvClone-macos-*.app.zip` | ✅ automated | ⚠️ no device, untested |
| Linux | x64 | `AiEnvClone-linux` (single-file) | ✅ automated | ⚠️ no device, untested |

- **Build & packaging**: all three platform artifacts are produced by GitHub Actions on their native OS (Windows / macOS / Ubuntu) via cross-platform PyInstaller; the flow is verified to produce valid outputs.
- **Runtime tested**: full end-to-end runtime is verified only on **Windows x64** so far. macOS and Linux are not yet exercised on real hardware (none available). The code paths (path resolution, cache dir, adapter detection all use cross-platform `os.path.expanduser("~")`) and the packaging flow are cross-platform by design, but if you hit issues on those platforms, please file an Issue.
- **Platform support of backed-up tools**: depends on each tool's own offerings. For example CatPaw currently ships Windows / macOS only (no Linux build), so on Linux its adapter will simply detect no data dir and skip — this does not affect backing up other supported tools on Linux.

## Install

### Option A: Download release (recommended, no Python needed)

Get the single-file build from [Releases](https://github.com/yinlichaoxi007/ai-env-clone/releases):

- **Windows (x64)**: `AiEnvClone-windows.exe`, double-click to run.
- **macOS (Apple Silicon / Intel)**: `AiEnvClone-macos-*.app.zip` — unzip, then drag `AiEnvClone.app` to Applications or right-click → Open. On first launch macOS may say "cannot verify developer" (this app is not Apple-signed); right-click the app → Open to bypass.
- **Linux (x64)**: `AiEnvClone-linux` — make it executable and run:
  ```bash
  chmod +x AiEnvClone-linux
  ./AiEnvClone-linux
  ```

> Distribution and launch steps for every platform are validated by the packaging flow. **End-to-end runtime is verified only on Windows x64**; macOS / Linux are not yet tested on real devices (no hardware available), but the code paths and packaging are cross-platform by design.

### Option B: Run from source (Python 3.10+ required)

```bash
git clone https://github.com/yinlichaoxi007/ai-env-clone.git
cd ai-env-clone
python -m ai_env_clone                    # launch GUI
```

### Option C: Build per-platform executables yourself

```bash
pip install -r requirements.txt
python build_exe.py --name AiEnvClone            # produces the platform-native executable in dist/
```

> On Windows it produces `AiEnvClone.exe`, on macOS `AiEnvClone.app`, on Linux `AiEnvClone` (no suffix). The CI `build-release.yml` invokes this script on three platforms (Windows / macOS / Linux) and publishes the results (macOS yields both arm64 and x86_64 builds).

- **Windows one-click build**: double-click `build.bat` in the repo (auto-checks Python → installs `requirements.txt` deps → runs `build_exe.py`; output at `dist/AiEnvClone.exe`). Copy that exe to any Windows PC without Python and run it directly.

## Usage

### GUI

```bash
python -m ai_env_clone
```

Auto-detect data dir → check items → "导出备份" (export) to make a zip, or "还原备份包" (restore) to recover.

GUI highlights:

- **Data-root detection status**: shown between the data-directory field and the current-user selector; lists each detected data root under the user home (with its relative path). Missing roots are marked with a greyed `✗`; if nothing is detected, the tool prompts you to specify the directory manually. The area shows at most ~2 rows; when more roots are detected a vertical scrollbar appears, so the main window height is not pushed up.
- **Backup-item paths visible**: each backup item shows its concrete relative path after its description, so you can confirm the backup scope.
- **Missing items highlighted**: when the data directory is not correctly detected, every item that cannot be found is shown in red and labelled "(未找到 / not found)"; the top-right of the backup list also shows "N 项未找到" (N items not found). Already-checked items keep their state — only a hint, no auto-uncheck.
- **Estimate size**: click "估算大小" (estimate size) to preview the packed size of selected items.
- **Hi-DPI support**: the window declares DPI awareness (Per-Monitor v2), so it is not blurry-scaled by the OS when the scaling factor exceeds 100%. The backup content area uses a fixed uniform height (independent of tool/item count), so switching Qoder / CodeBuddy / Reasonix keeps the content area height identical; when content overflows a scrollbar appears. The **main window height is adaptive** — it fits the content (capped at 92% of screen height, floored at 460px), so tools with less content (e.g. Qoder) get a more compact window with no large blank gap at the bottom.

On Windows you can also double-click `run.bat` (requires Python 3.10+ installed locally).

### CLI

> The CLI entry is being finalized. The core layer `ai_env_clone.core` is fully decoupled and callable via `export_backup` / `import_backup` / `inspect_backup`.

```bash
python -m ai_env_clone --backup --out ./my-backup.zip
python -m ai_env_clone --restore --in ./my-backup.zip
```

(Exact CLI flags follow the released version; see Release Notes.)

### About the backup archive

- A standard `.zip` named `<tool>_backup_<timestamp>.zip`, with a `qoder_backup_manifest.json` (kind, tool, creation time, source dir, modules, file count, …).
- Viewable via the "备份浏览器" (open from "还原备份包"): inspect details, verify integrity, and choose what to restore.
- Restore **overwrites** existing files and auto-creates a `<tool>_rollback_<timestamp>.zip` snapshot beforehand in the same `backup/<tool>/` directory, so you can revert anytime. Type is verified against the manifest to prevent accidental restore of a misnamed file.
- **Backup directory location**: next to the launch method. Source mode (`python -m ai_env_clone` / `run.bat`) → `<repo root>/backup/<tool>/`. Packaged mode (single-file exe / app / binary) → the executable is a standalone distributable, so backups go to `backup/<tool>/` **next to the exe** (e.g. `dist/backup/qoder/`), keeping the program and its data together. Re-packaging (`build_exe.py` / `build.bat`) only overwrites the exe itself and never clears the `backup/` subfolder, so backups are safe.

### Cross-computer restore: CodeBuddy session UUID auto-remap

CodeBuddy stores its centralized sessions/checkpoints under the **logged-in user UUID** (`CodeBuddyExtension/Data/<uuid>/CodeBuddyIDE/<uuid>/`). The backup pins this UUID into the archive's relative paths; restoring them verbatim on a new computer would write into a "dead directory" the current user cannot read — showing up as "session list visible, but empty when opened".

On restore, the tool auto-remaps the old UUID to the **current logged-in user UUID** on this machine (heuristically the most recently active UID under `Data`), so sessions land where this machine's CodeBuddy actually reads them. If CodeBuddy has never been logged into on this machine (no UUID found), paths are left unchanged rather than broken.

### Deep session message long-path fix (Windows)

CodeBuddy session message files are deeply nested (`history/<ws>/<sid>/messages/<id>.json`) and their absolute paths often exceed Windows' **260-char MAX_PATH** limit. Older versions silently dropped every `messages/` file because `getsize`/`open` raised `WinError 3` (path not found) — showing up as "session list visible, but empty when opened". **The current build adds the `\\?\` long-path prefix to all file operations** (`scan_items`, `export_backup`, `import_backup`), so deep session messages are backed up and restored completely.

## Architecture

```
ai_env_clone/                package (import name ai_env_clone, product name AiEnvClone)
├── __init__.py        package init & __version__
├── __main__.py        GUI layer (tkinter), unified entry point, interaction & progress
├── core.py            generic core (scan / pack / verify / restore / SQLite snapshot / Zip Slip guard), tool-agnostic
├── compress_estimate.py  compressed-size estimation (empirical ratios + calibratable cache)
├── adapters/
│   ├── base.py        BaseAdapter interface + adapter registry
│   ├── qoder.py       Qoder adapter (reference implementation, self-contained)
│   └── codebuddy.py   CodeBuddy adapter (user-level & global data, common root = user home)
│   └── reasonix.py    Reasonix adapter (config/data under AppData/Roaming/reasonix, cache under AppData/Local/reasonix)
└── backup/            backup/restore execution & rollback snapshots
build_exe.py           package into cross-platform executables via PyInstaller (Windows / macOS arm64 / macOS x86_64 / Linux)
.github/workflows/     build-release.yml (tag → auto-build multi-platform binaries & publish Release) + mirror-to-gitee.yml (mirror to Gitee)
```

**Multi-tool**: a unified adapter interface (`detect_root()` / `detect_data_roots()` / `build_items()` / `export()` / `restore()`). Each AI tool maps to one adapter module; adding a tool never touches the main flow. See `docs/CONTRIBUTING.md`.

## Testing

The repo ships full unit tests built on the Python stdlib `unittest`, **no third-party dependencies**.

- **One-click (Windows)**: double-click `run_tests.bat`; results are written to `test_result.txt` and shown in the window.
- **Manual**:
  ```bash
  # Run all tests (incl. headless GUI tests — no window pops up)
  python -m unittest discover -s tests -p "test_*.py"

  # Run only the headless GUI test class
  python -m unittest tests.test_qoder.TestGuiThreadSafety -v
  ```
- **About GUI tests**: GUI tests run **truly headless** — the root window is `withdraw()`-ed, and the backup-browser Toplevel opened by the import flow is also hidden in headless mode, so **no window ever pops up during a test run**. Layout measurement temporarily moves the (invisible) window off-screen (`+4000+4000`) and drives a real geometry pass without showing anything. Widgets, variables and event callbacks are created and fired normally; `messagebox` is mocked to avoid manual clicks. The success popup after a real backup/restore in the actual GUI is normal product behavior and unrelated to automated tests.

## Contributing

Issues and PRs are welcome — especially adapters for more **domestic AI tools**.

- Primary repo is **GitHub**; Gitee is a read-only mirror. **Please contribute and file issues on GitHub** (Gitee does not accept PRs).
- Adapter spec in `docs/CONTRIBUTING.md`.

## License

[MIT](./LICENSE) — free to use, modify and distribute, including commercially.

## About this project / 关于本项目

This project was designed and directed by the author, with code and documentation assisted by an AI coding assistant (vibe coding). All design decisions, architecture trade-offs and the release process are controlled by the author. Issues and PRs are welcome on GitHub.

本项目由作者主导设计，代码与文档借助 AI 编程助手（氛围编程 / vibe coding）辅助完成。所有设计决策、架构取舍与发布流程均由作者把控。欢迎在 GitHub 提 Issue / PR。
