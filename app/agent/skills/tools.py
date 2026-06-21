"""
Skill 工具：activate_skill / run_skill_script / read_skill_resource。

8 条安全防线（与 plan v2 同步）：
1. 路径遍历 — Path.resolve().is_relative_to() 校验
2. 解释器调度 — 按扩展名分发,Windows 拒 .sh,未知扩展拒绝
3. 环境隔离 — 基线 5 个系统 key + skills_env_allowlist 显式白名单,绝不全量继承
4. 超时与孤儿杀 — Popen + wait(timeout) + Windows CREATE_NEW_PROCESS_GROUP / Linux start_new_session
5. 输出处理 — bytes 收 + errors='replace' 解码 + 按字符截断
6. 资源大小上限 — read_skill_resource 文件 > skills_script_max_resource_bytes 拒绝
7. 非交互 — stdin=DEVNULL + shell=False + args 走 argv list
8. 跨 skill 写盘隔离 — MVP 不沙盒化（cwd=skill_dir 起步,信任受审 skill）

地雷修正（vs 原方案）：
- 地雷1：name 用 str + 容灾守卫,不做动态 Literal（schema 冻结发给 DeepSeek 后无法同步）
- 地雷2：env 最小化白名单灌注,绝不 env=os.environ
- 地雷3：按扩展名调度 sys.executable / uv / bash / node,绝不裸路径执行
- 地雷4：discovery 已用 COW,这里只读 catalog 不写
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

from agent.config import settings
from agent.skills import loader
from agent.skills.discovery import get_catalog

logger = logging.getLogger(__name__)


# ---- 环境隔离基线（地雷2修正）----
# 系统正常运行必需的最小集（解释器能跑起来 + 编码正确 + 临时目录可写）。
# 业务密钥（API key）一律不在此 → 必须用户在 skills_env_allowlist 显式开洞。
_BASE_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",     # Windows DLL 加载必需
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "USERNAME",       # 部分 Python stdlib 用得到
    "USERPROFILE",    # Windows 家目录
    "HOME",           # Unix 家目录
)


def _list_catalog_names() -> list[str]:
    """容灾错误提示用：当前可用 skill 名清单。"""
    return [m.name for m in get_catalog()]


def _find_skill_dir(name: str) -> tuple[Path | None, str]:
    """查 skill 目录。返回 (路径, 错误信息)；找到则错误为空串。"""
    for m in get_catalog():
        if m.name == name:
            return Path(m.location), ""
    return None, (
        f"Error: Skill '{name}' not found. "
        f"Available skills: {_list_catalog_names() or '(empty)'}"
    )


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符截断（不是字节,避免切到 UTF-8 多字节中间）。"""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n...[truncated, total {len(text)} chars]", True


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """杀子进程树（防孤儿）。

    Windows: proc.kill() 只杀直接子进程,uv run 这类会再 spawn python 形成 uv→python 链——
    必须用 taskkill /F /T /PID 递归杀整棵进程树,否则 python 子进程占着文件导致 tempdir 清不掉。
    Linux/macOS: 用 os.killpg + start_new_session 已建的进程组,一发信号灭整组。
    """
    pid = proc.pid
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=5, shell=False,
            )
        except Exception as e:
            logger.warning("[skills/run_script] taskkill 失败 pid=%d: %s", pid, e)
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), 9)  # SIGKILL 整组
        except Exception as e:
            logger.warning("[skills/run_script] killpg 失败 pid=%d: %s", pid, e)
            try:
                proc.kill()
            except Exception:
                pass



def _build_safe_env() -> dict[str, str]:
    """构造最小化环境变量集（地雷2核心修正）。

    基线系统 key + 用户在 config.toml 显式白名单列出的 key,绝不全量继承 os.environ。
    第三方 skill 默认拿不到 OPENAI_API_KEY/BOCHA_API_KEY 等机密。
    """
    env: dict[str, str] = {}
    # 基线灌注
    for k in _BASE_ENV_KEYS:
        if k in os.environ:
            env[k] = os.environ[k]
    # 显式白名单灌注
    for k in settings.skills_env_allowlist:
        k = k.strip()
        if k and k in os.environ:
            env[k] = os.environ[k]
    return env


def _resolve_interpreter(script_path: Path) -> tuple[list[str], str]:
    """按扩展名调度解释器（地雷3修正）。

    Returns:
        (cmd_prefix, error_msg)：成功时 cmd_prefix 是要拼接 script_path 前的部分,error_msg 为空。
        失败时 cmd_prefix 为空,error_msg 是用户/LLM 可读的错误说明。
    """
    ext = script_path.suffix.lower()

    if ext == ".py":
        # 优先 uv run（PEP 723 自包含依赖）；否则退回当前 Python 解释器
        uv_path = shutil.which("uv")
        if uv_path:
            return [uv_path, "run", "--script", str(script_path)], ""
        return [sys.executable, str(script_path)], ""

    if ext == ".sh":
        if sys.platform.startswith("win"):
            return [], (
                f"Error: .sh script not supported on Windows. "
                f"Please use a .py script. Path: {script_path.name}"
            )
        bash = shutil.which("bash") or "/bin/bash"
        return [bash, str(script_path)], ""

    if ext in (".js", ".mjs"):
        node_path = shutil.which("node")
        if not node_path:
            return [], (
                f"Error: 'node' executable not found in PATH. "
                f"Cannot run {script_path.name}; please install Node.js."
            )
        return [node_path, str(script_path)], ""

    return [], (
        f"Error: Unsupported script extension '{ext}'. "
        f"Supported: .py (preferred), .sh (Linux/macOS), .js/.mjs."
    )


def _activate_skill_impl(name: str) -> str:
    """activate_skill 的同步实现（便于单测直接调）。"""
    skill_dir, err = _find_skill_dir(name)
    if err:
        return err

    result = loader.activate(name)
    if not result:
        return f"Error: Skill '{name}' found but SKILL.md unreadable."

    meta = result["meta"]
    content = result["content"]
    resources = result["resources"]

    parts = [
        f'<skill_content name="{name}">',
        content,
        "</skill_content>",
        "<skill_resources>",
        f"scripts: {resources['scripts']}",
        f"references: {resources['references']}",
        f"assets: {resources['assets']}",
        "</skill_resources>",
    ]

    # requires-env 渐进同意：声明需要但未开洞 → warning（不阻断,由用户后续决定）
    if meta.requires_env:
        allow = set(settings.skills_env_allowlist)
        missing = [k for k in meta.requires_env if k not in allow]
        if missing:
            parts.append(
                f"<skill_warning>This skill declares requires-env: {list(meta.requires_env)}. "
                f"The following are NOT in skills_env_allowlist and will not be visible to "
                f"scripts: {missing}. If a script fails due to missing env, ask the user to "
                f"add them to config.toml `skills_env_allowlist`.</skill_warning>"
            )

    return "\n".join(parts)


def _run_skill_script_impl(name: str, script: str, args: list[str] | None = None) -> str:
    """run_skill_script 的同步实现。"""
    skill_dir, err = _find_skill_dir(name)
    if err:
        return err
    assert skill_dir is not None  # for type checker

    # ---- 防线1：路径校验（scripts/ 子树限定）----
    scripts_root = (skill_dir / "scripts").resolve()
    try:
        script_path = (skill_dir / "scripts" / script).resolve()
    except (OSError, ValueError) as e:
        return f"Error: invalid script path {script!r}: {e}"

    try:
        script_path.relative_to(scripts_root)  # Python 3.9 兼容的 is_relative_to 替代
    except ValueError:
        return (
            f"Error: script path escapes scripts/ directory. "
            f"Got {script!r} resolved to {script_path}, must be inside {scripts_root}."
        )

    if not script_path.is_file():
        return f"Error: script not found: {script}"

    # ---- 防线2：解释器调度 ----
    cmd_prefix, interp_err = _resolve_interpreter(script_path)
    if interp_err:
        return interp_err
    cmd = cmd_prefix + list(args or [])

    # ---- 防线3：环境最小化 ----
    safe_env = _build_safe_env()

    # ---- 防线4 & 7：Popen + 进程组隔离 + 非交互 ----
    popen_kwargs: dict = {
        "cwd": str(skill_dir),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "env": safe_env,
    }
    if sys.platform.startswith("win"):
        # CREATE_NEW_PROCESS_GROUP 让 Ctrl+C/kill 能精准命中子树
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # 新会话隔离子进程组
        popen_kwargs["start_new_session"] = True

    logger.info(
        "[skills/run_script] skill=%s script=%s argc=%d cwd=%s env_keys=%d",
        name, script, len(args or []), skill_dir, len(safe_env),
    )

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except (OSError, ValueError) as e:
        return f"Error: failed to spawn script: {type(e).__name__}: {e}"

    timeout_s = settings.skills_script_timeout
    timed_out = False
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_proc_tree(proc)
        try:
            stdout_b, stderr_b = proc.communicate(timeout=5)
        except Exception:
            stdout_b, stderr_b = b"", b""

    # ---- 防线5：bytes 解码 + 按字符截断 ----
    stdout = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr = (stderr_b or b"").decode("utf-8", errors="replace")
    limit = settings.skills_script_max_output
    stdout, t1 = _truncate(stdout, limit)
    stderr, t2 = _truncate(stderr, limit)

    exit_code = proc.returncode if proc.returncode is not None else -1
    status_note = "TIMEOUT (killed)" if timed_out else ""

    return (
        f'<skill_script_result name="{name}" script="{script}" exit_code="{exit_code}" '
        f'truncated="{t1 or t2}" status="{status_note}">\n'
        f"<stdout>\n{stdout}\n</stdout>\n"
        f"<stderr>\n{stderr}\n</stderr>\n"
        f"</skill_script_result>"
    )


def _read_skill_resource_impl(name: str, path: str) -> str:
    """read_skill_resource 的同步实现。"""
    skill_dir, err = _find_skill_dir(name)
    if err:
        return err
    assert skill_dir is not None

    # ---- 防线1：路径限定在 skill 根目录范围 ----
    skill_root = skill_dir.resolve()
    try:
        resource_path = (skill_dir / path).resolve()
    except (OSError, ValueError) as e:
        return f"Error: invalid resource path {path!r}: {e}"

    try:
        resource_path.relative_to(skill_root)
    except ValueError:
        return (
            f"Error: resource path escapes skill directory. "
            f"Got {path!r} resolved to {resource_path}, must be inside {skill_root}."
        )

    if not resource_path.is_file():
        return f"Error: resource not found: {path}"

    # ---- 防线6：大小校验 ----
    try:
        size = resource_path.stat().st_size
    except OSError as e:
        return f"Error: stat failed: {e}"
    max_bytes = settings.skills_script_max_resource_bytes
    if size > max_bytes:
        return (
            f"Error: resource too large ({size} bytes > limit {max_bytes}). "
            f"Skill author should split it or store as static asset."
        )

    try:
        text = resource_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error: read failed: {e}"

    return f'<skill_resource name="{name}" path="{path}">\n{text}\n</skill_resource>'


# ---- LangChain @tool 包装（绑给主 agent）----
# 注意：name 用普通 str + 函数体容灾守卫,不做动态 Literal（地雷1修正）。
# 工具描述里告诉 LLM 「先 activate_skill 拿到完整指令再操作」的渐进式披露流程。


@tool
def activate_skill(name: str) -> str:
    """加载指定 skill 的完整指令与资源清单。

    使用流程：
    1. 当用户任务匹配 system prompt 中 catalog 列出的某 skill 描述时,先调用此工具。
    2. 返回结构化文本包含 SKILL.md 正文 + scripts/references/assets 资源清单。
    3. 按正文指令决定下一步：调 run_skill_script 跑脚本,或 read_skill_resource 读参考。

    Args:
        name: skill 名称,必须是 catalog 中列出的。不存在会返回错误提示。
    """
    return _activate_skill_impl(name)


@tool
def run_skill_script(name: str, script: str, script_args: list[str] | None = None) -> str:
    """在受控沙盒中执行某 skill 的脚本。

    - script 必须是 skill 的 scripts/ 子目录下的相对路径（如 'extract.py'）。
    - .py 优先用 uv run（PEP 723 自包含依赖）,否则用当前 Python 解释器；.sh 仅 Linux/macOS。
    - 环境最小化：脚本默认看不到 API key 等密钥,除非用户在 config.toml `skills_env_allowlist` 显式开洞。
    - 超时 / 输出截断 / 非交互 stdin 自动应用。
    - 非零 exit 不抛异常,LLM 看 stderr 自己判断重试或换法。

    Args:
        name: skill 名称（catalog 中存在）。
        script: 相对 scripts/ 的脚本文件名（如 'extract.py'）。
        script_args: 透传给脚本的命令行参数列表（可选）。命名避开 `args` 关键字避免 LangChain
            自动 mangle 成 `v__args` 导致参数名不匹配。
    """
    return _run_skill_script_impl(name, script, script_args)


@tool
def read_skill_resource(name: str, path: str) -> str:
    """读取某 skill 目录下的参考文件 / 资产文件。

    - path 是相对 skill 根目录的路径（如 'references/api.md' 或 'reference.md'）。
    - 仅允许文本文件,大小受 skills_script_max_resource_bytes 限制（默认 100KB）。
    - 路径必须落在 skill 目录内,防越界。

    Args:
        name: skill 名称（catalog 中存在）。
        path: 相对 skill 根目录的资源文件路径。
    """
    return _read_skill_resource_impl(name, path)
