# 影片生成 — `generate_video` 完整參考

> 給 AI agent 讀的精確參考。對應 omniapi 工具：`mcp__omniapi-mcp__generate_video`（領取：`mcp__omniapi-mcp__get_job_result`）。
> 兩個供應商：大多數影片模型**經 OpenRouter**，需要 `PROVIDERS__OPENROUTER__API_KEY`（或在設定頁貼 OpenRouter 的 key）；Google 的 **`gemini-omni-1.1-flash`** 用 Google key **直連**（`PROVIDERS__GEMINI__API_KEY`，專案要開 Gemini API 付費層），見 §5。

---

## 0. 先記住三件事

1. **一送出就計費，沒有取消。** 供應商沒有取消的功能；就算之後不等了，那一支多半照樣做完、照樣收費。使用者沒明說要影片就不要叫。
2. **有每支上限。** 預估超過上限（出廠 $1）或送出前算不出金額的，**工具不會送出**，而是回一個 `refused`。要不要放行由使用者決定。
3. **影片要幾分鐘。** 呼叫等約 45 秒就回取件單（`task_id: "video_<id>"`），用 `get_job_result` 領。取件單在服務重啟後照樣有效（Gemini Omni 例外，見 §5）。

## 1. 參數

| 參數 | 說明 |
|---|---|
| `prompt` ★ | 1–4000 字元。寫主體、動作、鏡頭、光線、聲音 |
| `model` | `gemini-omni-1.1-flash`（Google 直連），或 OpenRouter 影片模型 id（例：`alibaba/wan-3.0`、`kwaivgi/kling-v3.0-std`、`x-ai/grok-imagine-video`、`google/veo-3.1-lite`、`runway/gen-4.5`、`bytedance/seedance-2.0-fast`，**實際以 `list_available_models(modality="video")` 為準**）。省略＝設定頁的影片預設模型（出廠 `alibaba/wan-3.0`，叫不到時用第一個能用的） |
| `duration` | 秒數，1–60，必須是該模型 `video_params.durations` 之一。省略＝5 秒；模型不收 5 秒就取最接近 5 的 |
| `resolution` | 該模型 `video_params.resolutions` 之一（如 `480p`／`720p`／`1080p`／`4K`）。省略＝模型自己的預設，**預估會變成一個區間**（每種解析度都可能） |
| `aspect_ratio` | 該模型 `video_params.aspect_ratios` 之一（如 `16:9`、`9:16`、`1:1`） |
| `generate_audio` | 要不要出聲。只有 `video_params.audio` 不是 false 的模型有；會出聲的模型預設開。有的模型有聲與無聲分開計價 |
| `seed` | 只對 `video_params.seed` 為 true 的模型有意義 |
| `first_frame` | 首幀（圖生影片）。三種給法：作品牆上一張圖的作品 id、本機圖檔路徑（stdio 下伺服器跟你共用檔案系統）、data URL。模型的 `video_params.frames` 要有 `first_frame` |
| `last_frame` | 尾幀，給法同上。只有 `video_params.frames` 有 `last_frame` 的模型收 |
| `max_cost_usd` | 這一支願意花到多少美元（以預估比）。超過上限、或模型送出前算不出價時才需要 |

不收的參數不要硬塞：先看 `list_available_models(modality="video")` 裡每個模型的 `video_params`（`durations`、`resolutions`、`aspect_ratios`、`frames`、`audio`、`seed`）與 `pricing`。

## 2. 花費上限與放行

上限在設定頁〈預設模型〉底下的「MCP 單支上限」：出廠 **$1**，可以改金額，也可以設「不限」。生成頁（GUI）每一支都有確認單，不受這個上限影響。

| 情況 | 回傳 | 怎麼辦 |
|---|---|---|
| 預估 ≤ 上限 | 送出 | — |
| 預估 > 上限 | `{status:"refused", reason:"over_limit", estimate_usd, estimate_low, estimate_high, limit_usd, message}` | 把金額轉告使用者；他同意就照 `message` 寫的數字帶 `max_cost_usd` 再叫一次。或改短、降解析度 |
| 送出前算不出（按 token 計價，如 Seedance） | `{status:"refused", reason:"price_unknown", estimate_usd:null, limit_usd, message}` | 使用者接受的話帶 `max_cost_usd`（當作他認可的上限）；或換按秒計價的模型 |

> **不要自己幫使用者加 `max_cost_usd` 重送**。被拒就是要人決定。

`max_cost_usd` 帶了，就以它取代設定頁的上限來比（預估還是超過它，照樣不送）。

## 3. 計價方式（預估怎麼來）

- 預估照 **OpenRouter 名單上的價格**算：大多是每秒價（依解析度、有聲／無聲分級），有的模型首幀圖另計、有的有每支最低收費。
- 沒指定解析度時，名單上幾種價都可能適用 → 回一個區間（`estimate.low`～`estimate.high`），比上限時用上緣。
- 按 token 計價的模型：每秒用多少 token 名單上沒寫，送出前算不出。
- **帳上記的是供應商實際收的**（完成時的 `cost_usd`）。OpenRouter 實際收的有時比名單低。Gemini Omni 的算法見 §5。
- 費用記在 GUI `/costs` 的「聊天・生成帳」。

## 4. 回傳

**完成**（等得到時直接回，或從 `get_job_result` 領到）：

`status:"completed"`、`generation_id`、`task_id`、`model`、`provider`、`artifact_id`、`file_path`、`video_url`（file://）、`file_url`、`duration_s`、`width`、`height`、`has_audio`（從檔案讀的）、`fps`、`cost_usd`、`estimate`、`waited_s`。服務重啟後才收回來的另帶 `resumed_after_restart: true`。

**還在做**：`status:"running"`、`task_id:"video_<id>"`、`remote_status`、`message`。隔一兩分鐘再 `get_job_result`。

**等太久**：超過設定頁的「最長等待」（出廠 20 分鐘）→ `status:"waited_too_long"`。供應商可能還在做、做好照樣收費；請使用者到 GUI 生成頁的出件口按「再去問一次」（用原本的工作編號再問，不重送、不多收）。

**沒收回**（使用者在 GUI 按了「不等了」，且設定是「不收」）→ `status:"not_collected"`。

**失敗** → `status:"failed"`、`error`、`error_kind`，以及 `charged`（有沒有可能已經收費）。失敗的不要自動重送：重送就是再花一次錢。

## 5. Gemini Omni（`gemini-omni-1.1-flash`，Google 直連）

| 項目 | 值 |
|---|---|
| key | Google 的 key（`PROVIDERS__GEMINI__API_KEY` 或設定頁），專案要開 **Gemini API 付費層**（沒有免費額度）。沒設時回 409，訊息寫明缺哪把 key |
| `duration` | 3–10 秒 |
| `resolution` | `360p`／`720p`（預設）／`1080p` |
| `aspect_ratio` | `16:9`／`9:16` |
| 聲音 | **一定有**，沒有開關：`generate_audio=false` 會被拒（`video_params.audio_fixed: true`）。不要聲音就在 prompt 寫 no dialogue / no music |
| 首尾幀 | 收 `first_frame`、`last_frame`；**`last_frame` 要搭 `first_frame`**（只給尾幀會被拒） |
| 預估 | 照 Google 定價頁按輸出 token 算：720p 每秒 5,792 token、約 $0.10／秒；其他解析度 Google 沒公布 token 數 → `price_unknown`，要使用者同意後帶 `max_cost_usd` |
| 實際費用 | Google 回報的費用（或依 usage 換算）；沒回報時記預估 |
| 重啟 | 這一版連著等到做好（同步）：**等待中服務重啟，那一支領不回來**（已送出、多半已收費）。不要在等 Omni 時建議重啟服務 |
| 重複列 | 有 Google key 時，OpenRouter 的 `google/gemini-omni*`、`google/veo-*` 不列出（沒 Google key 的人照樣看得到） |

實測：3 秒 720p 有聲，約 31 秒做好，實際 $0.31。

## 6. 範例

```
mcp__omniapi-mcp__list_available_models(modality="video")
mcp__omniapi-mcp__generate_video(prompt="a paper boat drifting down a rainy street, slow dolly-in, soft rain sound", model="alibaba/wan-3.0", duration=5, resolution="480p", aspect_ratio="16:9")
mcp__omniapi-mcp__generate_video(prompt="she turns toward the camera and smiles", model="kwaivgi/kling-v3.0-std", first_frame="C:/path/portrait.png", duration=5)
mcp__omniapi-mcp__generate_video(prompt="the city lights switch on one by one", model="kwaivgi/kling-v3.0-std", first_frame="<作品 id>", last_frame="<另一張作品 id>")
mcp__omniapi-mcp__generate_video(prompt="a fox runs through snow, wind and footsteps", model="gemini-omni-1.1-flash", duration=4, resolution="720p", aspect_ratio="16:9")
mcp__omniapi-mcp__get_job_result(task_id="video_…")
```

## 7. 不支援

- 影片延長、影片編輯、對嘴、放大、多張參考圖。OpenRouter 上這類模型（編輯、放大、數位人）不列在名單裡。
- 已經關閉的模型（例如 OpenAI 的 Sora 2）名單上標 retired，送不出去。
