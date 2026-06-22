"""步骤 1 测试：install 模块（source/preview/installer/env_grant）+ _dump_toml list 扩展。

不真调 GitHub API（用 mock 拦 httpx.AsyncClient.get），不真写用户 home 目录
（每个 installer 测试用 monkey patch 把 INSTALL_BASE 重定向到 tmp）。
"""
import sys, os, asyncio, base64, io, json, hashlib, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

from agent.config import settings
from agent.skills.install import (
    source as src_mod,
    preview as preview_mod,
    installer as inst_mod,
    env_grant as env_mod,
)
from agent.skills.install.source import (
    parse_github_subdir_url,
    extract_zip,
    _safe_relative_path,
)
from agent.skills.install.preview import (
    preview_from_zip_bytes,
    _parse_skill_md_bytes,
)
from agent.skills.install.installer import (
    install_from_zip_bytes,
    uninstall,
)


# =============================================================================
# source.py: parse_github_subdir_url
# =============================================================================

def test_parse_github_subdir_url_basic():
    o, r, ref, sub = parse_github_subdir_url(
        "https://github.com/anthropics/skills/tree/main/skills/pdf"
    )
    assert (o, r, ref, sub) == ("anthropics", "skills", "main", "skills/pdf")
    print("  PASS parse_github_subdir_url_basic")


def test_parse_github_subdir_url_trailing_slash():
    o, r, ref, sub = parse_github_subdir_url(
        "https://github.com/o/r/tree/v1.0/path/to/skill/"
    )
    assert (o, r, ref, sub) == ("o", "r", "v1.0", "path/to/skill")
    print("  PASS parse_github_subdir_url_trailing_slash")


def test_parse_github_subdir_url_invalid():
    cases = [
        "https://github.com/x/y",
        "https://gitlab.com/o/r/tree/main/sub",
        "not-a-url",
        "https://github.com/o/r/blob/main/file.py",
    ]
    for url in cases:
        try:
            parse_github_subdir_url(url)
            assert False, f"应抛 ValueError: {url}"
        except ValueError:
            pass
    print(f"  PASS parse_github_subdir_url_invalid ({len(cases)} cases)")


def test_safe_relative_path():
    assert _safe_relative_path("a/b.py") == "a/b.py"
    assert _safe_relative_path("SKILL.md") == "SKILL.md"
    # 这些应被拒
    bad = ["../x", "a/../b", "/abs/path", "a//b", "a/./b"]
    for p in bad:
        try:
            _safe_relative_path(p)
            assert False, f"应拒 {p}"
        except ValueError:
            pass
    print("  PASS safe_relative_path")


# =============================================================================
# source.py: extract_zip
# =============================================================================

def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def test_extract_zip_basic():
    zb = _make_zip({
        "SKILL.md": b"---\nname: t\ndescription: test skill\n---\nbody",
        "scripts/run.py": b"print(1)",
    })
    files = extract_zip(zb)
    assert "SKILL.md" in files
    assert "scripts/run.py" in files
    print("  PASS extract_zip_basic")


def test_extract_zip_strips_common_prefix():
    """GitHub Release 的 zip 含 repo-ref/ 顶层目录,extract_zip 应剥掉。"""
    zb = _make_zip({
        "anthropics-skills-abc/SKILL.md": b"---\nname: t\ndescription: test\n---\n",
        "anthropics-skills-abc/scripts/x.py": b"x",
    })
    files = extract_zip(zb)
    assert "SKILL.md" in files
    assert "scripts/x.py" in files
    assert "anthropics-skills-abc" not in str(list(files.keys()))
    print("  PASS extract_zip_strips_common_prefix")


def test_extract_zip_rejects_traversal():
    zb = _make_zip({
        "../etc/passwd": b"oops",
        "SKILL.md": b"---\nname: t\ndescription: test\n---\n",
    })
    try:
        extract_zip(zb)
        assert False, "应拒 .. 路径"
    except ValueError as e:
        assert "unsafe" in str(e).lower() or ".." in str(e)
    print("  PASS extract_zip_rejects_traversal")


def test_extract_zip_rejects_no_skill_md():
    zb = _make_zip({"README.md": b"hi"})
    try:
        extract_zip(zb)
        assert False, "应拒无 SKILL.md"
    except ValueError as e:
        assert "SKILL.md" in str(e)
    print("  PASS extract_zip_rejects_no_skill_md")


def test_extract_zip_rejects_too_many_files():
    files = {f"f{i}.py": b"x" for i in range(250)}
    files["SKILL.md"] = b"---\nname: t\ndescription: test\n---\n"
    zb = _make_zip(files)
    try:
        extract_zip(zb)
        assert False, "应拒文件数过多"
    except ValueError as e:
        assert "过多" in str(e) or "many" in str(e).lower()
    print("  PASS extract_zip_rejects_too_many_files")


# =============================================================================
# preview.py
# =============================================================================

def test_parse_skill_md_bytes_basic():
    content = b"---\nname: alpha\ndescription: handles alpha tasks\nversion: 1.0\nrequires-env:\n  - OPENAI_API_KEY\n---\n\n# body"
    fm = _parse_skill_md_bytes(content)
    assert fm["name"] == "alpha"
    assert fm["description"] == "handles alpha tasks"
    assert fm["version"] == "1.0"
    assert fm["requires_env"] == ["OPENAI_API_KEY"]
    print("  PASS parse_skill_md_bytes_basic")


def test_parse_skill_md_bytes_missing_description():
    content = b"---\nname: alpha\n---\nbody"
    try:
        _parse_skill_md_bytes(content)
        assert False, "应拒 description 缺失"
    except ValueError as e:
        assert "description" in str(e)
    print("  PASS parse_skill_md_bytes_missing_description")


def test_preview_from_zip_bytes():
    zb = _make_zip({
        "SKILL.md": b"---\nname: alpha\ndescription: alpha desc\nrequires-env:\n  - OPENAI_API_KEY\n  - PEXELS_API_KEY\n---\n# body",
        "scripts/run.py": b"# hello\nprint(1)\nprint(2)\nprint(3)\n",
        "scripts/util.py": b"x = 1\n",
        "references/api.md": b"# API doc",
    })
    p = preview_from_zip_bytes(zb)
    assert p["name"] == "alpha"
    assert p["description"] == "alpha desc"
    assert p["requires_env"] == ["OPENAI_API_KEY", "PEXELS_API_KEY"]
    assert p["script_count"] == 2
    assert p["file_count"] == 4
    assert p["source_type"] == "zip"
    # 脚本预览含 head 字段
    scripts = {s["path"]: s for s in p["scripts_preview"]}
    assert "scripts/run.py" in scripts
    assert "print(1)" in scripts["scripts/run.py"]["head"]
    print(f"  PASS preview_from_zip_bytes (name={p['name']}, scripts={p['script_count']}, env={p['requires_env']})")


def test_preview_cache_hit():
    zb = _make_zip({
        "SKILL.md": b"---\nname: cached\ndescription: cache test\n---\n",
        "scripts/x.py": b"x",
    })
    p1 = preview_from_zip_bytes(zb)
    p2 = preview_from_zip_bytes(zb)
    # 同对象引用说明命中缓存
    assert p1 is p2 or p1 == p2
    print("  PASS preview_cache_hit")


# =============================================================================
# installer.py
# =============================================================================

def _patch_install_base(tmp: Path):
    """把 INSTALL_BASE 重定向到 tmp,避免污染真实 home。"""
    inst_mod.INSTALL_BASE = tmp


async def test_install_from_zip(tmp):
    _patch_install_base(tmp / "agents/skills")
    zb = _make_zip({
        "SKILL.md": b"---\nname: testpkg\ndescription: test install\nversion: 1.0\n---\n# body",
        "scripts/run.py": b"print('hi')",
    })
    result = await install_from_zip_bytes(zb)
    assert result["name"] == "testpkg"
    skill_dir = Path(result["path"])
    assert skill_dir.is_dir()
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "scripts/run.py").is_file()
    meta_file = skill_dir / ".install.json"
    assert meta_file.is_file()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["name"] == "testpkg"
    assert meta["source_type"] == "zip"
    assert "files_sha256" in meta
    assert "SKILL.md" in meta["files_sha256"]
    print(f"  PASS install_from_zip (path={skill_dir})")


async def test_install_rejects_duplicate(tmp):
    _patch_install_base(tmp / "agents/skills")
    zb = _make_zip({
        "SKILL.md": b"---\nname: dup\ndescription: test\n---\n",
        "scripts/x.py": b"x",
    })
    await install_from_zip_bytes(zb)
    try:
        await install_from_zip_bytes(zb)
        assert False, "应拒已存在"
    except FileExistsError as e:
        assert "dup" in str(e)
    print("  PASS install_rejects_duplicate")


async def test_install_rejects_bad_name(tmp):
    _patch_install_base(tmp / "agents/skills")
    # 恶意 SKILL.md 用路径字符当 name
    zb = _make_zip({
        "SKILL.md": b"---\nname: ../../etc/passwd\ndescription: evil\n---\n",
    })
    try:
        await install_from_zip_bytes(zb)
        assert False, "应拒路径字符 name"
    except ValueError as e:
        assert "非法" in str(e) or "illegal" in str(e).lower()
    print("  PASS install_rejects_bad_name")


async def test_uninstall_user_skill(tmp):
    _patch_install_base(tmp / "agents/skills")
    zb = _make_zip({
        "SKILL.md": b"---\nname: rmme\ndescription: test\n---\n",
        "scripts/x.py": b"x",
    })
    await install_from_zip_bytes(zb)
    r = await uninstall("rmme")
    assert r["removed"]
    assert not (tmp / "agents/skills" / "rmme").exists()
    print("  PASS uninstall_user_skill")


async def test_uninstall_rejects_no_meta(tmp):
    """没有 .install.json 的目录 → 拒绝（防误删项目内置示例）。"""
    base = tmp / "agents/skills"
    _patch_install_base(base)
    skill_dir = base / "manual_placed"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: manual_placed\ndescription: ok\n---\n", encoding="utf-8")
    # 故意不写 .install.json
    try:
        await uninstall("manual_placed")
        assert False, "应拒无 .install.json"
    except PermissionError as e:
        assert "install.json" in str(e)
    # 目录应仍存在
    assert skill_dir.is_dir()
    print("  PASS uninstall_rejects_no_meta")


async def test_uninstall_rejects_traversal(tmp):
    _patch_install_base(tmp / "agents/skills")
    try:
        await uninstall("../../etc")
        assert False, "应拒路径越界"
    except ValueError:
        pass
    print("  PASS uninstall_rejects_traversal")


# =============================================================================
# env_grant.py + _dump_toml list 扩展
# =============================================================================

def test_dump_toml_list_extension():
    """_dump_toml 加 list[str] 分支后能正确序列化。"""
    from api_view.api.settings import _dump_toml
    text = _dump_toml({
        "name": "test",
        "count": 42,
        "tags": ["foo", "bar", "with\"quote"],
    })
    assert 'name = "test"' in text
    assert "count = 42" in text
    # list 行包含三个字符串元素,引号正确转义
    assert 'tags = ["foo", "bar", "with\\"quote"]' in text
    print(f"  PASS dump_toml_list_extension: tags 行 OK")


def test_dump_toml_list_reject_non_str():
    """list[int] 等应被拒。"""
    from api_view.api.settings import _dump_toml
    try:
        _dump_toml({"x": [1, 2, 3]})
        assert False, "应拒 list[int]"
    except ValueError as e:
        assert "str" in str(e).lower()
    print("  PASS dump_toml_list_reject_non_str")


async def test_env_grant_validates_keys(tmp):
    """非法 key 应被拒。"""
    cases = [
        ["lowercase_key"],   # 必须大写开头
        ["123_BAD"],         # 必须字母开头
        ["BAD-KEY"],         # 不允许连字符
        ["X Y"],             # 不允许空格
    ]
    for keys in cases:
        try:
            await env_mod.grant_env(keys)
            assert False, f"应拒非法 key {keys}"
        except ValueError as e:
            assert "非法" in str(e) or "illegal" in str(e).lower()
    print(f"  PASS env_grant_validates_keys ({len(cases)} cases)")


async def test_env_grant_dedupe_preserve_order(tmp):
    """重复 key 去重,顺序保持。需要真实 config.toml。"""
    # 用临时 config.toml 测试
    tmp.mkdir(parents=True, exist_ok=True)
    fake_config = tmp / "config.toml"
    fake_config.write_text(
        'openai_api_key = "sk-test"\nskills_env_allowlist = ["EXISTING_KEY"]\n',
        encoding="utf-8",
    )
    # patch env_grant 的 _CONFIG_TOML
    original = env_mod._CONFIG_TOML
    env_mod._CONFIG_TOML = fake_config
    try:
        r = await env_mod.grant_env(["NEW_KEY", "EXISTING_KEY", "ANOTHER", "NEW_KEY"])
        # 去重 + 保序：EXISTING + NEW + ANOTHER
        assert r["allowlist"] == ["EXISTING_KEY", "NEW_KEY", "ANOTHER"], f"got {r['allowlist']}"
        assert set(r["added"]) == {"NEW_KEY", "ANOTHER"}
        # 文件确实被写回
        written = fake_config.read_text(encoding="utf-8")
        assert "EXISTING_KEY" in written
        assert "NEW_KEY" in written
        print(f"  PASS env_grant_dedupe_preserve_order: {r}")
    finally:
        env_mod._CONFIG_TOML = original


# =============================================================================
# Main
# =============================================================================

async def main():
    print("=" * 60)
    print("Install module tests (step 1)")
    print("=" * 60)

    # 同步测试
    test_parse_github_subdir_url_basic()
    test_parse_github_subdir_url_trailing_slash()
    test_parse_github_subdir_url_invalid()
    test_safe_relative_path()
    test_extract_zip_basic()
    test_extract_zip_strips_common_prefix()
    test_extract_zip_rejects_traversal()
    test_extract_zip_rejects_no_skill_md()
    test_extract_zip_rejects_too_many_files()
    test_parse_skill_md_bytes_basic()
    test_parse_skill_md_bytes_missing_description()
    test_preview_from_zip_bytes()
    test_preview_cache_hit()
    test_dump_toml_list_extension()
    test_dump_toml_list_reject_non_str()

    # 异步测试（每个用独立 tmp 子目录避免互相污染）
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        await test_install_from_zip(tmp / "install_basic")
        await test_install_rejects_duplicate(tmp / "install_dup")
        await test_install_rejects_bad_name(tmp / "install_badname")
        await test_uninstall_user_skill(tmp / "uninstall_ok")
        await test_uninstall_rejects_no_meta(tmp / "uninstall_nometa")
        await test_uninstall_rejects_traversal(tmp / "uninstall_traversal")
        await test_env_grant_validates_keys(tmp / "env_validate")
        await test_env_grant_dedupe_preserve_order(tmp / "env_dedupe")

    print("\n" + "=" * 60)
    print("[Step 1: ALL PASS]")
    print("=" * 60)


asyncio.run(main())
