---
name: omniapi
description: 透過 OmniAPI MCP（mcp__omniapi-mcp__*）呼叫外部 AI 模型：生圖／改圖（gpt-image、Nano Banana、經 OpenRouter 的 FLUX／Seedream／Grok Imagine／Qwen 等）、語音轉錄、文字補全與多輪聊天（GPT-6／GPT-5.x／Claude／Gemini／DeepSeek／OpenRouter，可用等級別名 cheap／standard／strong）、TTS 語音合成、音樂生成（Suno V6／ElevenLabs Music）、短影片生成（generate_video：文生影片、首幀／尾幀圖生影片，經 OpenRouter，有每支花費上限），以及把任務派給 headless agent（run_agent：Claude Code／Codex／Gemini CLI）。呼叫任何 mcp__omniapi-mcp__* 工具前先讀本 skill 挑對工具、模型與參數。觸發：用 omniapi、生圖、改圖、FLUX、Seedream、轉錄、字幕、問 GPT-6、問 GPT-5、問 Claude、問 Gemini、問 DeepSeek、等級 cheap／standard／strong、列模型清單、TTS、語音合成、配音、生音樂、作曲、寫歌、Suno、人聲分離、影片、生影片、做影片、圖生影片、generate_video、呼叫外部模型、跟外部模型聊天、chat、派工、run_agent、外部 agent、omni run、omni chat。不用於 VPick 與 Remotion。
---

# OmniAPI MCP — 使用指南

一台統一 MCP 伺服器（工具前綴 `mcp__omniapi-mcp__*`），把 **6 種能力**（圖、轉錄、文字、語音、音樂、影片）加上派工與聊天收進 **20 個工具**，背後路由到 OpenAI / Anthropic / Google / DeepSeek / OpenRouter / ElevenLabs / Suno(kie.ai)。

> 先用 `health_check` 確認上線，`server_info` 看能力，`list_available_models(modality=...)` 看**當前**可用模型，再動手。

> 🖥 **這些工具背後是一個常駐服務**（`http://127.0.0.1:7788`）。工具整組叫不到（連線被拒）＝服務沒在跑：裝了桌面版的請使用者從開始功能表打開 OmniAPI（或在系統匣選「重啟服務」）；用 zip 裝的請他在 `mcp/` 下執行 `uv run omni serve`。之後重新連線 MCP。同一個服務也有網頁：看板 `http://127.0.0.1:7788/`、聊天 `/chat`、費用 `/costs`、模型 `/models`、設定 `/settings`——派工和聊天做的事都會出現在那裡。

> ⏱ **長任務會回「取件單」**：生成類工具（尤其 Suno 音樂、高品質/4K 圖）若跑超過 ~45 秒，會回 `{status:"running", task_id:"..."}` 而非結果——這是為了避開 client 的 ~60 秒硬 timeout（Desktop/Cowork 尤其）。拿到取件單就用 `get_job_result(task_id)` 領成品；若 status 仍 running，等 ~20–30 秒再領（影片：等一兩分鐘再領）。快的工具（文字、低品質圖、ElevenLabs）直接回結果、不會有取件單。**影片幾乎一定回取件單**（`task_id` 是 `video_…`），而且這張取件單在服務重啟後照樣領得到。

## 🛠 工具速查（20 個）

| 工具 | 能力 | 輸入 → 輸出 |
|---|---|---|
| `generate_image` | 圖像生成 | 文字 → 圖檔 URL |
| `edit_image` | 圖像編輯 | 圖(+遮罩) + 指令 → 圖檔 URL |
| `transcribe_audio` | 語音轉文字 | 音檔 → 文字 |
| `complete_text` | 文字 / 對話 | prompt（或 messages）→ 文字 |
| `generate_speech` | 語音合成 (TTS) | 文字 → 音檔 |
| `generate_music` | 音樂生成 | 文字 → 歌曲（Suno / ElevenLabs） |
| `edit_music(action)` | 音樂變形 (Suno) | 見 §6，6 種 action |
| `music_lyrics(action)` | 歌詞 (Suno) | 見 §6，2 種 action |
| `music_utility(action)` | WAV / MV 轉出 (Suno) | 見 §6，2 種 action |
| `compose_music(action)` | 編曲藍圖 (ElevenLabs) | 見 §6，2 種 action |
| `generate_video` | 影片生成（經 OpenRouter；Gemini Omni 用 Google key 直連） | 文字（＋首幀／尾幀）→ 影片檔；**有每支花費上限**，見 §9 |
| `list_available_models` | 診斷 | `modality` → **跨模態**即時模型名單 |
| `run_agent`／`get_run`／`list_runs`／`cancel_run` | 派工（agent run） | 任務 → `run_id` → 輪詢事件與回報，見 §7 |
| `chat` | 多輪聊天 | 訊息（＋`conversation_id`）→ 回覆＋`conversation_id`，對話留在 GUI，見 §8 |
| `health_check` | 診斷 | — → 伺服器 + provider 健康 |
| `server_info` | 診斷 | — → capabilities + 設定 |
| `get_job_result` | 領取 | task_id → 長任務成品（取件單機制，見上方 ⏱）|

### `action` 分流工具的必填參數一覽

四個音樂工具用 `action` 分流，**每個 action 的必填欄位不同**（照 server.py 的定義）：

| 工具 | `action` | 必填 | custom_mode=true 時另需 |
|---|---|---|---|
| `edit_music` | `extend` | `audio_id` | `prompt`、`style`、`title`、`continue_at` |
| | `cover` | `upload_url`、`prompt` | `style`、`title` |
| | `upload_extend` | `upload_url` | `prompt`、`style`、`title`、`continue_at` |
| | `add_instrumental` | `upload_url`、`title`、`tags`、`negative_tags` | （不適用） |
| | `add_vocals` | `upload_url`、`prompt`、`title`、`style`、`negative_tags` | （不適用） |
| | `separate_vocals` | `task_id`、`audio_id`、`separation_type` | （不適用） |
| `music_lyrics` | `generate` | `prompt`（≤200 字元） | — |
| | `timestamped` | `task_id`、`audio_id` | — |
| `music_utility` | `convert_to_wav` | `task_id`、`audio_id` | — |
| | `create_music_video` | `task_id`、`audio_id`（可選 `author`／`domain_name`，各 ≤50 字元） | — |
| `compose_music` | `create_plan` | `prompt`（可選 `music_length_ms`、`source_composition_plan`） | — |
| | `compose` | `prompt` **或** `composition_plan`（**二擇一、互斥**） | — |

## 📚 詳細參數子 skill（要設定具體參數時必讀）

本檔是**入口索引**；各能力的**完整參數、可選值、設計哲學、定價、雷區**在對應子 skill：

| 要做 | 讀這份 | 內含重點 |
|---|---|---|
| 生圖 / 改圖 | [`references/image.md`](references/image.md) | gpt-image 2.5／2／1.5 系與 Nano Banana 全參數、**「先 low 後 high」成本哲學**、4K / 透明、尺寸限制 |
| 語音轉文字 | [`references/transcription.md`](references/transcription.md) | gpt-transcribe 與棄用 gpt-4o-* 全參數、response_format 矩陣、字幕 / 時間戳 |
| 文字 / 對話 | [`references/text-chat.md`](references/text-chat.md) | 等級別名、GPT-5.x/6 reasoning 參數、DeepSeek thinking、結構化輸出、**temperature 陷阱** |
| 語音合成 TTS | [`references/speech.md`](references/speech.md) | OpenAI / ElevenLabs / Gemini TTS 全參數、voice 清單、instructions 語氣控制、output_format |
| 生音樂 / 作曲 / 寫歌 | [`references/music.md`](references/music.md) | Suno V6 全套（`generate_music` + 三個 action 工具）+ ElevenLabs composition plan、輪詢、audio_id 依賴鏈 |
| 生影片 | [`references/video.md`](references/video.md) | `generate_video` 全參數、每支上限與 `max_cost_usd`、取件單、回傳欄位、計價方式 |

## 🔑 供應商與金鑰

| Provider | 能力 | 金鑰 |
|---|---|---|
| **OpenAI** | 圖像 · 轉錄 · 文字（GPT-6／5.x）· 語音(TTS) | `PROVIDERS__OPENAI__API_KEY` |
| **Anthropic** | 文字（Claude） | `PROVIDERS__ANTHROPIC__API_KEY` |
| **Google (Gemini)** | 圖像（Nano Banana）· 文字 · 語音(TTS) | `PROVIDERS__GEMINI__API_KEY` ＝ **AI Studio API key**（不再是 service-account 檔案路徑） |
| **DeepSeek** | 文字 | `PROVIDERS__DEEPSEEK__API_KEY` |
| **OpenRouter** | 文字長尾（Kimi、GLM、MiniMax、Qwen、Mistral、Llama…）· 圖像（FLUX、Seedream、Grok Imagine、Qwen、Recraft、MAI…）· **影片**（目前唯一的影片來源） | `PROVIDERS__OPENROUTER__API_KEY` |
| **ElevenLabs** | 語音(TTS) · 音樂(Music) | `PROVIDERS__ELEVENLABS__API_KEY`（Music 另需**付費方案**） |
| **Suno** (via kie.ai) | 音樂生成 | `PROVIDERS__KIE__API_KEY` |

每家都要配一個 `PROVIDERS__<家>__ENABLED=true` 才會註冊。沒填 key 的家不會出現在 `list_available_models` 的 `configured_providers` 裡。

> 🔧 **key 也可以在網頁的設定頁（`/settings`）貼**：存在使用者資料夾的 `settings.json`、蓋在 `.env` 之上、**不用重啟就生效**（貼了就當作啟用）。缺某家的 key 時，請使用者到設定頁貼，不必改 `.env`、也不必重連 MCP。`list_available_models` 的 `providers.<家>.get_key` 是那一家拿 key 的網址與說明，可以直接轉告。**不要向使用者索取 key、也不要替他把 key 寫進任何檔案**。

---

## 1️⃣ 圖像生成 — `generate_image`

**參數**：`prompt`★、`model`、`size`、`quality`、`output_format`、`compression`、`background`、`moderation`、`n`(多圖 1-10，>1 回傳 `images[]`；Gemini 僅 1)、`user`(OpenAI)、`style`(vivid/natural，OpenAI 限定)。
**Gemini (Nano Banana) 專屬**：`image_size`(`512`/`1K`/`2K`/`4K`，**必須大寫 K**，預設 2K)、`aspect_ratio`(`16:9`/`9:16`/`21:9`/`2:3`/`4:5`… 解鎖 WxH 到不了的比例)、`person_generation`(`dont_allow`/`allow_adult`/`allow_all`)。
**OpenRouter 圖片模型**：`model` 直接寫 OpenRouter 的 id（如 `black-forest-labs/flux-3-image`、`x-ai/grok-imagine-image-2.0`）；`image_size`＝模型列的解析度級距、`aspect_ratio`＝模型列的比例、`quality`、`seed`（模型 `image_params.seed` 為 true 才吃）——**每個模型收什麼看 `list_available_models(modality="image")` 的 `image_params`**，別猜。
**歷史遺留、現已全部被忽略**：`safety_filter_level`、`enhance_prompt`、`guidance_scale`——為舊呼叫端向後相容而保留，任何現役模型都不吃（`seed` 只有部分 OpenRouter 模型吃）。

**模型**（名單是即時的，用 `list_available_models(modality="image")` 看現況）：

| model | 特點 | 狀態 |
|---|---|---|
| `gpt-image-2.5-sunburst` | 生圖／改圖榜雙冠，最長邊 ≤3840、多參照圖 | current |
| `gpt-image-2.5-flare` | 同上，榜二 | current |
| `gpt-image-2` | 自由解析度（含 4K 3840×2160）、文字渲染強 | current；**不支援透明背景** |
| `gpt-image-1.5` | 較快、**真‧透明背景**、吃 `input_fidelity` | ⚠️ deprecated，**2026-12-01 關閉** |
| `gpt-image-1-mini` | 最便宜 | ⚠️ deprecated，**2026-12-01 關閉** |
| `gpt-image-1` | 舊版 | ⚠️ **deprecated，2026-10-23 關閉** |
| `gemini-3.1-flash-image`（別名 `nano-banana-2`）| Gemini 預設；512/1K/2K/4K、最多 14 張參照圖 | current |
| `gemini-3-pro-image`（別名 `nano-banana-pro`）| Nano Banana Pro，1K/2K/4K | current |
| `gemini-3.1-flash-lite-image`（別名 `nano-banana-2-lite`）| 便宜版 | current |
| `gemini-2.5-flash-image`（別名 `nano-banana`）| 初代，實質 1K | ❌ retired（2026-10-02 關閉） |
| OpenRouter（例子，實際以名單為準）：`black-forest-labs/flux-3-image`、`bytedance-seed/seedream-5-0-pro`、`x-ai/grok-imagine-image-2.0`、`qwen/qwen-image-3`、`microsoft/mai-image-2.6`… | 名單與價格向 OpenRouter 現查、每天更新；用現有的 OpenRouter key | 依名單 |

> OpenRouter 圖片模型的注意事項：**已經直連的 OpenAI、Google（那家的 key 有設）不會再列 OpenRouter 的同家那一份**；只出 SVG 向量圖的（Recraft 的向量款）標為這一版不支援；費用回傳的是 OpenRouter 實際收的（有時比名單低）。

**範例**：
```
mcp__omniapi-mcp__generate_image(prompt="a serene mountain lake at dawn, oil painting", model="gpt-image-2", size="3840x2160", quality="high")
mcp__omniapi-mcp__generate_image(prompt="minimalist fox line-art icon, centered", model="gpt-image-1.5", background="transparent")
mcp__omniapi-mcp__generate_image(prompt="cinematic cyberpunk alley", model="nano-banana-2", image_size="4K", aspect_ratio="21:9")
mcp__omniapi-mcp__generate_image(prompt="product shot of a ceramic mug, soft light", model="black-forest-labs/flux-3-image", aspect_ratio="4:5")
```
- 4K / 海報 → `gpt-image-2` + `size=3840x2160`，或 Nano Banana + `image_size="4K"`
- 透明 PNG（logo/貼圖）→ `gpt-image-1.5` + `background=transparent`
- 大量草稿 → `gpt-image-1-mini` 或 `quality=low`

> ⭐ **「先 low 後 high」**：gpt-image-2 的 `high` 比 `low` **貴約 35×**（$0.211 vs $0.006）。先用 `quality=low` 快速便宜試構圖，定稿才切 `high`——省最多。
> 📖 全參數、各模型 size/quality/透明矩陣、Nano Banana、自訂尺寸限制 → **[`references/image.md`](references/image.md)**。

## 2️⃣ 圖像編輯 — `edit_image`
**參數**：`image_data`(base64 或 data URL) **或** `image_path`(本機檔路徑，STDIO 下**首選**——全解析度參照、免塞 base64；二擇一必填)、`prompt`★、`mask_data`(白=改/黑=留)、`model`、`size`、`quality`、`output_format`、`compression`、`background`、`input_fidelity`(`high` 貼近原圖五官/風格，僅 gpt-image-1 系)、`additional_images`(多張參照圖合成，最多 16 張)＋`additional_image_paths`(本機路徑版)、`user`、`image_size`／`aspect_ratio`(**只對 OpenRouter 模型有效**，值取自該模型的 `image_params`)。

> 🔀 **`edit_image` 依 model 路由**：`gpt-image-*` → OpenAI（可用遮罩）；Nano Banana（`gemini-*-image`／`nano-banana-*`）→ Gemini（參照圖走 `additional_images`，不吃遮罩）；OpenRouter id → OpenRouter，限 `image_params.max_references > 0` 的模型（來源圖＋`additional_images` 一起當參照圖送，不吃遮罩；不收參照圖的模型不能改圖）。
> ⚠️ **預設 model 以 `IMAGES__DEFAULT_MODEL` 為準**（出廠 `gpt-image-2`）——要哪個就明寫。

```
mcp__omniapi-mcp__edit_image(image_path="C:/path/photo.png", prompt="replace the sky with a starry night", model="gpt-image-2")
mcp__omniapi-mcp__edit_image(image_data="data:image/png;base64,...", prompt="keep her face, change outfit to red", model="gpt-image-1.5", input_fidelity="high")
mcp__omniapi-mcp__edit_image(image_path="C:/path/photo.png", prompt="turn it into a watercolor", model="black-forest-labs/flux-3-image", aspect_ratio="1:1")
```

## 3️⃣ 語音轉文字 — `transcribe_audio`
**參數**：`audio_path`(本機檔路徑，**首選**) 或 `audio_data`(base64)、`model`、`language`(ISO-639-1 如 en/zh/ja)、`prompt`、`response_format`、`temperature`、`timestamp_granularities`(`["word"]` 取**字級時間戳**，需 `verbose_json`+`whisper-1`)、`chunking_strategy`(長音檔自動切塊 `"auto"` 或 VAD dict)、`include`(`["logprobs"]`，gpt-4o-transcribe+json)、`known_speaker_names`/`known_speaker_references`(講者分離指名)。

| model | 用途 | 狀態 |
|---|---|---|
| `gpt-transcribe` ⭐預設 | 現役唯一 current 的轉錄模型；純文字 / json | current |
| `whisper-1` | 唯一支援 **srt / vtt / verbose_json** 與 word 級時間戳 | ⚠️ deprecated，**2027-02-26 關閉** |
| `gpt-4o-transcribe` | 可配 `include=["logprobs"]` | ⚠️ deprecated，2027-02-26 關閉 |
| `gpt-4o-mini-transcribe` | 便宜版 | ⚠️ deprecated，2027-02-26 關閉 |
| `gpt-4o-transcribe-diarize` | **講者分離**，配 `response_format="diarized_json"` | ⚠️ deprecated，2027-02-26 關閉 |

> ⚠️ 現實是：**字幕（srt/vtt）、時間戳、講者分離目前只有棄用模型做得到**，官方尚未公布替代路徑。純文字逐字稿請用 `gpt-transcribe`；需要時間軸就還是得叫 `whisper-1`（記得它會在 2027-02-26 消失）。

```
mcp__omniapi-mcp__transcribe_audio(audio_path="C:/path/clip.mp3", model="gpt-transcribe", language="zh")
mcp__omniapi-mcp__transcribe_audio(audio_path="C:/path/clip.mp3", model="whisper-1", response_format="srt", language="zh")
```
> 📖 完整參數、各模型 response_format 矩陣、25MB 限制 → **[`references/transcription.md`](references/transcription.md)**。

## 4️⃣ 文字 / 對話 — `complete_text`
**參數**：`prompt`★(或 `messages` 多輪)、`model`(**可給等級別名**)、`system`、`reasoning_effort`、`verbosity`、`max_completion_tokens`、`response_format`、`thinking`(bool，DeepSeek 思考開關)。
**Function calling**：`tools`、`tool_choice`、`parallel_tool_calls` → 模型呼叫工具時回傳 `tool_calls`，把結果用 `messages` 餵回續跑。
**取樣/其他**：`temperature`/`top_p`/`seed`/`stop`(⚠️ GPT-5.x/6 reasoning 模型一律自動丟棄這四個)、OpenAI 限定 `store`/`metadata`/`service_tier`/`prompt_cache_key`/`safety_identifier`。
回傳 `text` + `model` + `requested_model` + `provider` + `tool_calls` + `usage` + `cost_usd`（reasoning 模型另回 `reasoning`）。

### ⭐ 先用等級別名，別寫死型號

| 別名 | 內建對應 | 用途 |
|---|---|---|
| `cheap` | `deepseek-flash` | **預設**；日常問答、批量、整理 |
| `standard` | `gemini-3.8-flash` | 一般任務、要快要穩 |
| `strong` | `gpt-6-sol` | 最難的推理 / 長文 / 規劃 |

**使用者可以在設定頁改這張表**（例如只有 OpenAI key 的人把三個都改成 OpenAI 的模型），改了立刻生效、不用重連 MCP。**實際對應以 `list_available_models` 回傳的 `tiers` 為準**，上表只是出廠值；寫別名的 prompt 不用跟著改。`complete_text`、`chat`（新對話）、語音、音樂、轉錄沒給 `model` 時用的「預設模型」也是設定頁改得動的。要指名再從下面挑：

| 家 | 代表模型 |
|---|---|
| OpenAI | `gpt-6-astra`（最強）、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.5` / `gpt-5.5-pro`、`gpt-5.4` / `-pro` / `-mini` / `-nano`、`gpt-5.3-codex` |
| Anthropic | `claude-fable-5-1`、`claude-fable-5`、`claude-opus-5-5`、`claude-opus-5`、`claude-sonnet-5`、`claude-haiku-4-5-20251001` |
| Google | `gemini-3.8-flash`、`gemini-3.7-flash`、`gemini-3.1-pro-preview` |
| DeepSeek | `deepseek-flash`（**正式 id**；`deepseek-v4-flash` 是別名）、`deepseek-v4-pro` |
| OpenRouter | 命名空間式 `vendor/model`，如 `moonshotai/kimi-k3`；名單全靠 discovery |

> 📡 **名單是即時的**：啟動時向各家 `/models` 問「現在哪些在線」，再和人工維護的定價／能力覆蓋層合併。要看現況跑 `list_available_models(modality="text")`；加 `include_retired=true` 看已下架的、`include_snapshots=true` 看帶日期的 snapshot id、`refresh=true` 強制重抓（平常吃 24h 快取）。

```
mcp__omniapi-mcp__complete_text(prompt="用三句話解釋什麼是 MCP", model="cheap")
mcp__omniapi-mcp__complete_text(prompt="幫我規劃這個重構", model="strong", reasoning_effort="high", verbosity="low")
```

> 📖 **完整參數**（reasoning 特殊參數、DeepSeek thinking 模式、結構化輸出、定價）見 **[`references/text-chat.md`](references/text-chat.md)**。

**三個最常踩的雷（omniapi 已替你處理大半）：**
- **GPT-5.x/6 reasoning 模型禁送 sampling 參數** — `temperature`/`top_p`/`seed`/`stop` 會回 **HTTP 400**。✅ omniapi **自動丟棄**（記 log）、`system` 自動轉 `developer` role、`max_completion_tokens` 自動對應 DeepSeek 的 `max_tokens`。要調風格用 `reasoning_effort`(`none`/`low`/`medium`/`high`/`xhigh`) + `verbosity`(`low`/`medium`/`high`)。
- **DeepSeek thinking 模式 sampling 參數「靜默忽略」** — 不報錯但無效。✅ 用 `thinking=true/false` 開關思考。
- **DeepSeek 多輪 + 思考** — 歷史訊息的 `reasoning_content` 預設要剝掉，只有 tool-call 串接的回合原樣傳回，否則回 400。

## 5️⃣ 語音合成 (TTS) — `generate_speech`
**參數**：`text`★、`voice`、`model`、`output_format`、`instructions`(⭐gpt-4o-mini-tts 語氣/情緒控制)、`speed`(tts-1/hd 語速 0.25–4.0)。
**ElevenLabs 專屬**：`voice_settings`(stability/similarity_boost/style/use_speaker_boost/speed)、`language_code`、`seed`、`previous_text`/`next_text`、`apply_text_normalization`、`enable_logging`(false=零留存)。存檔到 `storage/audio/<date>/`，回傳 `audio_path`。

| model | provider | 備註 |
|---|---|---|
| `eleven_flash_v2_5` ⭐ElevenLabs 預設 | ElevenLabs | ~75ms 超低延遲 |
| `eleven_v4` / `eleven_v4_turbo` | ElevenLabs | 最新一代、最具表現力 / 半價版 |
| `eleven_v3` / `eleven_v3_conversational` | ElevenLabs | 前一代最進階 / 最具表現力即時 |
| `eleven_multilingual_v2` / `eleven_flash_v2` | ElevenLabs | 最擬真 / 英文快速 |
| `eleven_turbo_v2_5` | ElevenLabs | ⚠️ deprecated → 改用 `eleven_flash_v2_5`（未公布關閉日） |
| `gpt-4o-mini-tts` ⭐OpenAI 預設 | OpenAI | voice: alloy/echo/fable/onyx/nova/shimmer…；吃 `instructions` |
| `tts-1` / `tts-1-hd` | OpenAI | 吃 `speed`、不吃 `instructions`；⚠️ deprecated，**2027-01-06 關閉** |
| `gemini-3.8-flash-tts` ⭐Gemini 預設 / `gemini-3.8-flash-lite-tts` | Google | voice 給 prebuilt 名稱如 `Kore`（共 30 個）；原始輸出是 PCM：機器上有 `ffmpeg` 時 `mp3_*` 會轉成 MP3，否則回 WAV |
| `gemini-3.1-flash-tts-preview` | Google | ⚠️ deprecated（legacy preview，未公布關閉日） |

```
mcp__omniapi-mcp__generate_speech(text="晚安，今天辛苦了", model="gpt-4o-mini-tts", voice="nova", instructions="溫柔放慢、像哄睡的語氣")
```
> 模型路由自動：`eleven_*`→ElevenLabs、`gpt-4o-mini-tts`/`tts-1*`→OpenAI、`gemini-*-tts`→Gemini。
> ⭐ `instructions` **只對 gpt-4o-mini-tts 有效**；tts-1/hd 改吃 `speed`；ElevenLabs 細調用 `voice_settings`。
> ⚠️ Gemini TTS：`output_format` 填 `mp3_*`（預設）→ 有 ffmpeg 給 MP3、沒有給 WAV；填 `wav` 給 WAV；填 `pcm_*` 給裸 PCM。以回傳的 `output_format`／副檔名為準。
> 📖 全參數、voice 清單、instructions 用法、output_format → **[`references/speech.md`](references/speech.md)**。

## 6️⃣ 音樂 — `generate_music` ＋ 三個 action 工具

**三個後端**：**Suno**（via kie.ai，內部輪詢，需 `PROVIDERS__KIE__API_KEY`）＋ **ElevenLabs Music**（同步，需**付費方案**）＋ **Google Lyria**（用 Gemini 的 key，需**付費層**）。

**`generate_music`**：`prompt`★、`model`、`instrumental`、`output_format`/`music_length_ms`(ElevenLabs)、`custom_mode`+`style`+`title`+`vocal_gender`(Suno)、`negative_tags`。存到 `storage/music/<date>/`，Suno 另回 **`audio_id`** 供串接；Suno 一次給兩首，兩首都存，`tracks` 列出每一首（各自的 `audio_path`／`audio_id`），`track_count` 是首數（使用者在 `settings.json` 設 `music.suno_all_tracks=false` 時只收第一首、沒有 `tracks`）。

**模型**：
- Suno（kie.ai）：`V6`(預設)、`V6_MINI`(快/輕)、`V6_WILD`(實驗)。舊 id `V4`／`V4_5`／`V4_5PLUS`／`V4_5ALL`／`V5`／`V5_5` 上游已 **retired**，仍可送但會記 warning、隨時可能失效。
- ElevenLabs：`music_v1`(預設)、`music_v2`、`music_v2_5`(最高品質)。
- Google Lyria：`lyria-3.5`（完整歌曲，$0.08／首）、`lyria-3-clip-preview`（固定 30 秒，$0.04／首）。**只吃 `prompt` 與 `instrumental`**：歌詞用 `[Verse]`／`[Chorus]` 標籤直接寫進 prompt、長度用時間標記（`[0:00 - 0:10] Intro: …`）引導；`style`／`title`／`music_length_ms` 等對它無效。回傳多一個 `lyrics`（模型寫的歌詞與結構）與 `cost_usd`。

**`edit_music(action)`**（Suno，6 種 action）：`extend` 延長既有曲、`cover` 翻唱上傳音檔、`upload_extend` 延長上傳音檔、`add_instrumental` 加伴奏、`add_vocals` 加人聲、`separate_vocals` 分軌。**各 action 的必填欄位見上方速查表。** `add_instrumental`／`add_vocals` 只吃 V6 家族。
**`music_lyrics(action)`**：`generate` 寫歌詞（`prompt` ≤200 字元，另存 .txt）、`timestamped` 取字級時間軸（同步）。
**`music_utility(action)`**：`convert_to_wav`、`create_music_video`。
**`compose_music(action)`**（ElevenLabs）：`create_plan` 產可編輯編曲藍圖 → `compose` 用藍圖或 prompt 生成 + 回 metadata。

```
mcp__omniapi-mcp__generate_music(prompt="upbeat lo-fi hip-hop beat for studying", model="V6")
mcp__omniapi-mcp__generate_music(prompt="jazzy piano ballad", model="V6", custom_mode=true, style="smooth jazz, slow", title="Midnight Keys", vocal_gender="f")
mcp__omniapi-mcp__generate_music(prompt="city pop about a rainy evening, female vocal\n[Verse]\nRain on the window…", model="lyria-3.5")
mcp__omniapi-mcp__edit_music(action="separate_vocals", task_id="...", audio_id="...", separation_type="split_stem")
```

> ⚠️ **依賴鏈**：`extend` 要先有 `audio_id`；`convert_to_wav`／`create_music_video`／`separate_vocals`／`timestamped` 要 prior `task_id`+`audio_id`；`cover`／`upload_extend`／`add_*` 要公開 `upload_url`（≤8 分鐘）。先跑 `generate_music` 拿 id 再串。
> ⚠️ Suno 內部輪詢到完成（一次呼叫就回檔案）；太短/模糊 prompt 可能失敗退款（用具體完整 prompt）。ElevenLabs Music 免費版會 402（需付費方案）。
> 📖 全參數、輪詢機制、依賴鏈 → **[`references/music.md`](references/music.md)**。

---

## 7️⃣ 派工（agent run）— `run_agent` / `get_run` / `list_runs` / `cancel_run`

把任務交給**有工具的 headless agent**（Read／Write／Edit／Bash，在指定 cwd 裡幹活）。原廠 harness 自動路由：DeepSeek／Claude／OpenRouter 模型走 Claude Code、OpenAI 走 Codex CLI、Google 走 Gemini CLI。

> ⚠️ 派工會花使用者的錢、也會真的改檔案。使用者沒有明確要求派給外部 agent 時，不要主動用 `run_agent`。

- `run_agent(task★, model="cheap", cwd, title, yolo=false, search=true, harness, max_turns, resume_run_id, dispatcher, billing)` → **立刻回 `run_id`**（不等）。`model` 用等級別名 `cheap`／`standard`／`strong`（出廠是 deepseek-flash／gemini-3.8-flash／gpt-6-sol，使用者可能在設定頁改過，以 `list_available_models` 的 `tiers` 為準）或任何 id。`dispatcher` 填派工的對話標題（看板「來源」欄）。`task` 要寫成完整 brief：目標、動哪些檔、限制、要什麼回報；查證任務要求附 URL＋引文＋日期。
- `get_run(run_id★, after_event=0, include_events=true, max_events=200)` → state（starting／running／done／error／cancelled／dead）、turns、cost_usd、`result`（最終回報）、`events`（text／tool_call／tool_result…）。**輪詢**：先等 60–120 秒再拉，帶上次最大的 event id 當 `after_event` 只拿增量。
- `list_runs(limit, state)`、`cancel_run(run_id)`。
- **續接**：`run_agent(task="再補第 3 點", resume_run_id=<舊 run>)` 在同一個 harness session 裡接著做（三個 harness 都支援）。
- 預設權限 acceptEdits＋Bash／WebFetch／搜尋 MCP（DuckDuckGo＋Tavily）；`yolo=true` 才跳過所有確認。分身不載入主 session 的 hooks——派工前先確認目標檔沒被其他 session 認領。
- **計費**：Claude 模型預設走使用者的 Claude Code 訂閱登入（不按 token 計費）；`billing="api"` 才用 Anthropic API key 扣款——只有使用者明說要用 API 時才給。其他供應商一律用 API key。
- **在哪裡看**：看板 `http://127.0.0.1:7788/`；單筆頁 `http://127.0.0.1:7788/runs/<run_id>`（完整活動流，也能在網頁上追問、中止）。回報時把單筆頁的網址附給使用者。

```
mcp__omniapi-mcp__run_agent(task="查證以下三點…每點附 URL、引文、日期，寫到 notes/查證結果.md", model="cheap", cwd="C:/proj", title="查證 x", dispatcher="這段對話的標題")
mcp__omniapi-mcp__get_run(run_id="20260925130701-bf0", after_event=0)
```

## 8️⃣ 聊天 — `chat`

和外部模型**來回多輪**，對話存進 OmniAPI，使用者在 GUI 的 `/chat` 看得到、也能接著聊。

**三個文字工具怎麼選**

| 要做的事 | 用 |
|---|---|
| 問一次就好、要結構化輸出／function calling／自己組 messages，不必留紀錄 | `complete_text` |
| 要跟另一個模型討論好幾輪、對話要留給使用者看 | `chat` |
| 要模型**動手**：改檔、跑指令、在某個目錄裡做事 | `run_agent`（§7） |

- `chat(message★, conversation_id, model, system, title, reasoning_effort, temperature, max_completion_tokens)` → 等回覆完才回：`{conversation_id, title, state, text, model, requested_model, reasoning?, usage, cost_usd, error?, n_messages, url}`。
- **多輪一定要把回傳的 `conversation_id` 帶回下一次呼叫**；不帶就是開新對話（模型看不到前面的內容）。
- `model`：新對話不給就用設定頁的「預設聊天」（出廠是 `cheap`）；既有對話不給就沿用上次的模型，給了就從這一則起換模型（每則回覆各自記是誰答的）。
- `system`：新對話設定 system prompt；對既有對話給，會**取代**它原本的 system prompt。
- `url` 是 GUI 的聊天頁（`http://127.0.0.1:7788/chat/<id>`），回報時附給使用者。
- 回覆超過約 45 秒會回取件單（`status:"running"`、`task_id`，也帶 `conversation_id`）→ `get_job_result(task_id)` 領。
- 錯誤回 `{error, status}`：404＝`conversation_id` 不存在；400＝模型不認得；409＝上一則還在回覆中、對話已封存或正在刪除。
- **`chat` 不能帶附件**（沒有檔案參數）。要外部模型看圖、讀 PDF／文件／試算表：請使用者在 GUI 的對話裡附上，再用那段對話的 `conversation_id` 接著聊——附過的檔案模型照樣看得到，還會自己用讀檔工具（列出／讀一段／搜尋）翻內容。
- **從這裡送的回合不會出現生成提議**：GUI 裡模型可以提議生圖或配音、等使用者按確認；經 `chat` 沒有人能按，所以不給這個工具。要生圖就直接用 `generate_image`。
- 使用者在 GUI 重新生成或改過訊息時，對話會有分岔：`chat` 接在使用者**目前選的那一條**後面，模型只看到那一條；`n_messages` 則是所有分岔的合計。
- **作品牆**：所有生成工具做出來的檔案（圖、語音、音樂、歌詞、逐字稿、影片）都會登記進作品庫，使用者在 GUI 的 `/works` 看得到、搜得到、能下載；逐字稿會另存全文檔。`edit_image` 用 `image_path` 指到一件既有作品時，新圖會記得它的來源。
- **費用**記在「聊天・生成帳」（GUI `/costs` 的第二本帳，工具名 `chat`；生圖、語音、音樂、轉錄、影片的費用也在這本），跟派工帳分開。
- 終端機裡也能聊：`omni chat "一句話" -m standard`（逐字顯示），不帶訊息進互動模式；`-r <conversation_id>` 接續同一段對話。

```
r = mcp__omniapi-mcp__chat(message="幫我挑這份提案的三個弱點：…", model="strong", system="你是嚴格的審稿人")
mcp__omniapi-mcp__chat(message="第二點展開講，給改法", conversation_id=r.conversation_id)
mcp__omniapi-mcp__chat(message="換你從讀者角度看", conversation_id=r.conversation_id, model="gemini-3.8-flash")
```

## 9️⃣ 影片 — `generate_video`

短影片：文生影片，或給首幀（圖生影片）、有的模型還收尾幀。兩個供應商：大多數模型經 **OpenRouter**（需 `PROVIDERS__OPENROUTER__API_KEY`）；Google 的 **`gemini-omni-1.1-flash`** 用 Google key 直連（需 `PROVIDERS__GEMINI__API_KEY`，且那個專案要開 **Gemini API 付費層**）。有 Google key 時，OpenRouter 上的 `google/gemini-omni*`、`google/veo-*` 不列出。

> 💸 **影片一送出就計費，供應商沒有取消**。一支從幾美分到幾美元。使用者沒有明說要做影片時不要主動叫；送之前先跟使用者講模型、秒數、解析度與預估。

**參數**：`prompt`★、`model`（OpenRouter 影片 id，如 `alibaba/wan-3.0`、`kwaivgi/kling-v3.0-std`、`x-ai/grok-imagine-video`，或 `gemini-omni-1.1-flash`；省略＝設定頁的影片預設，出廠 `alibaba/wan-3.0`）、`duration`（秒，取模型 `video_params.durations` 之一；省略＝5 秒或最接近 5 的）、`resolution`（`video_params.resolutions` 之一，如 `480p`/`720p`/`1080p`；省略＝模型自己的預設，預估就變成區間）、`aspect_ratio`、`generate_audio`（模型 `video_params.audio` 不是 false 才有；會出聲的模型預設開，有的模型有聲較貴）、`seed`、`first_frame`、`last_frame`、`max_cost_usd`。

- **`first_frame`／`last_frame`** 三種給法：作品牆上一張圖的作品 id、本機圖檔路徑、data URL。模型的 `video_params.frames` 要有 `first_frame`／`last_frame` 才收。
- **每支上限**：預估超過上限（出廠 **$1**；使用者在設定頁「MCP 單支上限」可改或設「不限」）**不會送出**，回 `{status:"refused", reason:"over_limit", estimate_usd, limit_usd, message}`，`message` 裡寫了要帶多少 `max_cost_usd` 才放行。
- **算不出價的不送**：按 token 計價的模型（如 `bytedance/seedance-2.0-fast`）送出前算不出金額，回 `reason:"price_unknown"`；要送就帶 `max_cost_usd`（代表使用者接受的上限，實際費用事後記帳）。
- **被拒時不要自己加 `max_cost_usd` 重送**——把預估與上限轉告使用者，他同意了再帶上（或建議改短、降解析度）。
- **取件單**：等約 45 秒還沒好就回 `{status:"running", task_id:"video_<id>"}`；用 `get_job_result(task_id)` 領，還在跑就隔一兩分鐘再領。服務重啟過也領得到（送出時就記下了供應商那邊的工作編號）。
- **Gemini Omni 的規矩**：一定帶聲音（不要帶 `generate_audio=false`，會被拒；不要聲音就在 prompt 寫 no dialogue / no music）；3～10 秒、`360p`／`720p`（預設）／`1080p`、`16:9`／`9:16`；`last_frame` 要搭 `first_frame`。720p 約 $0.10／秒算得出預估；其他解析度送出前算不出（`price_unknown`，要使用者同意後帶 `max_cost_usd`）。費用照 Google 回報的記。這一版是連著等到做好（同步），**等待中服務重啟，那一支就領不回來**（已送出、多半已收費）——不要在等 Omni 時叫使用者重啟服務。
- **完成時回**：`status:"completed"`、`file_path`、`video_url`（file://）、`duration_s`、`width`、`height`、`has_audio`（從檔案讀的）、`cost_usd`（供應商實際收的，可能比名單價低）、`artifact_id`、`waited_s`。影片收進作品牆 `/works`。
- **等太久**：超過設定頁的「最長等待」（出廠 20 分鐘）回 `status:"waited_too_long"`；供應商可能還在做，請使用者到 GUI 出件口按「再去問一次」（不重送、不多收）。

```
mcp__omniapi-mcp__list_available_models(modality="video")        # 先看 video_params 與每秒價格
mcp__omniapi-mcp__generate_video(prompt="a paper boat drifting down a rainy street, slow dolly, soft ambient rain", model="alibaba/wan-3.0", duration=5, resolution="480p", aspect_ratio="16:9")
mcp__omniapi-mcp__generate_video(prompt="she turns toward the camera and smiles", first_frame="C:/path/portrait.png", model="kwaivgi/kling-v3.0-std")
mcp__omniapi-mcp__get_job_result(task_id="video_…")
```

> 不支援：影片延長、編輯、對嘴、放大、多張參考圖。
> 📖 全參數、計價方式、回傳欄位 → **[`references/video.md`](references/video.md)**。

## 🎯 一眼選對

```
高解析海報 / 4K          → generate_image  gpt-image-2 size=3840x2160 quality=high
生圖／改圖榜最強          → generate_image  gpt-image-2.5-sunburst
透明背景 logo            → generate_image  gpt-image-1.5 background=transparent
時間軸字幕 (srt/vtt)     → transcribe_audio whisper-1 response_format=srt（⚠️2027-02-26 關閉）
純文字逐字稿             → transcribe_audio gpt-transcribe
日常問答 / 文案 (省錢)   → complete_text   model="cheap"
一般任務要快要穩         → complete_text   model="standard"
最難推理 / 規劃          → complete_text   model="strong"
跟別的模型討論好幾輪     → chat            帶回 conversation_id（對話留在 GUI /chat）
旁白 / 配音              → generate_speech gpt-4o-mini-tts  (有 ElevenLabs key 則 eleven_flash_v2_5)
一句話出整首歌           → generate_music  V6
FLUX／Seedream 等風格     → generate_image  model=<OpenRouter id>（先看 image_params）
幾秒的短片（便宜試）      → generate_video  alibaba/wan-3.0 resolution=480p duration=5
讓一張圖動起來            → generate_video  first_frame=<作品 id 或路徑>
```

## 🔧 診斷
1. `health_check` — 整體 + 各 provider 健康（會 ping provider 的免費 endpoint）。
2. `server_info` — 看 capabilities 與預設值。
3. `list_available_models(modality=...)` — **跨模態**即時名單（`text`/`image`/`transcription`/`speech`/`music`/`video`，省略＝全部；圖片的 OpenRouter 模型帶 `image_params`，影片帶 `video_params`），含 provider、線上狀態、定價、棄用／關閉日與等級別名；上了 Artificial Analysis 排行榜的模型另帶 `rank`（在該模態各榜最好的名次，2026-10-06 的快照，數字越小越熱門；沒上榜就沒有這欄）。旗標：`include_retired`、`include_snapshots`、`refresh`。
   回傳裡值得看的：`tiers`（**現在**的等級對應，含使用者在設定頁的覆寫）、`configured_providers`（有 key 的家）、`default_text_model`、`providers.<家>.get_key`（拿 key 的網址與說明）、`providers.<家>.suggested_tiers`（這家的便宜／一般／最強建議值；OpenRouter 沒有）。只有一家 key、等級別名卻指到別家時，可以建議使用者到設定頁照 `suggested_tiers` 改。

## 🚫 界線
- **影片剪輯、延長、對嘴、變聲、多段合成** → 用 VPick（另一台 canvas MCP），不是 omniapi。omniapi 只做單支短片的生成（文生影片、首幀／尾幀圖生影片）。
- **程式化逐格動畫 / 資料視覺化影片** → 用 Remotion。
- omniapi 的範圍：圖 / 轉錄 / 文字 / 語音 / 音樂 / 短影片的 API 呼叫、跟外部模型多輪聊天、把任務派給 headless agent。
- 圖片、語音、音樂、轉錄、影片在網頁上也有入口（生成頁 `/make`；影片在那裡每支都有確認單）；從 MCP 做的作品一樣收進作品牆 `/works`，檔案路徑在工具回傳裡。
