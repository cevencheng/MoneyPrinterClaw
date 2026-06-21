"""里程碑 1 测试：发现 + frontmatter 解析 + COW 缓存 + catalog 渲染。"""
import sys, os, tempfile, threading
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

from agent.config import settings
from agent.skills import discovery, loader, catalog
from agent.skills.discovery import SkillMeta, discover_skills, refresh_skills, get_catalog


def _write_skill(root: Path, name: str, *, description="", reqenv=None, fm_name=None, extra="", body=""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    fm_name_line = f"name: {fm_name or name}\n"
    desc_line = f"description: {description}\n" if description else ""
    env_line = ""
    if reqenv:
        env_line = "requires-env:\n" + "".join(f"  - {k}\n" for k in reqenv)
    body_text = body or "# Body\nbody text.\n"
    (d / "SKILL.md").write_text(
        f"---\n{fm_name_line}{desc_line}{env_line}{extra}---\n\n{body_text}",
        encoding="utf-8",
    )
    return d


def test_basic_discovery(tmp):
    _write_skill(tmp, "alpha", description="alpha skill description")
    settings.skills_project_dir = str(tmp)
    settings.skills_user_dir = ""
    settings.skills_enabled = True
    metas = discover_skills()
    assert len(metas) == 1, f"want 1, got {len(metas)}"
    m = metas[0]
    assert m.name == "alpha"
    assert m.description == "alpha skill description"
    assert Path(m.location).resolve() == (tmp / "alpha").resolve()
    print(f"  PASS basic_discovery: name={m.name}")


def test_skip_empty_description(tmp):
    _write_skill(tmp, "beta", description="")
    settings.skills_project_dir = str(tmp)
    metas = discover_skills()
    assert all(m.name != "beta" for m in metas), "empty desc should be skipped"
    print("  PASS skip_empty_description")


def test_name_mismatch_warns(tmp):
    _write_skill(tmp, "gamma_dir", description="test", fm_name="custom_name")
    settings.skills_project_dir = str(tmp)
    metas = discover_skills()
    custom = [m for m in metas if m.name == "custom_name"]
    assert len(custom) == 1, "should accept frontmatter name"
    print(f"  PASS name_mismatch_warns: accepted as {custom[0].name}")


def test_requires_env_parsed(tmp):
    _write_skill(tmp, "delta", description="test", reqenv=["OPENAI_API_KEY", "PEXELS_API_KEY"])
    settings.skills_project_dir = str(tmp)
    metas = discover_skills()
    d = next(m for m in metas if m.name == "delta")
    assert d.requires_env == ("OPENAI_API_KEY", "PEXELS_API_KEY"), f"got {d.requires_env}"
    print(f"  PASS requires_env_parsed: {d.requires_env}")


def test_no_frontmatter_skipped(tmp):
    d = tmp / "no_fm"
    d.mkdir()
    (d / "SKILL.md").write_text("plain markdown no frontmatter\n", encoding="utf-8")
    settings.skills_project_dir = str(tmp)
    metas = discover_skills()
    assert all(m.name != "no_fm" for m in metas)
    print("  PASS no_frontmatter_skipped")


def test_project_overrides_user(tmp):
    user_root = tmp / "user"
    proj_root = tmp / "project"
    _write_skill(user_root, "shared", description="user version")
    _write_skill(proj_root, "shared", description="project version")
    settings.skills_user_dir = str(user_root)
    settings.skills_project_dir = str(proj_root)
    metas = discover_skills()
    shared = [m for m in metas if m.name == "shared"]
    assert len(shared) == 1 and shared[0].description == "project version"
    print(f"  PASS project_overrides_user: {shared[0].description}")
    settings.skills_user_dir = ""


def test_cow_atomic_refresh(tmp):
    for i in range(20):
        _write_skill(tmp, f"sk{i:02d}", description=f"skill {i}")
    settings.skills_project_dir = str(tmp)
    settings.skills_user_dir = ""
    refresh_skills()

    stop = threading.Event()
    errors = []

    def reader():
        try:
            while not stop.is_set():
                for m in get_catalog():
                    _ = m.name + m.description
        except RuntimeError as e:
            errors.append(f"reader RuntimeError: {e}")
        except Exception as e:
            errors.append(f"reader other: {type(e).__name__}: {e}")

    def writer():
        try:
            for _ in range(200):
                if stop.is_set():
                    break
                refresh_skills()
        except Exception as e:
            errors.append(f"writer {type(e).__name__}: {e}")

    r1 = threading.Thread(target=reader)
    r2 = threading.Thread(target=reader)
    w = threading.Thread(target=writer)
    r1.start(); r2.start(); w.start()
    w.join(timeout=10)
    stop.set()
    r1.join(timeout=2); r2.join(timeout=2)

    assert not errors, f"COW should not raise, got: {errors}"
    print(f"  PASS cow_atomic_refresh: 2 readers + 1 writer x 200 refresh no exceptions")


def test_disabled_returns_empty(tmp):
    _write_skill(tmp, "alpha", description="test")
    settings.skills_project_dir = str(tmp)
    settings.skills_enabled = False
    refresh_skills()
    assert get_catalog() == [], "disabled -> empty catalog"
    print("  PASS disabled_returns_empty")
    settings.skills_enabled = True


def test_loader_activate(tmp):
    skill_dir = _write_skill(tmp, "epsilon", description="test", body="# Epsilon Inst\nStep 1.\n")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print('hi')", encoding="utf-8")
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "doc.md").write_text("ref doc", encoding="utf-8")
    settings.skills_project_dir = str(tmp)
    settings.skills_enabled = True
    settings.skills_user_dir = ""
    refresh_skills()
    result = loader.activate("epsilon")
    assert result is not None
    assert "Epsilon Inst" in result["content"], f"content stripped fm: {result['content'][:100]}"
    first_line = result["content"].split("\n")[0]
    assert not first_line.startswith("---"), f"fm not stripped: {first_line!r}"
    assert "scripts/run.py" in result["resources"]["scripts"]
    assert "references/doc.md" in result["resources"]["references"]
    print(f"  PASS loader_activate: content len={len(result['content'])} scripts={result['resources']['scripts']}")


def test_loader_activate_missing(tmp):
    settings.skills_project_dir = str(tmp)
    refresh_skills()
    assert loader.activate("nope_xxx") is None
    print("  PASS loader_activate_missing")


def test_catalog_render(tmp):
    _write_skill(tmp, "zeta", description="zeta task handler")
    _write_skill(tmp, "eta", description="eta task handler")
    settings.skills_project_dir = str(tmp)
    settings.skills_enabled = True
    settings.skills_user_dir = ""
    refresh_skills()
    text = catalog.render_catalog_prompt()
    assert "## " in text
    assert "- zeta: zeta task handler" in text
    assert "- eta: eta task handler" in text
    print(f"  PASS catalog_render: {len(text)} chars")


def test_catalog_empty_when_no_skills(tmp):
    settings.skills_project_dir = str(tmp)
    settings.skills_user_dir = ""
    refresh_skills()
    assert catalog.render_catalog_prompt() == ""
    print("  PASS catalog_empty_when_no_skills")


def test_catalog_empty_when_disabled(tmp):
    _write_skill(tmp, "alpha", description="test")
    settings.skills_project_dir = str(tmp)
    refresh_skills()
    settings.skills_enabled = False
    try:
        assert catalog.render_catalog_prompt() == ""
        print("  PASS catalog_empty_when_disabled")
    finally:
        settings.skills_enabled = True


def main():
    print("=" * 60)
    print("Milestone 1: foundation tests")
    print("=" * 60)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tests = [
            ("basic_discovery", test_basic_discovery),
            ("skip_empty_description", test_skip_empty_description),
            ("name_mismatch_warns", test_name_mismatch_warns),
            ("requires_env_parsed", test_requires_env_parsed),
            ("no_frontmatter_skipped", test_no_frontmatter_skipped),
            ("project_overrides_user", test_project_overrides_user),
            ("disabled_returns_empty", test_disabled_returns_empty),
            ("loader_activate", test_loader_activate),
            ("loader_activate_missing", test_loader_activate_missing),
            ("catalog_render", test_catalog_render),
            ("catalog_empty_when_no_skills", test_catalog_empty_when_no_skills),
            ("catalog_empty_when_disabled", test_catalog_empty_when_disabled),
            ("cow_atomic_refresh", test_cow_atomic_refresh),
        ]
        for tname, tfn in tests:
            subdir = tmp / tname
            subdir.mkdir(exist_ok=True)
            tfn(subdir)
    print("\n" + "=" * 60)
    print("[Milestone 1: ALL PASS]")
    print("=" * 60)


main()
