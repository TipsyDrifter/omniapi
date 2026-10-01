# 語音合成 (TTS) — 完整參數參考

> 涵蓋 **OpenAI Speech**、**ElevenLabs** 與 **Gemini TTS**。對應 omniapi 工具：`mcp__omniapi-mcp__generate_speech`。
> **參數查證：2026-06-28**（OpenAI developers.openai.com + ElevenLabs elevenlabs.io/docs）。
> **模型名單查證：2026-09-25**（各家官方文件與公開榜單）。

---

## 0. omniapi 工具對接現況（先讀）

`generate_speech` 暴露的參數：`text`★、`voice`、`model`、`output_format`、`instructions`、`speed`、`voice_settings`、`language_code`、`seed`、`previous_text`、`next_text`、`apply_text_normalization`、`enable_logging`。存檔到 `storage/audio/<date>/`，回 `audio_path` 與含 `model_status`／`model_shutdown` 的 metadata。

**✅ 已暴露**：
- OpenAI：**`instructions`**（gpt-4o-mini-tts 的語氣/情緒控制，核心功能！）— omniapi **自動 gate**：只送 gpt-4o-mini-tts，傳給 tts-1/hd 記 warning 並忽略。`speed`（tts-1/hd 語速）也可傳。
- ElevenLabs（全套）：`voice_settings`（dict：`stability`/`similarity_boost`/`style`/`use_speaker_boost`/`speed`）、`language_code`(強制語言)、`seed`(可重現)、`previous_text`/`next_text`(長文銜接韻律)、`apply_text_normalization`(`auto`/`on`/`off`)、`enable_logging`(false=零留存隱私模式，走 query 參數)。ElevenLabs-only 參數自動只進 ElevenLabs body/query。
- Gemini TTS：`voice`（prebuilt 名稱，如 `Kore`）、`language_code`。

**仍刻意跳過（附原因）**：`optimize_streaming_latency`(ElevenLabs 已棄用＋只對串流有意義)、`stream_format`(OpenAI 串流專用，單次存檔無意義)。

**模型路由**：`eleven_*` → ElevenLabs；`gpt-4o-mini-tts`/`tts-1*` → OpenAI；`gemini-*-tts` → Gemini。沒填 model 時用該家自己的預設。

---

## 1. 模型名單與生命週期（2026-09-25）

| model | provider | 狀態 | 定位 |
|---|---|---|---|
| `eleven_flash_v2_5` ⭐ElevenLabs 預設 | ElevenLabs | current | 超低延遲 ~75ms |
| `eleven_flash_v2` | ElevenLabs | current | 超快，英文為主 |
| `eleven_v3` | ElevenLabs | current | 最進階 / 最具表現力 |
| `eleven_v3_conversational` | ElevenLabs | current | 表現力強的即時對話（~280ms） |
| `eleven_multilingual_v2` | ElevenLabs | current | 最擬真、情緒豐富，長文旁白首選 |
| `eleven_turbo_v2_5` | ElevenLabs | ⚠️ **deprecated** → `eleven_flash_v2_5` | 官方標「被 Flash 系列超越」；**未公布關閉日**，仍可叫 |
| `gpt-4o-mini-tts` ⭐OpenAI 預設 | OpenAI | current | 吃 `instructions` 語氣控制 |
| `tts-1` / `tts-1-hd` | OpenAI | current | 低延遲 / 高品質；吃 `speed` |
| `gemini-3.8-flash-tts` ⭐Gemini 預設 | Google | current | 最具表現力的 Gemini TTS |
| `gemini-3.8-flash-lite-tts` | Google | current | 便宜版 |
| `gemini-3.1-flash-tts-preview` | Google | ⚠️ **deprecated** → `gemini-3.8-flash-tts` | Google 標 legacy preview；**未公布關閉日** |

> 📡 名單是即時的：跑 `list_available_models(modality="speech")` 看現況，加 `include_retired=true` 看已真正下架的。
> 💡 兩個 deprecated 都**沒有**公布關閉日，所以 omniapi 只記 warning、不填 `model_shutdown`——「沒有關閉日」不等於「不會關」。

---

## 2. OpenAI Speech API

端點 `POST /v1/audio/speech`。

### 2.1 參數（omniapi 可傳標 ✅）

| 參數 | omniapi | 可選值 | 預設 | 說明 |
|---|:---:|---|---|---|
| `model` | ✅ | `gpt-4o-mini-tts`/`tts-1`/`tts-1-hd` | omniapi 預設 gpt-4o-mini-tts | 模型 |
| `input`(text) | ✅ | 文字 | — | 上限 **4096 字元**；gpt-4o-mini-tts 另限 **2000 tokens** |
| `voice` | ✅ | 見 2.2 | omniapi 預設 `alloy` | 聲音 |
| `response_format`(output_format) | ✅ | `mp3`/`opus`/`aac`/`flac`/`wav`/`pcm` | mp3 | 格式（omniapi 接受 `mp3_44100_128` 式字串，取前綴） |
| `instructions` | ✅ | 自然語言（口音/情緒/語調/語速/耳語…） | — | ⭐**僅 gpt-4o-mini-tts**；omniapi 對 tts-1/hd 自動忽略 |
| `speed` | ✅ | 0.25–4.0 | 1.0 | ⭐**對 gpt-4o-mini-tts 無效**（語速改寫進 instructions） |

### 2.2 voice 清單與模型支援

13 種：`alloy`/`ash`/`ballad`/`coral`/`echo`/`fable`/`onyx`/`nova`/`sage`/`shimmer`/`verse`/`marin`/`cedar`

| 模型 | 支援 voice |
|---|---|
| `gpt-4o-mini-tts` | **全 13 種**（音質最佳 = `marin`/`cedar`） |
| `tts-1` / `tts-1-hd` | 僅 9 種（**無** ballad/verse/marin/cedar） |

### 2.3 instructions（語氣控制，僅 gpt-4o-mini-tts）
自然語言操控 accent/emotion/intonation/speed/tone/whispering。例：`"Speak in a warm, reassuring tone with occasional pauses."`
> 實測：`generate_speech(text=..., model="gpt-4o-mini-tts", instructions="Speak slowly in a warm, cheerful tone.")` 已確認可生成。

### 2.4 成本
| 模型 | 價 |
|---|---|
| `tts-1` | $15 / 1M chars |
| `tts-1-hd` | $30 / 1M chars |
| `gpt-4o-mini-tts` | 輸入 $0.60/1M tok + 音訊輸出 $12/1M tok（≈$15/1M chars，≈$0.015/分鐘） |

---

## 3. ElevenLabs TTS API

端點 `POST /v1/text-to-speech/{voice_id}`，header `xi-api-key`。

### 3.1 關鍵參數
- **`output_format`**（⚠️**query 參數，不是 body**）：28 種，`mp3_44100_128`(預設)、`mp3_44100_192`、`opus_48000_128`、`pcm_16000`、`wav_44100`… 命名 `{格式}_{取樣率}_{位元率}`。
- body：`text`★、`model_id`、`language_code`、`seed`(0–4294967295)、`previous_text`/`next_text`、`apply_text_normalization`(auto/on/off，`on` 在 turbo/flash v2.5 不支援)。
- `voice_settings`：`stability`(0–1，預設0.5；v3 用三檔0/0.5/1)、`similarity_boost`(0–1，0.75)、`style`(0–1，0)、`use_speaker_boost`(bool,true)、`speed`(0.7–1.2,1.0)。
- `voice_id` 用 `GET /v1/voices` 取；omniapi 預設 Rachel `21m00Tcm4TlvDq8ikWAM`。

### 3.2 模型規格
| model_id | 字數上限/次 | 語言 | 延遲 | 場景 |
|---|---|---|---|---|
| `eleven_v3` | **3,000**(⚠️官方 3000/5000 不一，保守取 3000) | 70+ | 高/不穩 | 最具表現力；**不適合即時** |
| `eleven_v3_conversational` | 未查證 | 未查證 | ~280ms | 表現力強的即時對話 |
| `eleven_multilingual_v2` | 10,000 | 29 | 中 | 長文最穩、旁白首選 |
| `eleven_flash_v2_5`（預設） | 40,000 | 32 | ~75ms | 即時/對話、性價比 |
| `eleven_flash_v2` | 未查證 | 英文為主 | ~75ms | 英文快速 |
| `eleven_turbo_v2_5` | 40,000 | 32 | 低 | ⚠️ deprecated，官方建議改 flash |

> ⚠️ `eleven_v3_conversational` / `eleven_flash_v2` 的字數上限與語言數**尚未查證**（2026-09-25 的名單查證只確認了型號與狀態）。要跑長文前先實測或查官方 models 頁，別照抄同系列數字。

### 3.3 成本（每 1K 字元）
| 模型 | 價 |
|---|---|
| `eleven_v3` / `eleven_multilingual_v2` | **$0.10** |
| `eleven_v3_conversational` / `eleven_flash_v2_5` / `eleven_flash_v2` / `eleven_turbo_v2_5` | **$0.05** |

---

## 4. Gemini TTS

走 google-genai SDK 的 `models.generate_content`，`response_modalities=['AUDIO']` + `SpeechConfig` 指定 prebuilt voice。金鑰是 **Gemini Developer API（AI Studio）key**——`PROVIDERS__GEMINI__API_KEY` 直接放字串，不是檔案路徑。

- **voice**：prebuilt 名稱，omniapi 預設 `Kore`；完整清單在 Google 的 speech-generation 文件頁。
- **`language_code`**：可強制輸出語言（ISO-639-1）。
- ⚠️ **只吐 PCM**：官方文件說是 24 kHz / 16-bit / mono。omniapi 會包成 **WAV** 回傳（你要 `pcm_*` 格式才給裸 PCM）——所以 `output_format` 填 mp3 也一樣拿到 WAV。
  - 回應的 mime type（如 `audio/L16;codec=pcm;rate=24000`）若有標 rate，omniapi 以它為準，常數只是 fallback。**這組取樣率常數未對真實回應實測過**。
- 取音訊：`candidates[0].content.parts[*].inline_data.data` 已經是 **bytes，別再 base64 decode**。

### 4.1 成本
| 模型 | 輸入 | 音訊輸出 | 備註 |
|---|---|---|---|
| `gemini-3.8-flash-tts` | $0.50 / 1M tok | $9.00 / 1M tok | 現價到 **2026-12-31**；2027-01-01 起 $1.00 / $18.00。每秒約 25 audio tokens |
| `gemini-3.8-flash-lite-tts` | $0.50 / 1M tok | $6.00 / 1M tok | 現價到 2026-12-31；之後輸出 $12.00 |

---

## 5. 選型 + 雷

```
日常旁白/配音                → gpt-4o-mini-tts + voice=nova/marin（要語氣就寫 instructions）
高品質固定音色               → tts-1-hd（吃 speed、不吃 instructions、voice 限 9 種）
即時/低延遲                  → eleven_flash_v2_5
最具情緒表現                 → eleven_v3（不適合即時，字數保守 3000）
即時對話又要有情緒           → eleven_v3_conversational
長文有聲書                   → eleven_multilingual_v2
便宜的 Gemini 配音           → gemini-3.8-flash-lite-tts（注意回 WAV）
```
- **雷 1**：OpenAI 的 `instructions`（語氣）**只對 gpt-4o-mini-tts 有效**，且該模型**忽略 speed**；反之 tts-1/hd 吃 speed、不吃 instructions。
- **雷 2**：OpenAI 字數兩層限制（API 4096 字元 vs gpt-4o-mini-tts 2000 tokens）——切文字以 token 保守。
- **雷 3**：ElevenLabs `output_format` 是 **query 參數**；`eleven_v3` 官方字數上限文件自相矛盾（3000/5000），保守取 3000。
- **雷 4**：**Gemini TTS 不管你要什麼格式都回 WAV**，要接後製管線先預期這件事。
- **雷 5**：`eleven_turbo_v2_5` 與 `gemini-3.1-flash-tts-preview` 已 deprecated 但**沒有關閉日**——會記 warning，不會擋，新工作別選它們。

## 來源
- OpenAI（2026-06-28）：developers.openai.com/api/reference/resources/audio/subresources/speech、…/docs/guides/text-to-speech、…/docs/models/gpt-4o-mini-tts。
- ElevenLabs（2026-06-28 參數 / 2026-09-25 名單）：elevenlabs.io/docs/api-reference/text-to-speech/convert、…/docs/overview/models、…/pricing/api。
- Gemini（2026-09-25）：ai.google.dev speech-generation 與 pricing 頁。
