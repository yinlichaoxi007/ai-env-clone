"""提权重试（方案 6.1.1）：**只在 frozen Windows** 提供用户显式点击的提权路径。

独立成模块（而不留在 ``updater.py``）是为了**打桩单测**：真实调用 ``ShellExecuteW``
会弹 UAC，测试绝不走那条路——所有系统调用都收在模块级小函数里，测试 monkeypatch
它们即可覆盖全部判定逻辑。

三条硬边界（方案 6.4 的「不静默提权、不越权做事」）：

1. 提权**只能**由用户在对话框里显式点出来，本模块不提供任何静默提权入口；
2. 提权后的新进程**只做**「校验 → 替换 → 重启」这一件事（``--resume-update``），
   不启动 GUI、不接管其它操作；
3. 提权是「同一用户 + 提升后的令牌」，不是切换账户——用户目录 / 偏好文件保持一致。

**源码模式不提供提权按钮**（方案 6.1.1 边界一）：``sys.executable`` 是 python.exe，
重建出的命令没有意义，且源码目录几乎不在受限位置，提权收益极低。
"""

from __future__ import annotations

import sys

__all__ = [
    "is_frozen",
    "is_user_admin",
    "resume_command",
    "request_elevation",
]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里。"""
    return bool(getattr(sys, "frozen", False))


def is_user_admin() -> bool:
    """当前进程是否已处于提权上下文；非 Windows 一律 ``False``。

    ★「已提权却仍不可写」说明不是权限问题（只读卷 / 组策略 / 杀软锁定），
    调用方必须据此**不显示**提权按钮、只给手动路径（方案 6.1.1 边界表 ①）。
    """
    return _is_user_admin_impl()


def _is_user_admin_impl() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - 探测失败按「未提权」处理，只影响按钮显隐
        return False


def resume_command(staged_path: str) -> "tuple[str, str]":
    """重建提权新进程的命令 ``(exe, params)``（方案 6.1.1 边界一）。

    frozen 下 ``sys.executable`` 就是程序自身，续做指令走命令行参数传
    （新进程拿不到本进程内存里的下载路径）；路径可能含空格，参数值加引号。
    """
    if not is_frozen():
        raise RuntimeError("resume_command 仅支持打包版（frozen）")
    return sys.executable, '--resume-update="%s"' % staged_path


def request_elevation(staged_path: str) -> bool:
    """以提权方式拉起「续做替换」的新进程；返回是否成功受理。

    ``False`` 的两种典型：用户在 UAC 里点了「否」（``ShellExecuteW`` 返回 ≤ 32，
    **不是错误**，调用方应静默回到对话框）；非 frozen 环境本就不该走到这里。
    受理成功后**调用方必须退出当前进程**，避免两个实例并存。
    """
    if not is_frozen():
        return False
    exe, params = resume_command(staged_path)
    return _shell_execute_runas(exe, params) > 32


def _shell_execute_runas(exe: str, params: str) -> int:
    """``ShellExecuteW`` 的 ``runas`` 动作；返回值 ``> 32`` 为受理成功。

    UAC 弹窗由 Windows 给出，本函数不做任何等待——受理即返回，替换由
    新进程完成后自行重启本程序。
    """
    try:
        import ctypes

        return int(
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        )
    except Exception:  # noqa: BLE001 - 无 shell32（非 Windows）/ 异常环境
        return 0
