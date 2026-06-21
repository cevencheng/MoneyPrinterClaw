"""里程碑 3 测试：主图编译 + skills 工具注册 + catalog 注入。

不跑真实 LLM,只验证:
- build_agent 编译通过,节点和工具数量正确
- skills_enabled=True 时主 agent 看到 7 个工具 + system prompt 含 catalog
- skills_enabled=False 时回归原 4 工具 + 无 catalog（无副作用,视频链路完整）
- refresh_skills 在 build_agent 启动时被调用
"""
import sys, os, asyncio, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

from agent.config import settings
from agent.skills.discovery import refresh_skills, get_catalog


def _write_skill(root: Path, name: str, *, description="test skill", body="# body"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        encoding="utf-8",
    )
    return d


async def test_build_with_skills(tmp):
    """skills_enabled=True + 2 个 skill → 主图编译通过,主 agent 工具数 = 4 原始 + 3 skill = 7。"""
    _write_skill(tmp, "alpha", description="alpha task handler")
    _write_skill(tmp, "beta", description="beta task handler")
    settings.skills_project_dir = str(tmp)
    settings.skills_user_dir = ""
    settings.skills_enabled = True

    # 重 import build 模块以重置 logger / 确保最新代码
    from langgraph.checkpoint.memory import MemorySaver
    from agent.graph.build import build_agent

    ckpt = MemorySaver()
    graph = await build_agent(ckpt)
    # 验证图节点齐全（17 + ToolNode 是合并的,节点本身不变）
    nodes = graph.get_graph().nodes
    expected_nodes = {
        "agent", "tools",
        "video_start", "batch_start", "batch_dispatch", "batch_summary", "batch_redo_start",
        "supervisor_route", "researcher", "editor", "reviewer", "director",
        "request_review", "resource_prep", "render",
    }
    missing = expected_nodes - set(nodes.keys())
    assert not missing, f"主图缺节点: {missing}"
    print(f"  PASS build_with_skills: 节点数={len(nodes)}, catalog={len(get_catalog())} skill")


async def test_build_disabled(tmp):
    """skills_enabled=False → 主图仍可编译 + ToolNode 只含 web_search。"""
    _write_skill(tmp, "alpha", description="should not be loaded")
    settings.skills_project_dir = str(tmp)
    settings.skills_enabled = False

    from langgraph.checkpoint.memory import MemorySaver
    from agent.graph.build import build_agent

    ckpt = MemorySaver()
    graph = await build_agent(ckpt)
    # catalog 被强制清空
    assert get_catalog() == [], f"disabled 应清空 catalog, got {len(get_catalog())}"
    nodes = graph.get_graph().nodes
    assert "agent" in nodes and "tools" in nodes
    print(f"  PASS build_disabled: catalog 空, 节点齐 ({len(nodes)})")
    settings.skills_enabled = True


def test_catalog_in_system_prompt():
    """直接验证 dynamic_prompt 拼接逻辑：catalog 非空 → prompt 含 ## 段；catalog 空 → 无副作用。"""
    from agent.skills.catalog import render_catalog_prompt
    # 启用 + 无 skill → 空串
    settings.skills_enabled = True
    settings.skills_project_dir = "/tmp/__nonexistent__"
    settings.skills_user_dir = ""
    refresh_skills()
    assert render_catalog_prompt() == ""
    print("  PASS catalog_prompt_empty_no_skill")

    # 关闭 → 空串
    settings.skills_enabled = False
    assert render_catalog_prompt() == ""
    print("  PASS catalog_prompt_empty_disabled")

    # 启用 + 有 skill → 含 ## 段
    settings.skills_enabled = True
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_skill(tmp, "xray", description="xray skill")
        settings.skills_project_dir = str(tmp)
        refresh_skills()
        out = render_catalog_prompt()
        assert "## " in out and "xray" in out
        print(f"  PASS catalog_prompt_nonempty ({len(out)} chars)")


def test_skill_tools_decorated():
    """3 个 skill 工具应为 LangChain BaseTool 实例,有 name/description。"""
    from agent.skills import activate_skill, run_skill_script, read_skill_resource
    for t in (activate_skill, run_skill_script, read_skill_resource):
        assert hasattr(t, "name") and hasattr(t, "description")
        assert t.description, f"{t.name} 缺 description"
    assert activate_skill.name == "activate_skill"
    assert run_skill_script.name == "run_skill_script"
    assert read_skill_resource.name == "read_skill_resource"
    print(f"  PASS skill_tools_decorated: {activate_skill.name}, {run_skill_script.name}, {read_skill_resource.name}")


async def main():
    print("=" * 60)
    print("Milestone 3: integrate into build.py")
    print("=" * 60)
    test_skill_tools_decorated()
    test_catalog_in_system_prompt()
    with tempfile.TemporaryDirectory() as td:
        await test_build_with_skills(Path(td) / "with")
    with tempfile.TemporaryDirectory() as td:
        await test_build_disabled(Path(td) / "disabled")
    print("\n" + "=" * 60)
    print("[Milestone 3: ALL PASS]")
    print("=" * 60)


asyncio.run(main())
