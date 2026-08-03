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
python -m unittest test_qoder_backup -v
```

新增适配器后，请补充对应的 `build_items` 单元断言（参考 `test_qoder_backup.py` 中
`TestDetect` / `TestScan` 的写法），确认路径探测与条目清单符合预期。

---

## 6. PR 规范

- 一个工具一个适配器文件，命名 `ai_env_clone/adapters/<tool>.py`。
- 提交信息说明工具名称与支持范围（记忆 / 历史 / 配置 / 索引等）。
- 在 README「支持的工具」表格把状态从 🚧 改为 ✅，并补充该工具数据目录说明。
- 保持**零运行时第三方依赖**：适配器与核心层只能使用 Python 标准库。
