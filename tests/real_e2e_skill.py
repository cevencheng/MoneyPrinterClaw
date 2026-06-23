"""真实 E2E：用 DeepSeek 触发 skill 激活和脚本执行,观察 SSE 流。"""
import sys, json, time
import urllib.request

TID = "dc29157ac74f4d90894656fa0aa6e6bf"
BASE = "http://127.0.0.1:8765"

def send(message: str, timeout: int = 90):
    """发消息,流式接收,解析每个 SSE 事件,返回事件列表。"""
    payload = json.dumps({"thread_id": TID, "message": message, "auto_review": False}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/chat/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    events = []
    text_buf = []
    print(f"\n>>> 用户: {message}\n")
    print("--- SSE 事件流 ---")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "{}":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            events.append(obj)
            # 摘要打印
            if "delta" in obj:
                text_buf.append(obj["delta"])
            elif "tool_call" in obj:
                tc = obj["tool_call"]
                args_str = json.dumps(tc.get("args", {}), ensure_ascii=False)[:200]
                print(f"  [tool_call] {tc.get('name')} args={args_str}")
            elif "tool_result" in obj:
                tr = obj["tool_result"]
                result_str = str(tr.get("result", ""))[:300]
                print(f"  [tool_result] {tr.get('name')} -> {result_str}")
            elif "usage" in obj:
                print(f"  [usage] {obj['usage']}")
    if text_buf:
        print(f"\n>>> 助手回复: {''.join(text_buf)}\n")
    return events


if __name__ == "__main__":
    events = send("帮我统计下这段文字的字数和段落数：你好世界 Hello world!\n\n第二段是中文测试,看看分得对不对", timeout=120)
    print(f"\n=== 共 {len(events)} 个 SSE 事件 ===")
    # 统计调用了哪些工具
    tools_called = set()
    for e in events:
        if "tool_call" in e:
            tools_called.add(e["tool_call"].get("name"))
    print(f"调用的工具: {tools_called}")
    activate_called = "activate_skill" in tools_called
    script_called = "run_skill_script" in tools_called
    print(f"\nactivate_skill 被调: {activate_called}")
    print(f"run_skill_script 被调: {script_called}")
    if activate_called and script_called:
        print("\n[E2E PASS] DeepSeek 正确激活并执行了 skill")
    elif activate_called:
        print("\n[E2E PARTIAL] activate_skill 被调但脚本未执行 (LLM 直接答了?)")
    else:
        print("\n[E2E FAIL] skill 未被激活")
