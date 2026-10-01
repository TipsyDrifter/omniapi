# 語音轉文字 (Transcription) — 完整參數參考

> OpenAI Audio Transcription。對應 omniapi 工具：`mcp__omniapi-mcp__transcribe_audio`。
> **參數查證：2026-06-28**（developers.openai.com；`platform.openai.com` 擋爬蟲，改用官方鏡像，內容一致）。
> **模型名單查證：2026-09-25**（OpenAI models / deprecations 頁）。

---

## 0. omniapi 工具對接現況（先讀）

`transcribe_audio` 暴露的參數：`audio_path`★(本機檔路徑，首選) 或 `audio_data`(base64)、`model`、`language`、`prompt`、`response_format`、`temperature`、`timestamp_granularities`、`chunking_strategy`、`include`、`known_speaker_names`、`known_speaker_references`。

**✅ 已暴露**：
- `timestamp_granularities`：傳 `["word"]` 取**字級時間戳**；需 `verbose_json`+`whisper-1`。結果新增 `words[]`。**實機驗證**過。
- `temperature`（0–1）。
- **`chunking_strategy`**：`"auto"` 或 VAD dict，長音檔伺服器端自動切塊（免手動切 25MB）。diarize >30s 會自動補 `"auto"`。
- **`include`**：`["logprobs"]` 取逐字信心值（新增 `logprobs` 欄位）——僅 `gpt-4o-transcribe`/`-mini` + `response_format="json"`，omniapi 自動 gate，其餘丟棄。**實機驗證**回 13 個 token logprob。
- ⭐ **講者分離**：`model="gpt-4o-transcribe-diarize"` + `response_format="diarized_json"` → segments 帶 `speaker` 標籤；可選 `known_speaker_names`/`known_speaker_references`(2-10s 音檔 data URL 指名，最多 4 人)。omniapi 自動：diarize 模型丟掉 `prompt`/`include`、speaker 參數只對 diarize 有效。**實機驗證**回帶 speaker 的 segments。
- 回傳的 metadata 含 `model_status` / `model_shutdown`，叫到棄用模型時會告訴你關閉日。

**仍刻意跳過（附原因）**：`stream`(串流不適合單次回傳)；`extra_headers/body/query`(SDK 管線逃生口，非模型能力)。

---

## 1. ⚠️ 模型名單與生命週期（2026-09-25）

| model | 狀態 | 關閉日 | 定位 |
|---|---|---|---|
| **`gpt-transcribe`** ⭐預設 | current | — | 現役唯一 current 的轉錄模型；純文字 / json |
| `whisper-1` | **deprecated** | **2027-02-26** | 唯一文件化支援 `verbose_json`/`srt`/`vtt` 與 word 級時間戳的模型 |
| `gpt-4o-transcribe` | **deprecated** | **2027-02-26** | 可配 `include=["logprobs"]` |
| `gpt-4o-mini-transcribe` | **deprecated** | **2027-02-26** | 便宜版，同樣支援 logprobs |
| `gpt-4o-transcribe-diarize` | **deprecated** | **2027-02-26** | **講者分離**，配 `response_format="diarized_json"` |

> OpenAI 的 2026-08-26 棄用公告一次涵蓋後四款，**2027-02-26 關閉**。它們在 omniapi 裡照樣可叫（只記 warning），因為——

> 🚨 **尷尬的現況**：**字幕（srt/vtt）、segment/word 時間戳、講者分離，目前只有棄用模型做得到**，OpenAI 尚未公布替代路徑。所以：
> - 只要純文字逐字稿 → **`gpt-transcribe`**（現役、最便宜）。
> - 要時間軸字幕 → 還是得叫 `whisper-1`，並把 2027-02-26 記在行事曆上。
> - 要講者標籤 → `gpt-4o-transcribe-diarize`，同上。
>
> 📡 名單是即時的：跑 `list_available_models(modality="transcription")` 看現況，加 `include_retired=true` 看已真正下架的。

## 2. ⭐ 模型 × `response_format` 矩陣

| `response_format` | `gpt-transcribe` | `whisper-1` | `gpt-4o-transcribe` | `gpt-4o-mini-transcribe` | `gpt-4o-transcribe-diarize` |
|---|:---:|:---:|:---:|:---:|:---:|
| `json`（預設） | ✅ | ✅ | ✅ | ✅ | ✅ |
| `text` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `srt`（字幕） | ❌ | ✅ | ❌ | ❌ | ❌ |
| `vtt`（字幕） | ❌ | ✅ | ❌ | ❌ | ❌ |
| `verbose_json`（含 segment 時間戳） | ❌ | ✅ | ❌ | ❌ | ❌ |
| `diarized_json`（講者標籤） | ❌ | ❌ | ❌ | ❌ | ✅ |

**一句話**：**只有 `whisper-1` 支援 `srt`/`vtt`/`verbose_json`**（也因此只有它能配時間戳）。`gpt-transcribe` 與 GPT-4o 系列只回 `json`/`text`。
> omniapi 對超出範圍的組合是**記 warning 後照送**，不會硬擋——所以拿不到預期格式時先看 log。
> `gpt-4o-transcribe-diarize` **不吃** `prompt`/`include`（omniapi 自動丟），>30s 自動補 `chunking_strategy="auto"`。

## 3. 完整參數表（omniapi 可傳的標 ✅）

| 參數 | 型別 | omniapi | 可選值 | 預設 | 說明 |
|---|---|:---:|---|---|---|
| `audio_path`/`audio_data` | — | ✅ | 本機路徑 / base64 | — | 音檔來源（omniapi 介面，底層轉成 file） |
| `model` | string | ✅ | 見 §1 | **`gpt-transcribe`** | 模型 |
| `language` | string | ✅ | ISO-639-1（`en`/`zh`/`ja`…） | 自動偵測 | 指定可**提升準確度+降延遲** |
| `prompt` | string | ✅ | 任意文字 | — | 提示專有名詞/風格；whisper-1 只讀**前 224 tokens**；diarize 不支援(自動丟) |
| `response_format` | enum | ✅ | 見 §2 | omniapi 預設 `text` | 輸出格式 |
| `temperature` | number | ✅ | 0–1 | 0 | 取樣溫度 |
| `timestamp_granularities[]` | array | ✅ | `word`/`segment` | `segment` | 需 `verbose_json`+`whisper-1`；word 增延遲；結果多 `words[]` |
| `include[]` | array | ✅ | `logprobs` | — | 僅 gpt-4o-transcribe/-mini + `json`；omniapi 自動 gate；結果多 `logprobs` |
| `chunking_strategy` | str/obj | ✅ | `"auto"` / `{type:"server_vad",...}` | — | 長音檔自動切塊；diarize >30s 自動補 auto |
| `known_speaker_names`/`references` | array | ✅ | 名字 / 2-10s 音檔 data URL | — | 僅 diarize；指名講者（≤4），omniapi 自動 gate |
| `stream` | boolean | ❌ | true/false | false | 串流不適合單次回傳（跳過） |

## 4. 限制

| 項目 | 規格 |
|---|---|
| 檔案大小上限 | **25 MB**（所有模型） |
| 支援格式 | `flac`/`mp3`/`mp4`/`mpeg`/`mpga`/`m4a`/`ogg`/`wav`/`webm` |
| 長音檔 | >25MB 要自行切塊（勿在句中切，建議用 PyDub），或改用 `chunking_strategy="auto"`；語言數 99+ |

## 5. 成本（每分鐘 USD，2026-09-25）

| 模型 | 每分鐘 | 每小時 |
|---|---|---|
| **`gpt-transcribe`** | **$0.0045** | $0.27 |
| `whisper-1` | $0.006 | $0.36 |
| `gpt-4o-transcribe` | $0.006 | $0.36 |
| `gpt-4o-mini-transcribe` | $0.003 | $0.18 |
| `gpt-4o-transcribe-diarize` | $0.006 | $0.36 |

## 6. 選型 + 雷

```
純文字逐字稿（現役首選）      → gpt-transcribe
要 srt/vtt 字幕 或 時間軸     → whisper-1 + response_format=srt/vtt/verbose_json（⚠️2027-02-26 關閉）
要講者標籤                    → gpt-4o-transcribe-diarize + diarized_json（⚠️同上）
最省錢純文字                  → gpt-4o-mini-transcribe $0.003/min（⚠️同上）
非英文音檔                    → 任一模型 + language="zh"/"ja"（指定提升準確度）
```
- **雷 1**：要字幕/時間戳**只能 whisper-1**；用 `gpt-transcribe` 或 gpt-4o 系列拿不到 srt。
- **雷 2**：四款帶時間戳/講者能力的模型**全在棄用清單上**（2027-02-26）。做長期產品的話，時間軸這條路要另外規劃。
- **雷 3**：檔案 >25MB 一定要先切塊（或用 `chunking_strategy`）。
- **雷 4**：傳了不支援的 `response_format` 不會被擋，只記 warning 後照送——結果不如預期先看 log。

## 來源
- 參數（2026-06-28）：developers.openai.com/api/reference/resources/audio/subresources/transcriptions、…/docs/guides/speech-to-text。
- 模型名單與棄用日（2026-09-25）：各家官方文件與公開榜單、OpenAI deprecations 頁。
