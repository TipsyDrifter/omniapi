# 音樂生成 — 完整參考

> 入口在 [`../SKILL.md`](../SKILL.md) §6。本檔是音樂能力的完整參數、依賴鏈、輪詢機制、定價與雷區。
> **模型名單查證日：2026-09-25**（kie.ai 文件 + ElevenLabs compose reference）。

## §0 對接現況（2026-09-25，v1.0.0-alpha.1）

- **工具面收斂**：原本 13 個音樂工具已合併為 **5 個**——`generate_music`、`edit_music(action)`、`music_lyrics(action)`、`music_utility(action)`、`compose_music(action)`。功能一個沒少，只是改由 `action` 分流。
- **Suno via kie.ai** ✅ 端到端實測通過（生成 / 寫詞 / WAV / 字級時間軸實測綠燈；其餘 action 走同一組已驗證的引擎分支）。
- **ElevenLabs Music** 🟡 code 正確、送達 API 正確，但**免費版回 402**，要用需付費方案。`compose_music(action="compose")` 的 multipart 解析待付費帳號實測。

## 兩個後端

| 後端 | 同步性 | 金鑰 | 特點 |
|---|---|---|---|
| **Suno**（via kie.ai 聚合平台） | 內部輪詢到完成（對呼叫端是一次呼叫） | `PROVIDERS__KIE__API_KEY` | 一句話→完整歌、**V6 家族**、免洗商用、pay-as-you-go |
| **ElevenLabs Music** | 同步（直接回 audio bytes） | `PROVIDERS__ELEVENLABS__API_KEY`（**付費方案**） | composition plan 精控編曲、最長 10 分鐘、music_v1/v2/v2_5、$0.15/分鐘 |

路由：`generate_music` 依 `model` 值選後端（`V*`→Suno；`music_v*`→ElevenLabs）；不傳 model 用第一個配置好的後端。`edit_music`／`music_lyrics`／`music_utility` 一律走 kie.ai，`compose_music` 一律走 ElevenLabs。

## 模型名單（2026-09-25）

| model | 後端 | 狀態 |
|---|---|---|
| `V6` ⭐Suno 預設 | kie.ai | current |
| `V6_MINI` | kie.ai | current（快 / 輕量） |
| `V6_WILD` | kie.ai | current（實驗性） |
| `V4`、`V4_5`、`V4_5PLUS`、`V4_5ALL`、`V5`、`V5_5` | kie.ai | ⚠️ **retired**（kie.ai 標 Discontinued）。仍可送、只記 warning，隨時可能失效——舊 task id 與存檔 preset 才留著 |
| `music_v1` ⭐ElevenLabs 預設 | ElevenLabs | current |
| `music_v2` | ElevenLabs | current |
| `music_v2_5` | ElevenLabs | current（最高品質） |

> 📡 名單是即時的。跑 `list_available_models(modality="music")` 看現況；加 `include_retired=true` 才會列出已下架的 Suno 舊版。

## 1️⃣ `generate_music`（核心，兩後端）

| 參數 | 適用 | 說明 |
|---|---|---|
| `prompt`★ | 兩者 | 音樂描述；Suno custom mode 下可當歌詞 |
| `model` | 兩者 | 見上表；省略＝第一個配置好的後端的預設 |
| `instrumental` | 兩者 | 純器樂無人聲 |
| `music_length_ms` | ElevenLabs | 3000–600000（Suno 忽略） |
| `output_format` | ElevenLabs | mp3_44100_128 / mp3_44100_192 / pcm_44100 等；Suno 一律 mp3 |
| `custom_mode` | Suno | 開精控（需同時給 `style`+`title`） |
| `style` / `title` | Suno custom | 曲風 / 歌名（title ≤80 字元） |
| `vocal_gender` | Suno custom | `m` / `f` |
| `negative_tags` | Suno | 排除的曲風，如 `'heavy metal, distortion'` |

回傳：`audio_path`、`audio_url`、`provider`、`model`、`title`、`duration`、`bytes`，**Suno 另回 `audio_id` 供串接**。存 `storage/music/<date>/`。

**Suno 一次給兩首**（generate／extend／cover／upload_extend／add_instrumental／add_vocals 都是）：兩首都存、都進作品庫。頂層欄位＝第一首（跟以前一樣）；`tracks` 列出每一首 `{audio_path, audio_id, title, duration, bytes}`（第一首也在裡面），`track_count` 是首數；第二首檔名是第一首加 `_2`。要延長／轉 WAV／做 MV 哪一首，就帶那一首的 `audio_id`（`task_id` 兩首共用）。某一首下載失敗時，那一筆只有 `audio_id`＋`error`，不影響另一首。費用是整次生成的總額，作品庫裡兩首平分。`settings.json` 設 `"music": {"suno_all_tracks": false}` 可改回只收第一首。

## 2️⃣ `edit_music(action)` — Suno 音訊變形

一個工具、六種 action，**每種的必填參數不同**：

| `action` | 做什麼 | 必填 | `custom_mode=true` 時另需 |
|---|---|---|---|
| `extend` | 延長既有 Suno 曲 | `audio_id` | `prompt`、`style`、`title`、`continue_at` |
| `cover` | 翻唱／改編上傳音檔 | `upload_url`、`prompt` | `style`、`title` |
| `upload_extend` | 延長上傳音檔（非 Suno 產出） | `upload_url` | `prompt`、`style`、`title`、`continue_at` |
| `add_instrumental` | 幫上傳檔加伴奏 | `upload_url`、`title`、`tags`、`negative_tags` | （不支援 custom_mode） |
| `add_vocals` | 幫上傳的伴奏加人聲 | `upload_url`、`prompt`、`title`、`style`、`negative_tags` | （不支援 custom_mode） |
| `separate_vocals` | 分離人聲／樂器成多軌 | `task_id`、`audio_id`、`separation_type` | （不支援 custom_mode） |

其他參數：`model`（Suno 模型）、`instrumental`（僅 `cover`／`upload_extend`）、`separation_type`（`separate_vocal`＝人聲+伴奏兩軌；`split_stem`＝鼓/貝斯/吉他…逐樂器分軌）。

- `custom_mode=false`（預設）時，`extend`／`upload_extend`／`cover` **沿用來源曲的設定**，不必自己填 style/title。
- `upload_url` 必須是**公開可取**的音檔 URL，長度 **≤8 分鐘**。
- `add_instrumental` / `add_vocals` **只吃 V6 家族**（`V6`/`V6_MINI`/`V6_WILD`）；已 retired 的 `V4_5PLUS`/`V5`/`V5_5` 留作相容尾巴，其餘舊 id 會被擋下。

## 3️⃣ `music_lyrics(action)` — 歌詞

| `action` | 做什麼 | 必填 | 備註 |
|---|---|---|---|
| `generate` | 依主題/情緒/曲風寫歌詞 | `prompt`（**≤200 字元**） | 回文字，同時存成 .txt |
| `timestamped` | 取字級時間軸（`alignedWords`）+ 波形 | `task_id`、`audio_id` | **同步**；不會寫出歌詞檔 |

## 4️⃣ `music_utility(action)` — 轉出

| `action` | 做什麼 | 必填 | 可選 |
|---|---|---|---|
| `convert_to_wav` | 轉高音質 WAV | `task_id`、`audio_id` | — |
| `create_music_video` | 生成 MP4 MV | `task_id`、`audio_id` | `author`（封面署名 ≤50 字元）、`domain_name`（底部浮水印 ≤50 字元） |

## 5️⃣ `compose_music(action)` — ElevenLabs 編曲藍圖工作流

| `action` | 做什麼 | 必填 | 可選 |
|---|---|---|---|
| `create_plan` | 產出可編輯的 composition plan JSON（分段 styles + 歌詞 + 時長），**不出音訊** | `prompt` | `music_length_ms`、`source_composition_plan`（丟既有 plan 進來改寫）、`model` |
| `compose` | 生成音訊，連同用的 plan 與 song metadata 一起回 | `prompt` **或** `composition_plan`（**互斥、二擇一**） | `instrumental`、`output_format`、`music_length_ms`、`with_timestamps`、`model` |

- composition_plan 結構：music_v1 = `positive/negative_global_styles[]` + `sections[]`（`section_name`／`positive/negative_local_styles`／`duration_ms` 3000–120000／`lines[]` ≤30 行）；music_v2 = `chunks[]`。
- 只要一首普通的曲子 → 用 `generate_music`，不必繞 composition plan。

## 6️⃣ 依賴鏈（最常卡的地方）

```
generate_music ──► audio_id + task_id
                      │
                      ├─► edit_music(action="extend",           audio_id=…)
                      ├─► edit_music(action="separate_vocals",  task_id=…, audio_id=…)
                      ├─► music_lyrics(action="timestamped",    task_id=…, audio_id=…)
                      ├─► music_utility(action="convert_to_wav",      task_id=…, audio_id=…)
                      └─► music_utility(action="create_music_video",  task_id=…, audio_id=…)

你自己的公開音檔 URL ──► edit_music(action="cover" / "upload_extend" / "add_instrumental" / "add_vocals")
```

## 7️⃣ 定價

- **Suno via kie.ai**：credits 制，每 credit 約 **$0.005 USD**。實測（V6_MINI）每次工作扣：生成／延長／翻唱／加伴奏／加人聲 12（一次兩首，約 $0.06）、寫詞 0.4、轉 WAV 0.4、音樂影片 2；kie.ai 自己沒公布單價。比官方省 30–70%，watermark-free 商用授權。
- **ElevenLabs Music**：**$0.15/分鐘**（API），需 Starter 以上付費方案。

## 8️⃣ 雷區（實測血淚）

- ⚠️ **kie.ai `callBackUrl` 實測必填**：文件說可省，實際不帶會回 `422 "Please enter callBackUrl"`。工具已自動塞佔位 URL 並靠輪詢拿結果，呼叫端不用管。
- ⚠️ **Suno 是非同步的**：工具內部輪詢 record-info 到 `SUCCESS`（audio 家族看 `data.status`；wav/mp4/vocal-removal 看 `data.successFlag`），一次呼叫可能等 30–120 秒才回。超過 ~45 秒會改回**取件單** `{status:"running", task_id}`，用 `get_job_result(task_id)` 領。
- ⚠️ **太短/模糊 prompt → `code 531 credits refunded`**（Suno 內部生歌詞步驟失敗）。用具體完整的 prompt。
- ⚠️ **`add_instrumental` / `add_vocals` 只吃 V6 家族**；送其他舊 id 會被工具擋下。
- ⚠️ **ElevenLabs Music 需付費**：免費版所有音樂端點回 **402**；升級後同樣的呼叫即回音檔。（2026-07 實測，2026-09-25 覆核仍適用。）
- ⚠️ **record-info 欄位是 camelCase**（`audioUrl`），callback payload 才是 snake_case（`audio_url`）——工具兩者都容錯。
- ⚠️ **retired 的 Suno 舊 id 仍會被接受**（只記 warning），所以「沒報錯」不代表「還能用」。新工作一律指定 V6 家族。
- 生成產物保留 14 天（kie.ai）。

## 9️⃣ 舊工具名對照（2026-09-25 前的呼叫端）

| 舊工具 | 現在怎麼叫 |
|---|---|
| 延長既有曲 | `edit_music(action="extend")` |
| 翻唱上傳檔 | `edit_music(action="cover")` |
| 延長上傳檔 | `edit_music(action="upload_extend")` |
| 加伴奏 | `edit_music(action="add_instrumental")` |
| 加人聲 | `edit_music(action="add_vocals")` |
| 人聲分離 | `edit_music(action="separate_vocals")` |
| AI 寫歌詞 | `music_lyrics(action="generate")` |
| 字級時間軸歌詞 | `music_lyrics(action="timestamped")` |
| 轉 WAV | `music_utility(action="convert_to_wav")` |
| 生成 MV | `music_utility(action="create_music_video")` |
| 產編曲藍圖 | `compose_music(action="create_plan")` |
| 依藍圖生成 | `compose_music(action="compose")` |
