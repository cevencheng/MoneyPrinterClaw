"""里程碑 2 测试：activate_skill / run_skill_script / read_skill_resource。

覆盖所有 8 条安全防线 + 4 地雷修正。
"""
import sys, os, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

from agent.config import settings
from agent.skills.discovery import refresh_skills
from agent.skills.tools import (
    _activate_skill_impl,
    _run_skill_script_impl,
    _read_skill_resource_impl,
    _build_safe_env,
    _resolve_interpreter,
)


def _write_skill(root: Path, name: str, *, description="test skill", reqenv=None, body="# body\n"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {name}", f"description: {description}"]
    if reqenv:
        fm.append("requires-env:")
        for e in reqenv:
            fm.append(f"  - {e}")
    (d / "SKILL.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return d


def _setup_catalog(tmp: Path):
    settings.skills_project_dir = str(tmp)
    settings.skills_user_dir = ""
    settings.skills_enabled = True
    refresh_skills()


# =============================================================================
# activate_skill 测试
# =============================================================================

def test_activate_basic(tmp):
    _write_skill(tmp, "alpha", description="alpha task handler", body="# Alpha\nStep 1\n")
    _setup_catalog(tmp)
    out = _activate_skill_impl("alpha")
    assert '<skill_content name="alpha">' in out
    assert "Alpha" in out
    assert "</skill_content>" in out
    assert "<skill_resources>" in out
    assert "scripts: []" in out
    print("  PASS activate_basic")


def test_activate_not_found(tmp):
    """地雷1验证：name 不存在 → 容灾错误 + 可用清单。"""
    _write_skill(tmp, "alpha", description="test")
    _setup_catalog(tmp)
    out = _activate_skill_impl("nope_xxx")
    assert out.startswith("Error: Skill 'nope_xxx' not found")
    assert "alpha" in out, "应列出当前可用 skill"
    print(f"  PASS activate_not_found")


def test_activate_lists_resources(tmp):
    d = _write_skill(tmp, "beta", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "x.py").write_text("print(1)")
    (d / "references").mkdir()
    (d / "references" / "doc.md").write_text("doc")
    (d / "assets").mkdir()
    (d / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _setup_catalog(tmp)
    out = _activate_skill_impl("beta")
    assert "scripts/x.py" in out
    assert "references/doc.md" in out
    assert "assets/logo.png" in out
    print("  PASS activate_lists_resources")


def test_activate_requires_env_warning(tmp):
    """SKILL.md 声明 requires-env 但未在 allowlist → 应出 warning。"""
    _write_skill(tmp, "gamma", description="test", reqenv=["OPENAI_API_KEY"])
    _setup_catalog(tmp)
    settings.skills_env_allowlist = []  # 没开洞
    out = _activate_skill_impl("gamma")
    assert "<skill_warning>" in out
    assert "OPENAI_API_KEY" in out
    print("  PASS activate_requires_env_warning")


def test_activate_requires_env_no_warning_when_allowed(tmp):
    _write_skill(tmp, "gamma", description="test", reqenv=["OPENAI_API_KEY"])
    _setup_catalog(tmp)
    settings.skills_env_allowlist = ["OPENAI_API_KEY"]
    try:
        out = _activate_skill_impl("gamma")
        assert "<skill_warning>" not in out
        print("  PASS activate_requires_env_no_warning_when_allowed")
    finally:
        settings.skills_env_allowlist = []


# =============================================================================
# run_skill_script 测试
# =============================================================================

def test_run_script_success(tmp):
    """正常 .py 脚本执行,stdout/exit_code 正确。"""
    d = _write_skill(tmp, "echo_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "hi.py").write_text("print('hello-from-skill')")
    _setup_catalog(tmp)
    out = _run_skill_script_impl("echo_skill", "hi.py")
    assert 'exit_code="0"' in out
    assert "hello-from-skill" in out
    print("  PASS run_script_success")


def test_run_script_path_traversal_rejected(tmp):
    """防线1：scripts/ 子树越界拒绝。"""
    _write_skill(tmp, "evil", description="test")
    _setup_catalog(tmp)
    out = _run_skill_script_impl("evil", "../../../etc/passwd")
    assert out.startswith("Error: ") and "escape" in out.lower()
    print("  PASS run_script_path_traversal_rejected")


def test_run_script_not_found(tmp):
    _write_skill(tmp, "ghost", description="test")
    _setup_catalog(tmp)
    out = _run_skill_script_impl("ghost", "no_such.py")
    assert out.startswith("Error: script not found")
    print("  PASS run_script_not_found")


def test_run_script_unsupported_ext(tmp):
    """防线2：未知扩展名拒绝。"""
    d = _write_skill(tmp, "ruby_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "x.rb").write_text("puts 'hi'")
    _setup_catalog(tmp)
    out = _run_skill_script_impl("ruby_skill", "x.rb")
    assert "Unsupported script extension" in out
    print("  PASS run_script_unsupported_ext")


def test_run_script_windows_rejects_sh(tmp):
    """地雷3验证：Windows 下拒绝 .sh。"""
    if not sys.platform.startswith("win"):
        print("  SKIP run_script_windows_rejects_sh (non-Windows)")
        return
    d = _write_skill(tmp, "sh_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "x.sh").write_text("echo hi")
    _setup_catalog(tmp)
    out = _run_skill_script_impl("sh_skill", "x.sh")
    assert ".sh script not supported on Windows" in out
    print("  PASS run_script_windows_rejects_sh")


def test_run_script_env_isolation(tmp):
    """地雷2验证：默认 OPENAI_API_KEY 不会泄露给脚本；加入白名单后可见。"""
    d = _write_skill(tmp, "env_test", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "show_env.py").write_text(
        "import os\n"
        "print('OPENAI=' + os.getenv('OPENAI_API_KEY', 'NOT_SET'))\n"
        "print('PATH_OK=' + ('yes' if os.getenv('PATH') else 'no'))\n"
    )
    _setup_catalog(tmp)
    # 故意往真实环境里塞密钥（模拟用户配了 API key）
    os.environ["OPENAI_API_KEY"] = "sk-SECRET-MUST-NOT-LEAK"

    # 默认 allowlist 空 → 脚本应看不到
    settings.skills_env_allowlist = []
    out = _run_skill_script_impl("env_test", "show_env.py")
    assert "OPENAI=NOT_SET" in out, f"密钥泄露了！got: {out[:300]}"
    assert "PATH_OK=yes" in out, "基线 PATH 应可见"

    # 加入白名单 → 脚本应能看到
    settings.skills_env_allowlist = ["OPENAI_API_KEY"]
    try:
        out2 = _run_skill_script_impl("env_test", "show_env.py")
        assert "OPENAI=sk-SECRET-MUST-NOT-LEAK" in out2, f"白名单未生效: {out2[:300]}"
        print("  PASS run_script_env_isolation (默认隔离 + allowlist 可控)")
    finally:
        settings.skills_env_allowlist = []
        del os.environ["OPENAI_API_KEY"]


def test_run_script_timeout(tmp):
    """防线4：超时 kill。"""
    d = _write_skill(tmp, "timeout_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "loop.py").write_text("import time\nwhile True: time.sleep(0.5)\n")
    _setup_catalog(tmp)
    settings.skills_script_timeout = 2
    try:
        out = _run_skill_script_impl("timeout_skill", "loop.py")
        assert "TIMEOUT" in out, f"应触发超时: {out[:300]}"
        print("  PASS run_script_timeout")
    finally:
        settings.skills_script_timeout = 120


def test_run_script_non_utf8_output(tmp):
    """防线5：脚本输出非 UTF-8 bytes,decode 不崩。"""
    d = _write_skill(tmp, "binary_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "bin.py").write_text(
        "import sys\nsys.stdout.buffer.write(bytes([0xFF, 0xFE, 0xFD]))\n"
    )
    _setup_catalog(tmp)
    out = _run_skill_script_impl("binary_skill", "bin.py")
    assert 'exit_code="0"' in out
    # 不抛异常即可,replace 字符 � 在输出里
    print("  PASS run_script_non_utf8_output")


def test_run_script_output_truncation(tmp):
    """防线5：超长输出按字符截断。"""
    d = _write_skill(tmp, "loud_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "loud.py").write_text("print('x' * 100000)")
    _setup_catalog(tmp)
    settings.skills_script_max_output = 1000
    try:
        out = _run_skill_script_impl("loud_skill", "loud.py")
        assert "truncated" in out
        assert 'truncated="True"' in out
        # 单段 stdout 应该不超过 1000 + truncation 提示
        # 整个返回里 'xxx...' 段应远小于 100000
        assert len(out) < 5000, f"截断未生效,len={len(out)}"
        print(f"  PASS run_script_output_truncation (out len={len(out)})")
    finally:
        settings.skills_script_max_output = 30000


def test_run_script_non_zero_exit(tmp):
    """非零 exit 不抛异常,返回 exit_code + stderr 让 LLM 自决策。"""
    d = _write_skill(tmp, "fail_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "fail.py").write_text(
        "import sys\nsys.stderr.write('boom\\n')\nsys.exit(7)\n"
    )
    _setup_catalog(tmp)
    out = _run_skill_script_impl("fail_skill", "fail.py")
    assert 'exit_code="7"' in out
    assert "boom" in out
    print("  PASS run_script_non_zero_exit")


def test_run_script_args_passed(tmp):
    d = _write_skill(tmp, "args_skill", description="test")
    (d / "scripts").mkdir()
    (d / "scripts" / "args.py").write_text(
        "import sys\nprint('argc=' + str(len(sys.argv)))\nprint(sys.argv[1:])\n"
    )
    _setup_catalog(tmp)
    out = _run_skill_script_impl("args_skill", "args.py", args=["hello", "world"])
    assert "argc=3" in out  # argv[0] = script_path + 2 args
    assert "hello" in out and "world" in out
    print("  PASS run_script_args_passed")


# =============================================================================
# read_skill_resource 测试
# =============================================================================

def test_read_resource_basic(tmp):
    d = _write_skill(tmp, "doc_skill", description="test")
    (d / "references").mkdir()
    (d / "references" / "api.md").write_text("# API Reference\nstuff", encoding="utf-8")
    _setup_catalog(tmp)
    out = _read_skill_resource_impl("doc_skill", "references/api.md")
    assert "API Reference" in out
    assert "<skill_resource" in out
    print("  PASS read_resource_basic")


def test_read_resource_root_level(tmp):
    """anthropics/skills 实际把 reference.md 放根目录,应支持。"""
    d = _write_skill(tmp, "doc_skill", description="test")
    (d / "reference.md").write_text("root level doc", encoding="utf-8")
    _setup_catalog(tmp)
    out = _read_skill_resource_impl("doc_skill", "reference.md")
    assert "root level doc" in out
    print("  PASS read_resource_root_level")


def test_read_resource_path_traversal_rejected(tmp):
    _write_skill(tmp, "doc_skill", description="test")
    _setup_catalog(tmp)
    out = _read_skill_resource_impl("doc_skill", "../../../etc/passwd")
    assert "escape" in out.lower()
    print("  PASS read_resource_path_traversal_rejected")


def test_read_resource_too_large(tmp):
    """防线6：超过大小上限拒绝。"""
    d = _write_skill(tmp, "big_skill", description="test")
    (d / "references").mkdir()
    (d / "references" / "huge.md").write_text("x" * 200_000)
    _setup_catalog(tmp)
    settings.skills_script_max_resource_bytes = 100_000
    try:
        out = _read_skill_resource_impl("big_skill", "references/huge.md")
        assert "too large" in out
        print("  PASS read_resource_too_large")
    finally:
        settings.skills_script_max_resource_bytes = 100 * 1024


def test_read_resource_not_found(tmp):
    _write_skill(tmp, "doc_skill", description="test")
    _setup_catalog(tmp)
    out = _read_skill_resource_impl("doc_skill", "no_such.md")
    assert "not found" in out
    print("  PASS read_resource_not_found")


def test_read_resource_skill_not_found(tmp):
    _setup_catalog(tmp)
    out = _read_skill_resource_impl("nope", "x.md")
    assert "Skill 'nope' not found" in out
    print("  PASS read_resource_skill_not_found")


# =============================================================================
# 辅助函数测试
# =============================================================================

def test_build_safe_env_baseline(tmp):
    """基线 env 含 PATH 不含 OPENAI_API_KEY。"""
    os.environ["OPENAI_API_KEY"] = "sk-test-leak"
    settings.skills_env_allowlist = []
    try:
        env = _build_safe_env()
        assert "PATH" in env or "Path" in env, "PATH 应在基线"
        assert "OPENAI_API_KEY" not in env, "密钥不应进基线"
    finally:
        del os.environ["OPENAI_API_KEY"]
    print("  PASS build_safe_env_baseline")


def test_resolve_interpreter_py(tmp):
    p = Path("any/x.py")
    cmd, err = _resolve_interpreter(p)
    assert not err
    # uv run 或 sys.executable 任一（Windows 下 .EXE/.exe 大小写不一致,统一 lower 比较）
    first = cmd[0].lower()
    assert cmd and (first.endswith("uv") or first.endswith("uv.exe") or "python" in first), \
        f"unexpected interpreter: {cmd[0]}"
    print(f"  PASS resolve_interpreter_py: {cmd[0]}")


def test_resolve_interpreter_unknown(tmp):
    p = Path("any/x.xyz")
    cmd, err = _resolve_interpreter(p)
    assert not cmd and "Unsupported" in err
    print("  PASS resolve_interpreter_unknown")


def test_langchain_tool_invocation(tmp):
    """回归 LangChain @tool 装饰器层参数名问题（args 关键字会被 mangle 成 v__args）。

    LangChain StructuredTool 对名为 `args` 的参数会做特殊处理（避免与内部冲突）,
    导致 LLM 用 `args=[...]` 调用时实际接收的 kwarg 名变成 `v__args`,函数签名不匹配会 TypeError。
    用 script_args 命名规避此坑。本测试直接走 .invoke(...) 模拟 LLM 工具调用路径。
    """
    from agent.skills.tools import run_skill_script, activate_skill, read_skill_resource

    d = _write_skill(tmp, "lc_skill", description="lc test")
    (d / "scripts").mkdir()
    (d / "scripts" / "echo.py").write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))"
    )
    (d / "ref.md").write_text("ref content", encoding="utf-8")
    _setup_catalog(tmp)

    # 1. activate_skill 通过 LangChain 调用
    out1 = activate_skill.invoke({"name": "lc_skill"})
    assert '<skill_content name="lc_skill">' in out1, "activate via @tool 失败"

    # 2. run_skill_script 带 script_args 调用（核心回归）
    out2 = run_skill_script.invoke({
        "name": "lc_skill",
        "script": "echo.py",
        "script_args": ["hello", "from", "langchain"],
    })
    assert 'exit_code="0"' in out2 and "hello from langchain" in out2, \
        f"run via @tool with script_args 失败: {out2[:400]}"

    # 3. run_skill_script 不传 script_args（None 默认值）
    out3 = run_skill_script.invoke({"name": "lc_skill", "script": "echo.py"})
    assert 'exit_code="0"' in out3, f"无 script_args 调用失败: {out3[:400]}"

    # 4. read_skill_resource 通过 LangChain 调用
    out4 = read_skill_resource.invoke({"name": "lc_skill", "path": "ref.md"})
    assert "ref content" in out4
    print("  PASS langchain_tool_invocation: 4 工具通过 @tool 包装层全部正常")


# =============================================================================
# Main
# =============================================================================

def main():
    print("=" * 60)
    print("Milestone 2: skill tools (8 security defenses)")
    print("=" * 60)
    tests = [
        # activate_skill
        ("activate_basic", test_activate_basic),
        ("activate_not_found", test_activate_not_found),
        ("activate_lists_resources", test_activate_lists_resources),
        ("activate_requires_env_warning", test_activate_requires_env_warning),
        ("activate_requires_env_no_warning_when_allowed", test_activate_requires_env_no_warning_when_allowed),
        # run_skill_script
        ("run_script_success", test_run_script_success),
        ("run_script_path_traversal_rejected", test_run_script_path_traversal_rejected),
        ("run_script_not_found", test_run_script_not_found),
        ("run_script_unsupported_ext", test_run_script_unsupported_ext),
        ("run_script_windows_rejects_sh", test_run_script_windows_rejects_sh),
        ("run_script_env_isolation", test_run_script_env_isolation),
        ("run_script_timeout", test_run_script_timeout),
        ("run_script_non_utf8_output", test_run_script_non_utf8_output),
        ("run_script_output_truncation", test_run_script_output_truncation),
        ("run_script_non_zero_exit", test_run_script_non_zero_exit),
        ("run_script_args_passed", test_run_script_args_passed),
        # read_skill_resource
        ("read_resource_basic", test_read_resource_basic),
        ("read_resource_root_level", test_read_resource_root_level),
        ("read_resource_path_traversal_rejected", test_read_resource_path_traversal_rejected),
        ("read_resource_too_large", test_read_resource_too_large),
        ("read_resource_not_found", test_read_resource_not_found),
        ("read_resource_skill_not_found", test_read_resource_skill_not_found),
        # helpers
        ("build_safe_env_baseline", test_build_safe_env_baseline),
        ("resolve_interpreter_py", test_resolve_interpreter_py),
        ("resolve_interpreter_unknown", test_resolve_interpreter_unknown),
        ("langchain_tool_invocation", test_langchain_tool_invocation),
    ]
    with tempfile.TemporaryDirectory() as td:
        for tname, tfn in tests:
            sub = Path(td) / tname
            sub.mkdir(exist_ok=True)
            tfn(sub)
    print("\n" + "=" * 60)
    print("[Milestone 2: ALL PASS]")
    print("=" * 60)


main()
