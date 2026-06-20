
<div align="center">

# 🦞 MoneyPrinterClaw

**The first industrial-grade, fully-automated short-video matrix factory powered by Multi-Agent orchestration**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)]()
[![Next.js 16](https://img.shields.io/badge/Next.js-16-black.svg)]()
[![LangGraph](https://img.shields.io/badge/LangGraph-Agentic-orange.svg)]()
[![License](https://img.shields.io/badge/License-MIT-green.svg)]()

[简体中文](README.md) · **English** · SaaS-grade crash recovery · Pure natural-language driven

</div>

---

> Give it a fuzzy topic — say, "make 5 differentiated videos on Dragon Boat Festival customs" — and leave the rest to the Agentic Workflow: **find footage, write the script, voiceover, align subtitles, composite the video, review**. Power outage midway? Network jitter? Occasional LLM hiccup? The system auto-revives precisely from the latest checkpoint, ending the nightmare of restarting from scratch.

<div align="center">
<h4>Web UI</h4>

![](docs/images/webui.png)

</div>
<div align="center">
  <br/>
  <img src="docs/images/money_printer_claw_demo.gif" alt="MoneyPrinterClaw Multi-Agent Processing Flow" style="border-radius: 12px; border: 1px solid rgba(0,0,0,0.08); box-shadow: 0 4px 20px rgba(0,0,0,0.05);"/>
  <br/>
  <br/>
  <p><b>⚡ 4 highly-cohesive workers flexibly pulling in parallel + a pure-FFmpeg industrial pipeline running a serial batch (shown at 12x speed)</b></p>
</div>

## 💡 Why MoneyPrinterClaw?

Traditional video-generation scripts rely on rigid, linear pipelines that easily collapse the whole line on network jitter or script tweaks.

**MoneyPrinterClaw is a fundamental architectural rebuild.** We subverted linear control, abandoned the toy mode of "one big prompt for everything," and introduced **SOP-driven agent orchestration (Agentic Workflow)**. The user only needs to type a macro concept in the chat box, and the system activates a fully-flattened multi-agent topology to autonomously complete high-fidelity video-matrix production in an unattended state.

### ⚔️ Core Architecture Comparison

| Pain Point | Traditional Pipeline Projects | **MoneyPrinterClaw (This Project 🚀)** |
| --- | --- | --- |
| **Interaction** | Rigid web-form filling (Streamlit) | **💬 Chat-driven: an immersive conversational editorial studio built on assistant-ui** |
| **Worker Architecture** | Single monolithic LLM blindly outputting, elements drift easily | **👥 4 highly-cohesive workers flattened (researcher / editor / reviewer / director), decoupled iteration** |
| **Crash Recovery** | Killed process means restart from scratch, ghost duplicate tasks | **🛑 Dual-layer state self-healing: LangGraph node-level snapshots + disk sub-step ledgers** |
| **Workflow Routing** | Hard-coded edges, no rework loops | **🧠 Soft-coded flow: an LLM acts as Supervisor, flexibly routing per SOP** |
| **Multimedia Pipeline** | Heavy Python video libs, easily OOM | **⚡ Pure FFmpeg composite-filter forging, 8 clips rendered into a final cut in ~10s** |

---

## 🎯 Core Features

- **[x] HITL (Human-in-the-Loop) breakpoint review**: First-class LangGraph `interrupt` mechanism pops a review card in the UI. You can take over at any time to edit the agent-generated script or storyboard keywords, then let the engine continue rendering.
- **[x] SaaS-grade state self-healing & checkpoint resume**:
  - *Soft self-healing*: On occasional packet loss from external footage APIs, stateless exponential-backoff retry — never propagates to the main flow.
  - *Hard crash recovery*: `AsyncSqliteSaver` persists every superstep in real time; the underlying `progress.json` provides atomic step-skip protection. Even after a power-off restart, the system revives in-place from "footage download 5/9" in milliseconds.
- **[x] Dynamic subgraph factory (YAML-driven)**: Adding a new agent only requires a new YAML config file — mounted into the main graph with zero intrusion into core business code.
- **[x] Batch high-concurrency production**: One sentence — "generate 5 series videos" — kicks off a serial batch run. Independent `task_id`s and physically isolated persistent storage ensure a single failure never affects the global matrix.
- **[x] SSE real-time industrial monitoring**: The backend pushes sub-second stage progress via `adispatch_custom_event`; the frontend renders it live as dynamic progress cards and an industrial control panel.
- **[x] Multi-aspect & heterogeneous-model compatibility**: Full support for 9:16 / 16:9 / 1:1. Defaults to DeepSeek (high IQ / low cost), compatible with any OpenAI-standard endpoint.

---

## 🚀 Hardcore Architecture Breakdown (For Geeks & Architects)

### 1. 🗺️ SOP-driven flattened multi-agent orchestration

Built on the latest **LangGraph** design paradigm, we dismantled the black-box vacuum of traditional nested graphs and flattened the routing hub `supervisor_route` together with 4 specialized workers (`researcher` for web search, `editor` for writing/revising, `reviewer` for quality checks, `director` for storyboarding) directly onto the root graph. An LLM strictly follows a Markdown-format Standard Operating Procedure (SOP) for microscopic autonomous routing — ensuring creative flexibility and chaos while physically locking down token-leak defenses.

### 2. 🛡️ SaaS-grade long-transaction crash recovery & self-healing

We refuse to test an LLM's morality and memory on checkpoint revival. The system ships a "dual-layer checkpoint gate":

* **Soft self-healing**: On occasional packet loss when calling the Bocha search API or Pexels video gateway, `tenacity` triggers in-node stateless backoff retry; the frontend card loads in-place 1:1.
* **Hard crash recovery**: After a hard error kills the process, restart triggers `/continue` resume; the `_compute_can_continue` gate precisely protects the already-completed final-cut snapshot and guides the state machine to revive in 0ms step-skips; the downstream execution workshop performs "business-layer skipping" via the on-disk `progress.json` ledger — never re-consuming tokens.

### 3. 🎙️ Industrial-grade audio/video editing & rendering pipeline

* **Voiceover & timeline**: Seamlessly interfaces with the Alibaba CosyVoice voice-cloning pipeline; after audio is produced, it auto-launches the offline **Faster-Whisper high-precision ASR model** to align per-word timestamps in seconds and persist a high-fidelity standard `.ass` subtitle.
* **Footage & compositing**: Concurrently dispatches Pexels 4K vertical HD footage, completely abandoning the traditional Python video libs that cause OOM memory leaks; everything is re-forged on a pure FFmpeg composite-filter (Filtergraph) layer. Slicing, aspect scaling, centered padding, audio + subtitle overlay are all done in one pass at the C layer — single-clip render speed up 75%.

---

## 📦 Quick Start

### 1. Requirements
Non-compute-intensive by design; runs smoothly on an ordinary laptop:
- **Runtime**: Python 3.11/3.12, Node.js 18+
- **Disk / RAM**: Reserve 10GB, 8GB RAM recommended.
- **GPU**: **Not required at all.** Cloud LLM + online TTS + pure FFmpeg pipeline — CPU can max out concurrency.

### 2. Clone & install dependencies

```bash
git clone https://github.com/yourusername/MoneyPrinterClaw.git
cd MoneyPrinterClaw

# 1. Set up the Python backend virtual env
python -m venv .venv
source .venv/bin/activate  # On Windows use .\.venv\Scripts\activate
pip install -r requirements.txt

# 2. Install Next.js frontend deps
cd webui
npm install
cd ..

```

### 3. Inject credentials

Copy the config template and rename it to `config.toml` (already protected by .gitignore):

```bash
cp config.example.toml config.toml

```

Fill in your core model key (e.g. DeepSeek), video-footage API credentials (Pexels), etc. The `AGENT_<UPPER>` env vars support full config override — perfect for Docker & CI/CD.

### 4. Ignite the industrial pipeline

**Windows**: Double-click `start.bat` in the root to bring up both ends.
**macOS / Linux**:

```bash
# Terminal 1: bring up the central nerve (backend)
cd app && python -m uvicorn api_view.web_main:app --host 127.0.0.1 --port 8000

# Terminal 2: wake the console (frontend)
cd webui && npx next dev --port 5200

```

Open `http://localhost:5200` in your browser and shout your first line at the Agent: **"Make me a viral short video about the origin of coffee, vertical."**

---

## 🗂️ Core Directory Topology

```text
MoneyPrinterClaw/
├── app/                       # 🐍 Backend agent orchestration hub
│   ├── agent/graph/           # Multi-agent graph assembly bus
│   ├── agent/supervisor/      # SOP-driven LLM router
│   ├── agent/subagents/       # Department worker YAML config set
│   ├── agent/video/engine/    # In-house video compositing engine (FFmpeg-based)
│   └── api_view/              # FastAPI access layer & SSE push
├── webui/                     # ⚛️ Next.js 16 + React 19 immersive frontend
├── storage/                   # 💾 Local persistence & anti-avalanche isolation (Git-ignored)
│   ├── app.db                 # SQLite state truth line (LangGraph Checkpoint)
│   └── video_tasks/           # Isolated physical artifact matrix
└── config.toml                # 🔒 Global config hub

```

## 🗺️ Roadmap
    - [x] Multi-agent core engine rebuild based on LangGraph
    - [x] Checkpoint resume & SQLite state self-healing crash recovery
    - [x] Pure-FFmpeg high-performance video rendering base
    - [ ] i18n international multi-language support (planned soon, PRs welcome 👏)
    - [ ] Integrate more cloud / local open-source LLMs (Ollama, etc.)
    - [ ] Integrate more TTS voice engines (Qwen-TTS, GPT-SoVITS, etc.)
    - [ ] Provide more customizable subtitle effects
    - [ ] Subtitle segmentation optimization


## 🤝 Contributing & Acknowledgements

* Standing on the shoulders of giants: this project's underlying pipeline is inspired by [@harry0703's MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo), rewritten and elevated on top of it via the multi-agent paradigm.
* This project is released under the **MIT** license. We welcome self-media geeks and AI architects from around the world to submit Issues / PRs, and jointly build the OmniForge-driven MoneyPrinterClaw high-concurrency content middleware ecosystem!
