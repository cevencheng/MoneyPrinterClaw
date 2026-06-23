"""步骤 2 测试：HTTP API（FastAPI TestClient）。

覆盖 6 个端点 happy + error path,不调真实 GitHub（用 zip_b64 走完整管线）。
"""
import sys, os, base64, io, json, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.getcwd(), "app"))
os.environ.setdefault("AGENT_OPENAI_API_KEY", "sk-test")

from fastapi.testclient import TestClient

from agent.config import settings
from agent.skills.discovery import refresh_skills
from agent.skills.install import installer as inst_mod
from agent.skills.install import env_grant as env_mod


def _make_zip_b64(files: dict[str, bytes]) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_app(tmp: Path):
    """构造一个最小 FastAPI app,只挂 skills router(跳过 lifespan 的 db/agent 初始化)。"""
    from fastapi import FastAPI
    from api_view.api.skills import router as skills_router

    app = FastAPI()
    app.include_router(skills_router, prefix="/api/skills")
    return app


def setup_isolated_env(tmp: Path) -> Path:
    """重定向 INSTALL_BASE + config.toml 到 tmp,避免污染。"""
    install_base = tmp / "agents/skills"
    inst_mod.INSTALL_BASE = install_base.resolve()
    fake_config = tmp / "config.toml"
    fake_config.write_text(
        'openai_api_key = "sk-test"\nskills_env_allowlist = []\n',
        encoding="utf-8",
    )
    env_mod._CONFIG_TOML = fake_config
    # 把 catalog 重定向：仅扫 tmp/agents/skills（user）
    settings.skills_project_dir = ""
    settings.skills_user_dir = str(install_base)
    settings.skills_env_allowlist = []
    refresh_skills()
    return install_base


def test_list_empty(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        r = client.get("/api/skills")
        assert r.status_code == 200
        assert r.json() == []
    print("  PASS list_empty")


def test_preview_zip(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    zip_b64 = _make_zip_b64({
        "SKILL.md": b"---\nname: previewme\ndescription: preview test\nrequires-env:\n  - OPENAI_API_KEY\n---\n# body",
        "scripts/run.py": b"print(1)\nprint(2)",
    })
    with TestClient(app) as client:
        r = client.post("/api/skills/preview", json={"zip_b64": zip_b64})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "previewme"
        assert data["description"] == "preview test"
        assert data["requires_env"] == ["OPENAI_API_KEY"]
        assert data["script_count"] == 1
    print("  PASS preview_zip")


def test_preview_invalid_body(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        # 缺 url/zip_b64
        r = client.post("/api/skills/preview", json={})
        assert r.status_code == 400
        # 两个都给
        r = client.post("/api/skills/preview", json={"url": "x", "zip_b64": "y"})
        assert r.status_code == 400
    print("  PASS preview_invalid_body")


def test_install_zip(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    zip_b64 = _make_zip_b64({
        "SKILL.md": b"---\nname: installme\ndescription: install test\n---\n# body",
        "scripts/x.py": b"x = 1",
    })
    with TestClient(app) as client:
        # 先 preview 让缓存填上
        client.post("/api/skills/preview", json={"zip_b64": zip_b64})
        # 然后 install
        r = client.post("/api/skills/install", json={"zip_b64": zip_b64})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["name"] == "installme"
        assert Path(data["path"]).is_dir()
        assert (Path(data["path"]) / ".install.json").is_file()

        # 再列一次应能看到
        r2 = client.get("/api/skills")
        names = [s["name"] for s in r2.json()]
        assert "installme" in names
        # source = user, removable = True（有 .install.json）
        item = next(s for s in r2.json() if s["name"] == "installme")
        assert item["source"] == "user"
        assert item["removable"] is True
    print("  PASS install_zip + list_after_install")


def test_install_duplicate_409(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    zip_b64 = _make_zip_b64({
        "SKILL.md": b"---\nname: dup\ndescription: dup test\n---\n",
        "scripts/x.py": b"x",
    })
    with TestClient(app) as client:
        r1 = client.post("/api/skills/install", json={"zip_b64": zip_b64})
        assert r1.status_code == 200
        r2 = client.post("/api/skills/install", json={"zip_b64": zip_b64})
        assert r2.status_code == 409
        assert "已安装" in r2.json()["detail"] or "installed" in r2.json()["detail"].lower()
    print("  PASS install_duplicate_409")


def test_uninstall(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    zip_b64 = _make_zip_b64({
        "SKILL.md": b"---\nname: rmme\ndescription: rm test\n---\n",
        "scripts/x.py": b"x",
    })
    with TestClient(app) as client:
        client.post("/api/skills/install", json={"zip_b64": zip_b64})
        r = client.delete("/api/skills/rmme")
        assert r.status_code == 200, r.text
        # 列表里不应再有
        r2 = client.get("/api/skills")
        assert not any(s["name"] == "rmme" for s in r2.json())
    print("  PASS uninstall")


def test_uninstall_not_found(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        r = client.delete("/api/skills/nope_xxx")
        assert r.status_code == 404
    print("  PASS uninstall_not_found")


def test_refresh(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        r = client.post("/api/skills/refresh")
        assert r.status_code == 200
        assert "count" in r.json()
    print("  PASS refresh")


def test_env_allowlist(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        # 合法 keys
        r = client.post("/api/skills/env-allowlist", json={"keys": ["OPENAI_API_KEY", "PEXELS_API_KEY"]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert "OPENAI_API_KEY" in data["allowlist"]
        assert "PEXELS_API_KEY" in data["allowlist"]

        # 非法 key
        r2 = client.post("/api/skills/env-allowlist", json={"keys": ["lowercase"]})
        assert r2.status_code == 400

        # 错误 body
        r3 = client.post("/api/skills/env-allowlist", json={"keys": "not_array"})
        assert r3.status_code == 400
    print("  PASS env_allowlist (legal + illegal + bad body)")


def test_install_invalid_body(tmp):
    setup_isolated_env(tmp)
    app = _build_app(tmp)
    with TestClient(app) as client:
        r = client.post("/api/skills/install", json={"zip_b64": "not-valid-base64-!!!"})
        assert r.status_code == 400
    print("  PASS install_invalid_body")


def main():
    print("=" * 60)
    print("Skill HTTP API tests (step 2)")
    print("=" * 60)
    tests = [
        ("list_empty", test_list_empty),
        ("preview_zip", test_preview_zip),
        ("preview_invalid_body", test_preview_invalid_body),
        ("install_zip", test_install_zip),
        ("install_duplicate_409", test_install_duplicate_409),
        ("uninstall", test_uninstall),
        ("uninstall_not_found", test_uninstall_not_found),
        ("refresh", test_refresh),
        ("env_allowlist", test_env_allowlist),
        ("install_invalid_body", test_install_invalid_body),
    ]
    with tempfile.TemporaryDirectory() as td:
        for tname, tfn in tests:
            sub = Path(td) / tname
            sub.mkdir(exist_ok=True)
            tfn(sub)
    print("\n" + "=" * 60)
    print("[Step 2: ALL PASS]")
    print("=" * 60)


main()
