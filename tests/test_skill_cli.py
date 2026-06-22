"""步骤 3 测试：CLI 入口（install/list/uninstall/refresh）。"""
import sys, os, io, json, tempfile, zipfile, subprocess
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")


def _make_zip(tmp: Path, files: dict[str, bytes]) -> Path:
    """造一个 zip 文件到 tmp 下,返回文件路径。"""
    zip_path = tmp / "skill.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return zip_path


def _run_cli(args: list[str], install_base: Path, env_extra: dict | None = None) -> tuple[int, str, str]:
    """跑 CLI 子进程,把 INSTALL_BASE 重定向到 install_base。"""
    # 通过环境变量 patch INSTALL_BASE 不直观。改用 HOME 环境变量重定向 ~/.agents/skills/
    # Path("~/.agents/skills").expanduser() 在 Windows 走 USERPROFILE,Unix 走 HOME。
    env = os.environ.copy()
    # 设置 fake home,让 ~/.agents/skills/ 落在 install_base 的父级
    fake_home = install_base.parent.parent  # ~/.agents/skills → install_base = fake_home/.agents/skills
    env["USERPROFILE"] = str(fake_home)  # Windows
    env["HOME"] = str(fake_home)          # Unix
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    # 子进程 cwd 设到 app/ 以匹配生产启动方式
    proc = subprocess.run(
        [sys.executable, "-m", "agent.skills", *args],
        cwd=os.path.join(os.getcwd(), "app"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_install_local_zip(tmp):
    install_base = tmp / ".agents/skills"
    zip_path = _make_zip(tmp, {
        "SKILL.md": b"---\nname: clipkg\ndescription: cli test\n---\n# body",
        "scripts/run.py": b"print('hi')",
    })
    rc, out, err = _run_cli(["install", str(zip_path)], install_base)
    assert rc == 0, f"rc={rc}\nstdout: {out}\nstderr: {err}"
    assert "已安装 skill: clipkg" in out
    # 装在 fake home 下
    assert install_base.is_dir() or (tmp / ".agents/skills/clipkg").is_dir(), \
        f"未在 fake home 下找到 skill: ls {tmp} = {list(tmp.iterdir())}"
    print(f"  PASS cli_install_local_zip: {out.strip().splitlines()[-1] if out else ''}")


def test_cli_list_after_install(tmp):
    install_base = tmp / ".agents/skills"
    zip_path = _make_zip(tmp, {
        "SKILL.md": b"---\nname: listme\ndescription: list test\nversion: 2.0\n---\n",
        "scripts/x.py": b"x",
    })
    _run_cli(["install", str(zip_path)], install_base)
    rc, out, err = _run_cli(["list"], install_base)
    assert rc == 0, f"rc={rc}\nstderr: {err}"
    assert "listme" in out
    assert "user" in out  # source 标识
    print(f"  PASS cli_list_after_install")


def test_cli_uninstall(tmp):
    install_base = tmp / ".agents/skills"
    zip_path = _make_zip(tmp, {
        "SKILL.md": b"---\nname: rmcli\ndescription: rm cli test\n---\n",
        "scripts/x.py": b"x",
    })
    rc1, _, _ = _run_cli(["install", str(zip_path)], install_base)
    assert rc1 == 0
    rc2, out2, _ = _run_cli(["uninstall", "rmcli"], install_base)
    assert rc2 == 0
    assert "已卸载 skill: rmcli" in out2
    # 卸载后列表应空
    rc3, out3, _ = _run_cli(["list"], install_base)
    assert rc3 == 0
    assert "rmcli" not in out3
    print("  PASS cli_uninstall")


def test_cli_uninstall_nonexistent(tmp):
    install_base = tmp / ".agents/skills"
    rc, _, err = _run_cli(["uninstall", "ghost"], install_base)
    assert rc == 1
    assert "未安装" in err or "not installed" in err.lower()
    print("  PASS cli_uninstall_nonexistent")


def test_cli_refresh(tmp):
    install_base = tmp / ".agents/skills"
    rc, out, _ = _run_cli(["refresh"], install_base)
    assert rc == 0
    assert "已刷新" in out or "refresh" in out.lower()
    print(f"  PASS cli_refresh")


def test_cli_invalid_local_file(tmp):
    install_base = tmp / ".agents/skills"
    rc, _, err = _run_cli(["install", "nonexistent.zip"], install_base)
    assert rc == 1
    assert "找不到" in err or "not found" in err.lower()
    print("  PASS cli_invalid_local_file")


def main():
    print("=" * 60)
    print("Skill CLI tests (step 3)")
    print("=" * 60)
    tests = [
        ("cli_install_local_zip", test_cli_install_local_zip),
        ("cli_list_after_install", test_cli_list_after_install),
        ("cli_uninstall", test_cli_uninstall),
        ("cli_uninstall_nonexistent", test_cli_uninstall_nonexistent),
        ("cli_refresh", test_cli_refresh),
        ("cli_invalid_local_file", test_cli_invalid_local_file),
    ]
    with tempfile.TemporaryDirectory() as td:
        for tname, tfn in tests:
            sub = Path(td) / tname
            sub.mkdir(exist_ok=True)
            tfn(sub)
    print("\n" + "=" * 60)
    print("[Step 3: ALL PASS]")
    print("=" * 60)


main()
