# OmniAPI

> 把外部 AI 模型收進同一個地方用：在瀏覽器裡派工給 agent、跟任何文字模型聊天、看每一筆花了多少；
> Claude Code 透過 MCP 用同一套能力，終端機裡有 `omni` 指令。三個入口，背後是同一個常駐服務、同一顆資料庫。

![version](https://img.shields.io/badge/version-1.0.0-1F2330)
![platform](https://img.shields.io/badge/platform-Windows%2011-blue)
![python](https://img.shields.io/badge/python-3.10%2B-yellow)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

**OmniAPI** is a local dispatch center for external AI models. One background daemon (`omni serve`, bound to `127.0.0.1`) gives you three doors onto the same store: a web GUI (live board of agent runs, dispatch form, streaming chat, cost ledgers), an MCP server for Claude Code / Claude Desktop (19 tools: image, transcription, text, chat, speech, music, agent runs), and the `omni` CLI. Agent runs are delegated to the vendor's own headless harness — Claude Code, Codex CLI or Gemini CLI — chosen by model. Tested on Windows 11. MIT licensed.

---

## 這是什麼

OmniAPI 是一個跑在自己電腦上的常駐服務。它做三件事：

| 能力 | 說明 | 從哪裡用 |
|---|---|---|
| **派工** | 把一件事交給 headless agent（Claude Code、Codex CLI、Gemini CLI，依模型自動挑）。它會進你指定的資料夾讀檔、改檔、跑指令，做完回報；可以追問、可以中途中止 | GUI、MCP `run_agent`、`omni run` |
| **聊天** | 跟任何文字模型多輪對話，逐字顯示，每一則都能換模型 | GUI、MCP `chat`、`omni chat` |
| **多模態工具** | 生圖與改圖、語音轉錄、語音合成、音樂生成、單次文字補全 | MCP 工具（在 Claude Code／Claude Desktop 裡使用） |

另外有一面**看板**：正在跑的 agent 在做什麼、跑過哪些、兩本帳各花了多少。

![看板：執行中的派工票券與即時活動流](gui/screenshots/board.png)

| 聊天 | 費用 |
|---|---|
| ![聊天頁：每一則回覆標示模型、耗時、token 與費用](gui/screenshots/chat.png) | ![費用頁：派工帳與聊天・生成帳](gui/screenshots/costs.png) |

<sub>截圖裡的專案、對話與金額都是示範資料。</sub>

支援的供應商：OpenAI、Anthropic、Google Gemini、DeepSeek、OpenRouter（長尾模型）、ElevenLabs、kie.ai（Suno）。模型名單在啟動時向各家即時查詢，新模型上線就能用；`cheap`／`standard`／`strong` 三個等級別名會對到當下設定的模型。

## 需求

- Windows 11（開發與測試都在這上面；其他平台沒有驗證過）
- [uv](https://docs.astral.sh/uv/)（會自己準備 Python 3.10）
- Node.js 20.19 以上或 22.12 以上（只有從原始碼打包 GUI 時需要；下載 Release 的 zip 就不用）
- 至少一家供應商的 API key
- 要派工的話，另外安裝對應的 harness CLI 並登入：
  [Claude Code](https://docs.claude.com/claude-code)（`claude`）、[Codex CLI](https://github.com/openai/codex)（`codex`）、[Gemini CLI](https://github.com/google-gemini/gemini-cli)（`gemini`）。只裝你會用到的就好

## 安裝

### 方法一：下載 Release（不用 Node）

到 Releases 下載 `omniapi-v1.0.0.zip`，解壓縮後：

```bash
cd omniapi-v1.0.0/mcp
uv sync
```

### 方法二：從原始碼

```bash
git clone https://github.com/TipsyDrifter/omniapi.git
cd omniapi/mcp
uv sync
cd ../gui
npm ci
npm run build
```

## 設定金鑰

在 `mcp/` 底下把 `.env.example` 複製成 `.env`，填入你有的 key，並把對應的 `ENABLED` 設成 `true`：

```
PROVIDERS__OPENAI__API_KEY=sk-...
PROVIDERS__OPENAI__ENABLED=true

PROVIDERS__DEEPSEEK__API_KEY=...
PROVIDERS__DEEPSEEK__ENABLED=true
```

沒有的那幾家維持 `ENABLED=false`。範例檔裡 OpenAI 預設是開的，沒有 OpenAI key 的話記得關掉。
`.env` 只留在你的電腦上，不要提交到任何 repo。

## 啟動

```bash
cd mcp
uv run omni serve
```

服務會在背景啟動，綁在 `http://127.0.0.1:7788`。**安裝後第一次啟動大約要一分鐘**，之後幾秒就好；看到 `started: pid …` 就是起來了。常用指令：

| 指令 | 作用 |
|---|---|
| `uv run omni serve` | 啟動（已經在跑就不會重複啟動） |
| `uv run omni status` | 看有沒有在跑、載入了哪些供應商 |
| `uv run omni stop` | 停止 |
| `uv run omni autostart install` | 登入 Windows 時自動啟動 |

紀錄檔在 `~/.omniapi/logs/daemon.log`，資料庫在 `~/.omniapi/omniapi.db`。

沒有填任何 key 也啟動得起來，但派工和聊天會失敗。`omni status` 的 `providers` 顯示 `(none …)`、或網頁最下方出現「尚未設定 API key」，就是這個情況：回上一節填好 `.env` 再重新啟動。

## 三個入口

### 瀏覽器

打開 <http://127.0.0.1:7788/>。

- **看板**：執行中的 agent 以票券顯示，右邊是它的即時活動流；下面是歷史與兩本帳
- **＋新對話**：選「派工」或「聊天」。派工要選模型和工作目錄；聊天只要選模型
- **聊天**：對話清單與對話內容，輸入列上方可以換下一則要用的模型、改 system prompt，可以匯出成 markdown
- **費用**：兩本帳的明細

操作細節見 [USER_GUIDE.md](USER_GUIDE.md)。

### Claude Code（MCP）

```bash
cd mcp
uv run omni mcp-config --apply
```

這會把 Claude Code 的設定指向 `http://127.0.0.1:7788/mcp`，重開 Claude Code 後生效。不加 `--apply` 只會印出設定內容。
工具清單與參數見 [mcp/README.md](mcp/README.md)；`skill/omniapi/` 是給 Claude 讀的使用說明（skill），可以放進 `~/.claude/skills/`。

Claude Desktop 可以安裝 Release 裡的 `omniapi-mcp.dxt`。它是獨立的 stdio 版 MCP server，不需要常駐服務，但也就沒有看板。

### 終端機

```bash
uv run omni run "把 README 的安裝步驟檢查一遍" --model cheap --cwd C:\path\to\project
uv run omni runs
uv run omni chat "這段程式在做什麼？" --model standard
uv run omni chat
```

`omni run` 會即時印出 agent 的動作；`omni chat` 不帶訊息會進入互動模式。每個指令都有 `--help`。

在 Windows 上把 `omni chat` 的輸出導到檔案或管線時，中文可能變成亂碼；先設定環境變數 `PYTHONIOENCODING=utf-8` 就好。

## 花費與安全

- **這會花錢**。派工和聊天都是用你自己的 API key 呼叫供應商。看板的兩本帳會記下每一筆：派工帳是 agent 回報的費用，聊天・生成帳是每呼叫一次模型記一筆
- **Claude 模型預設走 Claude Code 的訂閱登入**，不扣 API 餘額；要用 API 計費得在派工時明確選擇
- **服務沒有登入機制**，所以只綁 `127.0.0.1`。不要把 7788 埠開放到網路上
- **派工的 agent 會真的改你的檔案、跑指令**。預設是「自動接受改檔」；`yolo` 會略過所有權限確認，除非你清楚後果否則不要開。工作目錄請指定到你願意讓它動的資料夾
- 如果供應商的 key 曾經出現在紀錄檔或畫面分享裡，到該供應商後台換一把

## 不花錢先試試：離線沙盒

想先看看介面、或在開發時測試，可以用離線沙盒模式。它不會呼叫任何供應商：

```bash
cd mcp
OMNIAPI_HOME=/path/to/scratch-home OMNIAPI_DEV=1 OMNIAPI_OFFLINE=1 uv run omni serve --port 7799
```

- `OMNIAPI_HOME`：資料放到另一個資料夾，不碰正式的資料庫
- `OMNIAPI_DEV=1`：開啟兩個替身——`echo` 模型（把固定的稿子逐字吐回來）和「重播」harness（把舊的 run 重新播一次）
- `OMNIAPI_OFFLINE=1`：拒絕所有會打到真供應商的呼叫

PowerShell 要先用 `$env:OMNIAPI_DEV = "1"` 這種寫法設定環境變數。

沙盒和正式服務可以同時開著。停沙盒時帶上同一個埠：`uv run omni stop --port 7799`。其他指令（`status`、`chat`、`run`）也都用 `--port` 指定要連哪一台。

## 專案結構

```
mcp/     後端：常駐服務（FastAPI）、MCP server、omni CLI、各家供應商與 harness 的轉接
gui/     前端：React + Vite + TypeScript
skill/   給 Claude 讀的 skill
scripts/ 打包與發布用的腳本
```

開發：

```bash
cd mcp && uv run python -m pytest tests/unit -q
cd gui && npm run dev            # 連正式服務（7788）
cd gui && npm run dev:sandbox    # 連離線沙盒（7799）
```

## 目前的限制

- 只在 Windows 11 上測試過
- 圖片、語音、音樂、轉錄目前只能透過 MCP 工具使用，GUI 還沒有入口
- 還不支援影片
- 聊天不能附檔案；不能刪除對話，只能封存
- 沒有桌面應用程式的外殼，GUI 是瀏覽器頁面

## 授權

MIT，見 [LICENSE](LICENSE)。

MCP server 的生圖部分最初衍生自 [image-gen-mcp](https://github.com/lansespirit/image-gen-mcp)。
