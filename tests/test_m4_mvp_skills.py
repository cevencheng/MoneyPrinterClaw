"""里程碑 4 测试：MVP skill E2E。

验证放在 .agents/skills/ 下的 2 个真实 skill 在 build_agent 启动后能被发现 +
直接调工具能正常激活/执行（不走 LLM,只测受控管线本身）。
"""
import sys, os, json, asyncio, tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

# 用默认配置（指向 .agents/skills,以 app/ 为锚点）
from agent.config import settings
from agent.skills.discovery import refresh_skills, get_catalog
from agent.skills.tools import (
    _activate_skill_impl,
    _run_skill_script_impl,
    _read_skill_resource_impl,
)


def test_discovers_real_skills():
    """settings 默认指向 .agents/skills/,refresh 后应找到 2 个 MVP skill。"""
    settings.skills_enabled = True
    settings.skills_user_dir = ""
    # 强制使用默认值（其他测试可能修改过）
    settings.skills_project_dir = "../.agents/skills"
    refresh_skills()
    names = [m.name for m in get_catalog()]
    assert "markdown-reviewer" in names, f"未找到 markdown-reviewer: {names}"
    assert "text-stats" in names, f"未找到 text-stats: {names}"
    print(f"  PASS discovers_real_skills: {names}")


def test_activate_markdown_reviewer():
    """activate markdown-reviewer 返回正文 + 资源清单。"""
    out = _activate_skill_impl("markdown-reviewer")
    assert '<skill_content name="markdown-reviewer">' in out
    assert "审稿清单" in out, "应含正文关键词"
    assert "scripts: []" in out, "纯指令型无 scripts"
    assert "</skill_content>" in out
    print(f"  PASS activate_markdown_reviewer ({len(out)} chars)")


def test_activate_text_stats():
    """activate text-stats 返回正文 + scripts/count.py 清单。"""
    out = _activate_skill_impl("text-stats")
    assert '<skill_content name="text-stats">' in out
    assert "scripts/count.py" in out, "应列出 count.py"
    print("  PASS activate_text_stats")


def test_run_text_stats_count():
    """端到端跑 text-stats 的 count.py,验证 JSON 输出可解析。"""
    test_text = "你好世界。Hello world!\n\n第二段中文测试。"
    out = _run_skill_script_impl("text-stats", "count.py", args=["--text", test_text])
    assert 'exit_code="0"' in out, f"脚本执行失败: {out[:500]}"
    # 提取 stdout 段里的 JSON
    import re
    m = re.search(r"<stdout>\n(.+?)\n</stdout>", out, re.DOTALL)
    assert m, f"未找到 stdout: {out[:500]}"
    stdout = m.group(1).strip()
    data = json.loads(stdout)
    assert data["paragraphs"] == 2, f"段落数={data['paragraphs']}, want 2"
    assert data["chinese_chars"] > 0
    assert data["chars"] > 0
    print(f"  PASS run_text_stats_count: {data}")


def test_run_text_stats_help():
    """跑 --help 验证 argparse 接口工作（agentskills 标准要求脚本提供 --help）。"""
    out = _run_skill_script_impl("text-stats", "count.py", args=["--help"])
    assert 'exit_code="0"' in out
    assert "--text" in out, "help 应展示 --text 参数"
    print("  PASS run_text_stats_help")


async def test_full_build_agent_e2e():
    """完整 build_agent 编译后,主图能注册所有节点且 skill 工具可用。"""
    settings.skills_enabled = True
    settings.skills_project_dir = "../.agents/skills"
    settings.skills_user_dir = ""
    from langgraph.checkpoint.memory import MemorySaver
    from agent.graph.build import build_agent

    ckpt = MemorySaver()
    graph = await build_agent(ckpt)
    assert "agent" in graph.get_graph().nodes
    assert "tools" in graph.get_graph().nodes
    # catalog 应已加载
    assert len(get_catalog()) >= 2
    names = [m.name for m in get_catalog()]
    print(f"  PASS full_build_agent_e2e: {len(get_catalog())} skills loaded: {names}")


def main():
    print("=" * 60)
    print("Milestone 4: MVP skill E2E")
    print("=" * 60)
    test_discovers_real_skills()
    test_activate_markdown_reviewer()
    test_activate_text_stats()
    test_run_text_stats_count()
    test_run_text_stats_help()
    asyncio.run(test_full_build_agent_e2e())
    print("\n" + "=" * 60)
    print("[Milestone 4: ALL PASS]")
    print("=" * 60)


main()
