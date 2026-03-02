#!/usr/bin/env python3
"""
LiveScope 自动启动器
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
自动完成：
  1. Python 版本检查（需要 3.10+）
  2. 虚拟环境创建与激活
  3. 依赖安装（requirements.txt）
  4. Protobuf 编译（抖音 proto 定义）
  5. .env 初始化
  6. 启动 LiveScope CLI

用法：
  python start.py tiktok @username
  python start.py douyin 7123456789
  python start.py douyin https://live.douyin.com/xxx
  python start.py sessions
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# ── 路径常量 ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_FILE = ROOT / ".env"
PROTO_SRC = ROOT / "proto" / "douyin.proto"
PROTO_OUT = ROOT / "src" / "proto"
PROTO_PY = PROTO_OUT / "douyin_pb2.py"

IS_WIN = platform.system() == "Windows"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")
VENV_PIP = VENV_DIR / ("Scripts/pip.exe" if IS_WIN else "bin/pip")

# ── ANSI 颜色（Windows 无彩色回退）────────────────────────────────────────────
if IS_WIN or not sys.stdout.isatty():
    def _c(code: str, text: str) -> str: return text
else:
    def _c(code: str, text: str) -> str: return f"\033[{code}m{text}\033[0m"

def green(t: str) -> str:  return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str) -> str:   return _c("36", t)
def bold(t: str) -> str:   return _c("1",  t)
def red(t: str) -> str:    return _c("31", t)
def dim(t: str) -> str:    return _c("2",  t)


def say(icon: str, msg: str) -> None:
    print(f"  {icon}  {msg}")


def banner() -> None:
    print()
    print(cyan("  ┌─────────────────────────────────────────┐"))
    print(cyan("  │") + bold("   LiveScope  —  自动启动器              ") + cyan("│"))
    print(cyan("  │") + dim("   TikTok & 抖音直播 AI 监控工具          ") + cyan("│"))
    print(cyan("  └─────────────────────────────────────────┘"))
    print()


def run(cmd: list[str], check: bool = True, capture: bool = False, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
        **kwargs,
    )


def pip_install(packages: list[str], quiet: bool = False) -> None:
    cmd = [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade"] + packages
    if quiet:
        cmd.append("-q")
    run(cmd)


# ── 检查步骤 ──────────────────────────────────────────────────────────────────

def find_python() -> str:
    """返回满足 3.10+ 的 Python 可执行路径，找不到则退出。"""
    candidates = ["python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"]
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        result = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        try:
            major, minor = eval(result.stdout.strip())  # noqa: S307
            if (major, minor) >= (3, 10):
                return exe
        except Exception:
            continue
    return ""


def check_python() -> None:
    say("🐍", f"Python 版本检查 …  当前：{platform.python_version()}")
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        say("✓", green(f"Python {major}.{minor} 满足要求"))
        return

    say("⚠ ", yellow(f"当前 Python {major}.{minor} 低于要求，正在寻找系统中更高版本 …"))
    found = find_python()
    if not found:
        print(red("\n  ✗ 未找到 Python 3.10+"))
        print(yellow("    macOS 推荐用 Homebrew 安装：brew install python@3.12"))
        print(yellow("    或访问 https://www.python.org/downloads/"))
        sys.exit(1)

    say("✓", green(f"找到满足要求的解释器：{found}"))
    say("🔄", f"使用 {cyan(found)} 重新启动 …")
    print()
    os.execv(found, [found] + sys.argv)


def setup_venv() -> None:
    if VENV_DIR.exists():
        say("✓", green(f"虚拟环境已存在  {dim(str(VENV_DIR))}"))
        return

    say("📦", "创建虚拟环境 .venv …")
    run([sys.executable, "-m", "venv", str(VENV_DIR)])
    say("✓", green("虚拟环境创建完成"))


def install_dependencies() -> None:
    say("📥", "检查并安装依赖 …")

    # 先升级 pip
    run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"])

    # 比对已安装包，判断是否需要更新
    result = run(
        [str(VENV_PYTHON), "-m", "pip", "install", "--dry-run", "-r", str(REQUIREMENTS)],
        check=False, capture=True,
    )
    needs_install = "Would install" in result.stdout

    if needs_install or result.returncode != 0:
        say("⬇ ", "安装缺失依赖（可能需要几分钟）…")
        run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"])
        say("✓", green("依赖安装完成"))
    else:
        say("✓", green("所有依赖已是最新"))


def install_playwright_browsers() -> None:
    """确保 Playwright Chromium 浏览器已安装。"""
    # 检查 chromium 是否已安装
    result = run(
        [str(VENV_PYTHON), "-m", "playwright", "install", "--dry-run", "chromium"],
        check=False, capture=True,
    )
    # dry-run 输出含 "chromium" 说明需要安装
    if result.returncode != 0 or "chromium" in (result.stdout + result.stderr).lower():
        say("🌐", "安装 Playwright Chromium（首次约 150MB，后续跳过）…")
        run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"], check=False)
        say("✓", green("Playwright Chromium 安装完成"))
    else:
        say("✓", green("Playwright Chromium 已就绪"))


def compile_proto() -> None:
    if PROTO_PY.exists():
        say("✓", green(f"Protobuf 已编译  {dim('src/proto/douyin_pb2.py')}"))
        return

    say("⚙ ", "编译抖音 Protobuf 定义 …")
    PROTO_OUT.mkdir(parents=True, exist_ok=True)
    init_file = PROTO_OUT / "__init__.py"
    if not init_file.exists():
        init_file.touch()

    # 安装 grpcio-tools（仅用于编译 proto，不加入 requirements.txt）
    result = run(
        [str(VENV_PYTHON), "-m", "grpc_tools.protoc", "--version"],
        check=False, capture=True,
    )
    if result.returncode != 0:
        say("⬇ ", "安装 grpcio-tools（用于编译 proto）…")
        pip_install(["grpcio-tools"], quiet=True)

    # 执行编译
    result = run(
        [
            str(VENV_PYTHON), "-m", "grpc_tools.protoc",
            f"-I{ROOT / 'proto'}",
            f"--python_out={PROTO_OUT}",
            str(PROTO_SRC),
        ],
        check=False, capture=True,
    )

    if result.returncode == 0 and PROTO_PY.exists():
        say("✓", green("Protobuf 编译成功"))
    else:
        say("⚠ ", yellow("Protobuf 编译失败，抖音采集将以降级模式运行（跳过消息解析）"))
        say("  ", dim(result.stderr.strip()[:200] if result.stderr else ""))


def setup_env() -> None:
    if ENV_FILE.exists():
        say("✓", green(f".env 已存在"))
        return

    say("📝", "初始化 .env 配置文件 …")
    shutil.copy(ENV_EXAMPLE, ENV_FILE)
    say("✓", green(".env 已从模板创建"))
    say("💡", yellow("如需使用抖音，请编辑 .env 填入 DOUYIN_COOKIE"))


def preflight_check() -> None:
    """汇总所有环境检查，输出一行通过/失败摘要。"""
    checks = {
        "Python 3.10+":     sys.version_info >= (3, 10),
        "虚拟环境":          VENV_DIR.exists(),
        "依赖已安装":        VENV_PIP.exists(),
        "Protobuf 已编译":  PROTO_PY.exists(),
        ".env 已配置":       ENV_FILE.exists(),
    }
    all_ok = all(checks.values())
    print()
    print(bold("  环境状态："))
    for name, ok in checks.items():
        icon = green("✓") if ok else yellow("○")
        print(f"    {icon}  {name}")
    print()
    if all_ok:
        print(green("  ✅ 环境就绪，启动中 …"))
    else:
        print(yellow("  ⚠  部分检查未通过（见上方），程序仍将尝试运行"))
    print()


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    banner()

    # ── 1. 环境准备 ───────────────────────────────────────────────────────────
    print(bold("  【环境准备】"))
    check_python()
    setup_venv()
    install_dependencies()
    install_playwright_browsers()
    compile_proto()
    setup_env()
    preflight_check()

    # ── 2. 解析用户参数（无参数时进入交互菜单）─────────────────────────────────
    args = sys.argv[1:]

    if not args:
        args = interactive_menu()
        if not args:
            return

    # ── 3. 启动 LiveScope 主程序 ──────────────────────────────────────────────
    cmd = [str(VENV_PYTHON), "-m", "src.main"] + args
    say("🚀", f"启动：{cyan(' '.join(cmd))}")
    print()

    try:
        os.execv(str(VENV_PYTHON), cmd)   # 替换当前进程，保持前台
    except OSError:
        # Windows 不支持 execv，用 subprocess 代替
        result = subprocess.run(cmd, cwd=str(ROOT))
        sys.exit(result.returncode)


def _free_port(port: int) -> None:
    """如果端口被占用，杀掉占用进程。"""
    import signal
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().splitlines()
        for pid in pids:
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    say("🔄", yellow(f"已清理占用端口 {port} 的旧进程（PID {pid}）"))
                except ProcessLookupError:
                    pass
    except FileNotFoundError:
        pass  # lsof 不可用（Windows）


def launch_dashboard() -> None:
    """启动 Web 看板，并自动打开浏览器。"""
    import time, threading, webbrowser

    port = 8765
    url  = f"http://127.0.0.1:{port}"

    _free_port(port)

    say("🖥️ ", f"启动数据看板  →  {cyan(url)}")
    say("💡", dim("按 Control + C 关闭看板"))
    print()

    def open_browser() -> None:
        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    cmd = [
        str(VENV_PYTHON), "-m", "uvicorn",
        "src.api:app",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--log-level", "warning",
    ]
    try:
        subprocess.run(cmd, cwd=str(ROOT))
    except KeyboardInterrupt:
        pass


def ask(prompt: str, default: str = "") -> str:
    """读取用户输入，支持默认值。"""
    hint = f"（默认：{default}）" if default else ""
    try:
        val = input(f"  {prompt}{hint}  › ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or default


def choose(prompt: str, options: list[tuple[str, str]]) -> str:
    """
    选项菜单，返回选中的值。
    options: [(显示文字, 返回值), ...]
    """
    print(f"\n  {bold(prompt)}")
    for i, (label, _) in enumerate(options, 1):
        print(f"    {cyan(str(i))}.  {label}")
    print()
    while True:
        raw = ask(f"请输入数字 1-{len(options)}")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print(red(f"  请输入 1 到 {len(options)} 之间的数字"))


def interactive_menu() -> list[str]:
    """无参数启动时显示的交互式向导，返回等价的 CLI 参数列表。"""
    print(bold("  【选择功能】"))

    action = choose("你想做什么？", [
        ("🖥️   打开数据看板（采集 + 查看全在浏览器里）", "dashboard"),
        ("❌  退出",                                     "exit"),
    ])

    if action == "exit":
        return []

    if action == "dashboard":
        launch_dashboard()
        return []


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n  {yellow('已中止')}")
        sys.exit(0)
