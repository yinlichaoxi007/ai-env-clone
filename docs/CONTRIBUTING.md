# 贡献指南（适配器开发）

`ai-env-clone` 以**适配器（adapter）**模式支持多种国内 AI 编程工具。新增一种工具，
只需新增一个适配器模块，**无需改动 CLI / GUI / 核心逻辑**。

> 仓库主站为 GitHub，Gitee 为只读镜像。**请到 GitHub 提交 Issue 与 Pull Request。**
> https://github.com/yinlichaoxi007/ai-env-clone

---

## 1. 目录结构

```
ai_env_clone/
├── core.py            # 通用逻辑：扫描/打包/校验/恢复/SQLite快照/ZipSlip防护
└── adapters/
    ├── base.py        # BaseAdapter 抽象接口 + 注册表
    └── qoder.py       # Qoder 适配器（参考实现）
```

核心层（`core.py`）与"具体工具"完全解耦，它只认通用的 `BackupItem`：

```python
@dataclass
class BackupItem:
    key: str            # 唯一键，如 "memories"
    label: str          # 界面展示名
    path: str           # 绝对路径（文件或目录）
    description: str    # 说明
    recommended: bool = True
```

---

## 2. 适配器接口

继承 `BaseAdapter`，实现三个抽象方法，并用 `@register` 装饰器注册：

```python
from ai_env_clone.adapters.base import BaseAdapter, register


@register
class MyToolAdapter(BaseAdapter):
    name = "mytool"                 # 工具唯一标识（小写、无空格），写入备份清单
    display_name = "MyTool"         # 界面展示名

    def detect_root(self) -> str | None:
        """探测本机数据根目录；找不到返回 None。"""
        import os
        cand = os.path.expanduser("~/.mytool")
        return cand if os.path.isdir(cand) else None

    def build_default_root(self) -> str:
        """未探测到时的默认（建议）数据目录。"""
        import os
        return os.path.expanduser("~/.mytool")

    def build_items(self, root: str) -> list[BackupItem]:
        """根据根目录构造可备份条目清单。"""
        from ai_env_clone.core import BackupItem
        return [
            BackupItem(
                key="memories",
                label="记忆区",
                path=os.path.join(root, "memories"),
                description="长/短期记忆数据。",
                recommended=True,
            ),
            # ... 更多条目
        ]
```

通用方法（`export` / `inspect` / `restore`）已由基类基于 `core.py` 实现，通常**无需重写**。

---

## 3. 注册与发现

- `@register` 会把 `name` 注册进全局表。
- 主程序通过 `get_adapter("mytool")` 获取实例，`list_adapters()` 列出全部。
- 把新文件放到 `ai_env_clone/adapters/mytool.py` 即可，包导入时自动触发注册。

---

## 4. 条目设计要点

1. **归档路径相对根目录**：`BackupItem.path` 必须在 `root` 之下，`scan_items` 会自动用
   `os.path.relpath` 计算归档内相对路径，保证"备份什么路径，就恢复到什么路径"。
2. **关键 SQLite 不被体积上限裁剪**：核心层 `ALWAYS_INCLUDE` 已豁免 `*/cache/db/*.db`
   及其 `-wal`/`-shm`。若你的工具数据库在其他路径，请在 `build_items` 里把对应条目设为
   `recommended=True` 或扩展 `core.ALWAYS_INCLUDE`。
3. **噪音过滤**：核心层默认排除 `*.zip` `*.7z` `*.log` `*- 副本*` `*.tmp` 等。
   若需额外排除规则，可在 `export_backup(..., excludes=(...))` 传入。
4. **一致性快照**：条目指向的 SQLite 文件在打包时用在线备份 API 做一致性快照，
   工具运行中也可安全备份，无需停机。

---

## 5. 本地验证

```bash
# 导入并检查适配器注册
python -c "from ai_env_clone.adapters import list_adapters; print(list_adapters())"

# 跑全部测试（含核心逻辑回归）
python -m unittest tests.test_qoder -v
```

新增适配器后，请补充对应的 `build_items` 单元断言（参考 `tests/test_qoder.py` 中
`TestDetect` / `TestScan` 的写法），确认路径探测与条目清单符合预期。

---

## 6. PR 规范

- 一个工具一个适配器文件，命名 `ai_env_clone/adapters/<tool>.py`。
- 提交信息说明工具名称与支持范围（记忆 / 历史 / 配置 / 索引等）。
- 在 README「支持的工具」表格把状态从 🚧 改为 ✅，并补充该工具数据目录说明。
- 保持**零运行时第三方依赖**：适配器与核心层只能使用 Python 标准库。

---

## 7. 发布流程（维护者参考，普通贡献者无需关心）

版本发布由 GitHub Actions 自动完成，**无需手动在网页上传编译产物**。规则如下：

### Tag 命名约定

| Tag 形如        | 含义           | 触发动作                                              |
| --------------- | -------------- | ----------------------------------------------------- |
| `v1.2.3`        | 正式版         | 构建多平台可执行程序并发布 GitHub Release（正式 Release）          |
| `beta1` `rc2` `alpha1` | 预发布 | 构建多平台可执行程序并发布 GitHub Release（标记为 pre-release）    |

> 本工具是**面向终端用户的 GUI 程序**；分发靠 GitHub Releases 的
> 多平台可执行程序（Windows / macOS arm64 / macOS x86_64 / Linux）+ 自动附带的源码包。

### 发布步骤

```bash
# 1) 在本地打 tag（版本号与 ai_env_clone/__init__.py 的 __version__ 保持一致）
git tag v1.0.0
git push origin v1.0.0
```

推送后 `build-release.yml` 自动运行（无需手动点 Release 按钮）：

- **`build-release.yml`** 用 matrix 一次性构建四个平台的可执行程序并汇总发布：
  - `windows-latest` → `AiEnvClone-windows.exe`
  - `macos-latest`（Apple Silicon / arm64）→ `AiEnvClone-macos-arm64.app.zip`
  - `macos-13`（Intel / x86_64）→ `AiEnvClone-macos-x86_64.app.zip`
  - `ubuntu-latest` → `AiEnvClone-linux`（直接使用 runner 自带 python3，已含 tkinter）
  - 上述产物全部作为 GitHub Release 资产上传；
  - GitHub 会**自动附上** `Source code (zip)` / `Source code (tar.gz)` 源码包，无需手动传；
  - `v*` 为正式 Release，`beta/rc/alpha*` 自动标记为预发布；
  - Release 说明由 `generate_release_notes` 自动汇总该 tag 的提交生成。

### 手动触发

Actions 页面可手动运行 `build-release.yml`（填已存在的 tag 名），用于补发或重试。

### 追加资产

日常给某 Release 追加额外附件（文档、校验和等），**直接在 GitHub Release 页面手动上传即可**，
无需修改 workflow；只有"需要由 Actions 自动构建的新资产"才需扩展 `build-release.yml`。

### 版本号同步

每次发正式版前，请更新 `ai_env_clone/__init__.py` 中的 `__version__`，与 tag 保持一致。

### 镜像

代码推送到 `main` 后，`mirror-to-gitee.yml` 会自动把仓库镜像到 Gitee（只读镜像），无需干预。

---

# Contributing Guide (Adapter Development) — English

`ai-env-clone` supports multiple domestic (China-based) AI coding tools via an **adapter** pattern.
Adding a new tool only requires adding one adapter module — **no changes to the CLI / GUI / core logic**.

> The primary repo is GitHub; Gitee is a read-only mirror. **Please file Issues and Pull Requests on GitHub.**
> https://github.com/yinlichaoxi007/ai-env-clone

---

## 1. Directory Structure

```
ai_env_clone/
├── core.py            # Common logic: scan / pack / verify / restore / SQLite snapshot / Zip Slip guard
└── adapters/
    ├── base.py        # BaseAdapter abstract interface + registry
    └── qoder.py       # Qoder adapter (reference implementation)
```

The core layer (`core.py`) is fully decoupled from any specific tool; it only knows the generic `BackupItem`:

```python
@dataclass
class BackupItem:
    key: str            # unique key, e.g. "memories"
    label: str          # display name in the UI
    path: str           # absolute path (file or directory)
    description: str    # description
    recommended: bool = True
```

---

## 2. Adapter Interface

Subclass `BaseAdapter`, implement the three abstract methods, and register with the `@register` decorator:

```python
from ai_env_clone.adapters.base import BaseAdapter, register


@register
class MyToolAdapter(BaseAdapter):
    name = "mytool"                 # unique tool id (lowercase, no spaces), written to the backup manifest
    display_name = "MyTool"         # display name in the UI

    def detect_root(self) -> str | None:
        """Detect this machine's data root directory; return None if not found."""
        import os
        cand = os.path.expanduser("~/.mytool")
        return cand if os.path.isdir(cand) else None

    def build_default_root(self) -> str:
        """Default (suggested) data directory when detection fails."""
        import os
        return os.path.expanduser("~/.mytool")

    def build_items(self, root: str) -> list[BackupItem]:
        """Build the list of backup items from the root directory."""
        from ai_env_clone.core import BackupItem
        return [
            BackupItem(
                key="memories",
                label="Memory",
                path=os.path.join(root, "memories"),
                description="Long/short-term memory data.",
                recommended=True,
            ),
            # ... more items
        ]
```

The common methods (`export` / `inspect` / `restore`) are already implemented by the base class on top of `core.py` and usually **do not need to be overridden**.

---

## 3. Registration & Discovery

- `@register` registers `name` in the global table.
- The main program fetches an instance via `get_adapter("mytool")` and lists all with `list_adapters()`.
- Just drop the new file at `ai_env_clone/adapters/mytool.py`; registration is triggered automatically on package import.

---

## 4. Item Design Notes

1. **Archive paths are relative to the root**: `BackupItem.path` must sit under `root`; `scan_items` computes the in-archive relative path with `os.path.relpath`, guaranteeing "backup what path → restore to what path".
2. **Critical SQLite is exempt from size caps**: the core layer `ALWAYS_INCLUDE` already exempts `*/cache/db/*.db` and its `-wal`/`-shm`. If your tool's database lives elsewhere, set the corresponding item `recommended=True` or extend `core.ALWAYS_INCLUDE`.
3. **Noise filtering**: the core layer excludes `*.zip` `*.7z` `*.log` `*- 副本*` `*.tmp`, etc. by default. Pass extra rules via `export_backup(..., excludes=(...))` if needed.
4. **Consistent snapshots**: SQLite files referenced by items are snapshotted consistently at pack time using the online-backup API, so backups are safe even while the tool is running — no downtime required.

---

## 5. Local Verification

```bash
# Import and check adapter registration
python -c "from ai_env_clone.adapters import list_adapters; print(list_adapters())"

# Run all tests (incl. core logic regression)
python -m unittest tests.test_qoder -v
```

After adding an adapter, please add corresponding `build_items` unit assertions (see the `TestDetect` / `TestScan` style in `tests/test_qoder.py`) to confirm path detection and item lists behave as expected.

---

## 6. PR Guidelines

- One tool per adapter file, named `ai_env_clone/adapters/<tool>.py`.
- Commit messages should state the tool name and supported scope (memory / history / config / index, etc.).
- In the README "Supported Tools" table, change the status from 🚧 to ✅ and add the tool's data-directory notes.
- Keep **zero runtime third-party dependencies**: adapters and the core layer may only use the Python standard library.

---

## 7. Release Process (Maintainer Reference — not needed by regular contributors)

Releases are automated by GitHub Actions — **no manual upload of build artifacts on the web**. Rules:

### Tag naming

| Tag like        | Meaning        | Triggered action                                       |
| --------------- | -------------- | ------------------------------------------------------ |
| `v1.2.3`        | Stable         | Build exe and publish a GitHub Release (stable)        |
| `beta1` `rc2` `alpha1` | Pre-release | Build exe and publish a GitHub Release (marked pre-release) |

> This tool is an **end-user GUI program**; distribution relies on the
> multi-platform executables on GitHub Releases (Windows / macOS arm64 / macOS x86_64 / Linux) plus the auto-attached source archives.

### Release steps

```bash
# 1) Tag locally (version must match __version__ in ai_env_clone/__init__.py)
git tag v1.0.0
git push origin v1.0.0
```

After pushing, `build-release.yml` runs automatically (no need to click the Release button):

- **`build-release.yml`** builds all four platform binaries in one matrix run and publishes them together:
  - `windows-latest` → `AiEnvClone-windows.exe`
  - `macos-latest` (Apple Silicon / arm64) → `AiEnvClone-macos-arm64.app.zip`
  - `macos-13` (Intel / x86_64) → `AiEnvClone-macos-x86_64.app.zip`
  - `ubuntu-latest` → `AiEnvClone-linux` (uses the runner's system python3, which already ships tkinter)
  - all of the above are uploaded as GitHub Release assets;
  - GitHub **auto-attaches** `Source code (zip)` / `Source code (tar.gz)` archives, no manual upload;
  - `v*` becomes a stable Release; `beta/rc/alpha*` is auto-marked pre-release;
  - Release notes are auto-generated by `generate_release_notes` summarizing commits for that tag.

### Manual trigger

You can manually run `build-release.yml` from the Actions page (fill in an existing tag name) to re-publish or retry.

### Adding assets

To attach extra files (docs, checksums, etc.) to a Release, **upload them directly on the GitHub Release page** —
no workflow change needed. Only "new assets that must be built by Actions" require extending `build-release.yml`.

### Version sync

Before each stable release, update `__version__` in `ai_env_clone/__init__.py` to match the tag.

### Mirroring

After code is pushed to `main`, `mirror-to-gitee.yml` automatically mirrors the repo to Gitee (read-only mirror), no intervention needed.
