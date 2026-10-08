# 圖像生成 / 編輯 — 完整參數參考

> 給 AI agent 讀的精確參考。涵蓋 **OpenAI gpt-image**、**Google Gemini Nano Banana**，以及 **經 OpenRouter 的圖片模型**（§E）。
> 對應 omniapi 工具：`mcp__omniapi-mcp__generate_image`、`mcp__omniapi-mcp__edit_image`。
> **參數／定價查證：2026-06-28**（OpenAI developers.openai.com + Google ai.google.dev）。
> **模型名單查證：2026-09-25**（各家官方文件與公開榜單）。
> 標 `🔶` = 官方僅透過計算器/SDK 提供（非靜態表，已驗算吻合）；`⚠️` = 官方未逐字正面陳述的推論。

---

## 0. omniapi 工具對接現況（先讀）

| 工具 | 能傳的參數 |
|---|---|
| `generate_image` | `prompt`★、`model`、`size`、`quality`、`style`、`output_format`、`compression`、`background`、`moderation`、`n`、`user`、`image_size`、`aspect_ratio`、`person_generation`、`seed`（只有部分 OpenRouter 模型吃）＋三個歷史遺留欄位（見下） |
| `edit_image` | `image_data` **或** `image_path`★、`prompt`★、`mask_data`、`model`、`size`、`quality`、`output_format`、`compression`、`background`、`input_fidelity`、`additional_images`、`additional_image_paths`、`user`、`image_size`、`aspect_ratio`（後兩個只對 OpenRouter 模型有效） |

**✅ 重點能力：**
- OpenAI：**`n`**（多圖 1-10，n>1 回傳 `images[]` 清單＋`count`，n=1 維持單圖形狀——已實機驗證）、`user`；`edit_image` 有 **`input_fidelity`**(`high` 貼近原圖五官/風格，僅 gpt-image-1 系)、**`additional_images`**(多張參照圖合成，最多 16)、可選 **`model`**。
- 本機檔路徑：`image_path` / `additional_image_paths`（STDIO transport 下首選——全解析度參照、免塞 base64）。
- Gemini：**`image_size`**(`512`/`1K`/`2K`/`4K`)、**`aspect_ratio`**(顯式比例，解鎖 size 映射不到的 16:9/9:16/21:9 等)、`person_generation`。Nano Banana 也正確吃 `output_format`。

**🕘 歷史遺留、現已全部被忽略**：`safety_filter_level`、`enhance_prompt`、`guidance_scale`。這幾個是 2026 年退場的舊 Google 生圖路徑留下的欄位，為了不讓舊呼叫端壞掉而保留簽章，**任何現役模型都不吃**。別靠它們做事。`seed` 原本也是，現在只有 `image_params.seed` 為 true 的 OpenRouter 模型會用它。

**仍刻意跳過（附原因）：** `stream`/`partial_images`(串流不適合單次回傳)、`response_format`(內部存檔自理)、`output_mime_type`(已由 `output_format` 涵蓋)。

> 🔀 **`edit_image` 依 model 路由**：`gpt-image-*` → OpenAI（吃遮罩）；Nano Banana → Gemini（參照圖走 `additional_images`，不吃遮罩）；OpenRouter 的 `vendor/model` id → OpenRouter（限 `image_params.max_references > 0` 的模型，來源圖＋`additional_images` 一起當參照圖，不吃遮罩）。
> 🚨 **`edit_image` 的預設 model 以 `IMAGES__DEFAULT_MODEL` 為準**（出廠 `gpt-image-2`）。要哪個就明寫。

---

## 1. ⭐ 核心設計哲學：「先 low 後 high」（gpt-image-2 省最多）

**實證**：gpt-image-2 在 1024×1024 下，`quality=high` 每張 **$0.211**、`quality=low` **$0.006** — **high 比 low 貴約 35 倍**（成本完全由 output image token 數驅動，low/medium/high 的 token 量約 1× / 9× / 36×）。

**工作流**：
1. 構圖/探索階段 → `quality=low` 快速便宜大量迭代（一張不到一分錢）。
2. 選定構圖後 → 同 prompt 改 `quality=high`（或 `medium`）出定稿。

> 💡 各模型 low→high 倍率：gpt-image-2 **≈35×**（省最多）、gpt-image-1.5 ≈15×、gpt-image-1 ≈15×、gpt-image-1-mini ≈7×。
> ⚠️ 速度差異（low 較快）是合理推論（token 少→生成快），但 OpenAI **未公布**各 quality 的延遲數字，別當官方數據引用。

---

## A. OpenAI 圖像 API

端點：`POST /v1/images/generations`、`POST /v1/images/edits`。

### A.1 模型名單與狀態（2026-09-25）

| 模型 | 狀態 | 定位 |
|---|---|---|
| `gpt-image-2.5-sunburst` | current | 2026-09-08 發布；生圖榜 + 改圖榜**雙第一** |
| `gpt-image-2.5-flare` | current | 同代，兩榜第二 |
| `gpt-image-2` | current | 自由解析度到 4K；兩榜第三 |
| `gpt-image-1.5` | ⚠️ deprecated | **2026-12-01 關閉**；較快、**真‧透明背景**、吃 `input_fidelity` |
| `gpt-image-1-mini` | ⚠️ deprecated | **2026-12-01 關閉**；最便宜 |
| `gpt-image-1` | ⚠️ **deprecated** | **2026-10-23 關閉**；仍可叫但會記 warning |

> 舊的 `dall-e-2` / `dall-e-3` 已於 **2026-05-12 被 OpenAI 關閉**，omniapi 已從名單移除——現在叫會直接錯。

### A.2 各模型 size / quality / 透明 / 4K 矩陣

| 模型 | 標準尺寸 (1024²/1024×1536/1536×1024) | 自訂尺寸 + 2K/4K | quality | `background=transparent` |
|---|---|---|---|---|
| **`gpt-image-2.5-sunburst` / `-flare`** | ✅ | ✅ 自由解析度(≤3840px) | auto/high/medium/low | ❌ 同 gpt-image-2（自動降級為 auto） |
| **`gpt-image-2`** | ✅ | ✅ 自由解析度(≤3840px)，含 **4K 3840×2160** | auto/high/medium/low | ❌ **不支援**（官方逐字確認；傳了會被靜默降級成 `auto`） |
| `gpt-image-1.5` | ✅ | ❌ 僅三標準尺寸 | auto/high/medium/low | ⚠️ 支援（png/webp 透明） |
| `gpt-image-1` | ✅ | ❌ | auto/high/medium/low | ⚠️ 支援 |
| `gpt-image-1-mini` | ✅ | ❌ | auto/high/medium/low | ⚠️ 支援 |

**關鍵**：**4K / 自訂解析度是 gpt-image-2 家族（2 與 2.5）獨有**；**這家族不支援透明背景** → 要透明請用 `gpt-image-1.5` + `output_format=png`。

### A.3 `generate_image` 參數完整可選值

| 參數 | 可選值 | 預設 | 說明 |
|---|---|---|---|
| `prompt` ★ | 文字（工具限 1–4000 字元） | 必填 | 越具體越好：主體+場景+風格+光線+構圖 |
| `model` | 見 A.1（+ Gemini 系，見 B） | `IMAGES__DEFAULT_MODEL`（出廠 `gpt-image-2`） | 模型 |
| `size` | `auto`/`1024x1024`/`1536x1024`(橫)/`1024x1536`(直)；gpt-image-2 家族另可 `3840x2160` 等自訂(見 A.5) | `auto` | 尺寸 |
| `quality` | `auto`/`high`/`medium`/`low` | `auto` | ⭐成本主槓桿(見 §1) |
| `background` | `auto`/`transparent`/`opaque` | `auto` | gpt-image-2 家族的 transparent 會被降級 |
| `output_format` | `png`/`jpeg`/`webp` | `png` | 透明需 png/webp |
| `compression` | 0–100(%) | 100 | 僅 jpeg/webp |
| `moderation` | `auto`/`low` | `auto` | 審查嚴格度 |
| `n` | 1–10 | 1 | gpt-image 系最多 10；**Gemini 只能 1** |
| `style` | `vivid`/`natural` | `vivid` | OpenAI 限定欄位；現役 gpt-image 系實質不吃（`supports_style=False`） |
| `user` | 字串 | — | OpenAI 濫用監控用的穩定使用者識別碼 |

### A.4 `edit_image` 參數

`image_data`(base64/data URL) **或** `image_path`(本機路徑)★、`prompt`★、`mask_data`(PNG 遮罩，**alpha=0 透明處=要重繪區**，需與來源同尺寸)、`size`/`quality`/`output_format`/`compression`/`background`(同 generate)。
- ✅ **`input_fidelity`**(`high`/`low`)：貼近原圖五官/風格，**僅 gpt-image-1 家族**（`gpt-image-1`/`1.5`/`1-mini`）；傳給 gpt-image-2 家族會自動忽略並記 warning。
- ✅ **`additional_images`** / **`additional_image_paths`**：多張參照圖合成，gpt-image edits 最多 16 張；**遮罩只作用在第一張**。

### A.5 gpt-image-2 家族自訂尺寸限制（官方逐字）

- 最長邊 **≤ 3840px**；兩邊都須 **16 的倍數**；長:短邊比 **≤ 3:1**；總像素 **655,360 ~ 8,294,400**。
- 超過 2560×1440(2K) 總像素的輸出官方標 **experimental**。
- 常用：`1024x1024`/`1536x1024`/`1024x1536`/`2048x2048`/`3840x2160`/`2160x3840`/`auto`。
- omniapi 會先拿模型能力表正規化 size，非法自訂尺寸會回落到支援值（不是直接 400）。

### A.6 成本速查（每張 USD，1024×1024 方形價）

| 模型 | low | medium | high | 4K(high)🔶 |
|---|---|---|---|---|
| gpt-image-2.5-sunburst / -flare | 與 gpt-image-2 同一張價目表（文字輸入 $5 / 圖像輸入 $8 / 圖像輸出 $30，每 1M tokens） | | | |
| **gpt-image-2** | $0.006 | $0.053 | $0.211 | ≈$0.40 |
| gpt-image-1.5 | $0.009 | $0.034 | $0.133 | — |
| gpt-image-1 | $0.011 | $0.042 | $0.167 | — |
| gpt-image-1-mini | $0.005 | $0.011 | $0.036 | — |

（gpt-image-2 的 2K/4K 為官方 token 計算器算出 🔶，已用標準尺寸價反算吻合。gpt-image-2.5 的每張價未見官方靜態表，只有 per-1M-token 單價——**別直接套 gpt-image-2 的每張數字當保證**。）

---

## B. Google Gemini — Nano Banana

> 🕘 **Google 的專用生圖模型系列已全面退場**（2026 年關閉），連帶那條走雲端企業平台、`generate_images` 端點、service-account 憑證的路徑也一起沒了。omniapi 自 v1.0 起**只留 Nano Banana 系列**，並改用 **AI Studio API key**（`genai.Client(api_key=…)`）——`PROVIDERS__GEMINI__API_KEY` 給檔案路徑會直接被擋下。

呼叫方式：`generate_content` + `GenerateContentConfig(response_modalities, image_config)`。

### B.1 模型名單（2026-09-25）

| model id | 別名 | 狀態 | 特點 |
|---|---|---|---|
| `gemini-3.1-flash-image` | `nano-banana-2` | current（omniapi Gemini 預設） | 512/1K/2K/4K、最多 **14** 張參照圖、極端比例 |
| `gemini-3-pro-image` | `nano-banana-pro` | current | Pro，1K/2K/4K、最多 6 張參照圖 |
| `gemini-3.1-flash-lite-image` | `nano-banana-2-lite` | current | 便宜版 |
| `gemini-2.5-flash-image` | `nano-banana` | ❌ **retired** | 初代，實質 1K；2026-10-02 關閉 |

> ⚠️ `"nano-banana"` 本身是行銷名、不是 API 字串；omniapi 把上表的別名接下來並映射成真正的 model id，直接寫 model id 也可以。

### B.2 `image_config` 核心欄位

- **`aspect_ratio`**：`1:1`/`2:3`/`3:2`/`3:4`/`4:3`/`4:5`/`5:4`/`9:16`/`16:9`/`21:9`（共 10 種，**信官方 10 種，SDK docstring 漏列 4:5/5:4**）；3.1 Flash 另加 `1:4`/`4:1`/`1:8`/`8:1`。
- **`image_size`**：`512`/`1K`/`2K`/`4K`（**必須大寫 K**，小寫 `1k` 被拒），預設 2K。2K/4K 僅 Gemini-3 世代；`gemini-2.5-flash-image` 實質 1K。
- 只給 `size` 不給 `aspect_ratio` 時的自動映射：`1536x1024`→**`3:2`**、`1024x1536`→**`2:3`**、`1024x1024`／`auto`→`1:1`（用 NB 原生比例，精確對應）。
- `person_generation`：`dont_allow` / `allow_adult` / `allow_all`。

### B.3 取圖與限制

- 取圖：`response.candidates[0].content.parts[*].inline_data.data`（**raw bytes，勿再 base64 decode**）。
- ⚠️ **一次一張**（`max_images_per_request=1`）——要多張須多次呼叫，`n>1` 對 Gemini 無效。
- 不支援 `background`（透明）與 `compression`。

### B.4 定價（每張 USD，2026-09-25）

| 模型 | 0.5K | 1K | 2K | 4K | 文字輸入 |
|---|---|---|---|---|---|
| `gemini-3.1-flash-image` | $0.045 | $0.067 | $0.101 | $0.151 | $0.50 / 1M tok |
| `gemini-3-pro-image` | — | $0.134 | $0.134 | $0.24 | $2.00 / 1M tok |
| `gemini-3.1-flash-lite-image` | — | $0.045 ⚠️未驗證 | $0.067 ⚠️未驗證 | — | — |
| `gemini-2.5-flash-image` | — | $0.039 | — | — | — |

（`gemini-3.1-flash-lite-image` 的兩個數字在程式裡就標著 unverified，別當官方價引用。）

---

## E. 經 OpenRouter 的圖片模型

有 OpenRouter 的 key 就能用，**不用另外辦 key**。FLUX、Seedream、Grok Imagine、Qwen、Recraft、MAI 等等；名單與價格向 OpenRouter 現查、每天更新，用 `list_available_models(modality="image")` 看（`provider: "openrouter"`）。

- **model 照 OpenRouter 的寫法**：`black-forest-labs/flux-3-image`、`x-ai/grok-imagine-image-2.0`、`bytedance-seed/seedream-5-0-pro`、`qwen/qwen-image-3`、`microsoft/mai-image-2.6`……（例子，以名單為準）。
- **每個模型收什麼看 `image_params`**：`aspect_ratios`（→ `aspect_ratio`）、`resolutions`（→ `image_size`，如 `512`／`768`／`1K`／`1.5K`／`2K`／`4K`）、`qualities`（→ `quality`）、`max_references`（參考圖上限；0＝不能改圖）、`seed`。`size`（WxH）只給了的話會換算成比例。
- **改圖**：`edit_image(model=<OpenRouter id>)`，來源圖＋`additional_images` 一起當參考圖送；不吃遮罩。
- **不重複**：OpenAI、Google 的 key 有設的話，OpenRouter 上同家的那一份（例如 `google/gemini-3.1-flash-image`）不列，請直接用直連的 id。
- **不支援**：只出 SVG 向量圖的模型（Recraft 的向量款），作品牆這一版不收，標為不可用。
- **費用**：預估照 OpenRouter 名單（每張、依解析度／品質分級、參考圖另計，或按 token）；回傳與帳上記的是 OpenRouter 實際收的，有時比名單低。

```
mcp__omniapi-mcp__generate_image(prompt="isometric tiny bakery, warm morning light", model="black-forest-labs/flux-3-image", aspect_ratio="1:1")
mcp__omniapi-mcp__edit_image(image_path="C:/path/sketch.png", prompt="render as a clean product photo", model="black-forest-labs/flux-3-image")
```

---

## C. 選型速查

```
生圖／改圖品質天花板       → gpt-image-2.5-sunburst（兩榜第一）
高解析海報 / 4K            → gpt-image-2 + size=3840x2160 + quality=high（先 low 試構圖！）
透明背景 logo/貼圖          → gpt-image-1.5 + background=transparent + output_format=png
大量草稿 / 探索             → gpt-image-1-mini 或 任意模型 quality=low
多參照圖合成（最多 14 張）  → nano-banana-2 (gemini-3.1-flash-image)
極端比例 21:9 / 4:1        → nano-banana-2 + aspect_ratio
省錢 Gemini 生圖            → gemini-3.1-flash-lite-image
改圖                        → gpt-image 系（可用遮罩）、Nano Banana、或 max_references > 0 的 OpenRouter 模型
FLUX／Seedream 等其他風格    → OpenRouter id（先看 image_params）
```

## D. 寫 code / 直呼 API 時的 7 個雷
1. **4K / 自訂解析度只有 gpt-image-2 家族**；**該家族不支援透明背景**（透明用 1.5 + png）。
2. **「先 low 後 high」省最多在 gpt-image-2**（35× 價差，官方價實證）。
3. **Google 舊的專用生圖那條路已經沒了**：沒有 service account、沒有 `generate_images`，只剩 Nano Banana 的 `generate_content` + AI Studio key。
4. Gemini `image_size` 必須**大寫 K**；2K/4K 僅 Gemini-3 世代；一次只出一張。
5. `safety_filter_level`/`enhance_prompt`/`guidance_scale` 現在**全是裝飾**，傳了不會報錯也不會生效（`seed` 只有部分 OpenRouter 模型吃）。
6. Nano Banana aspect_ratio 信**官方 10 種**（SDK docstring 漏列）；取圖 `inline_data.data` 是 **raw bytes**。
7. gpt-image-2 自訂尺寸：邊長 16 倍數、≤3840、比例 ≤3:1、像素 655,360~8,294,400。

## 來源
- 參數／定價（2026-06-28）：OpenAI developers.openai.com/api/docs/guides/image-generation、…/reference/python/resources/images、…/docs/pricing；Gemini ai.google.dev/gemini-api/docs/image-generation、…/docs/pricing；googleapis.github.io/js-genai（ImageConfig）。
- 模型名單與榜單（2026-09-25）：各家官方文件與公開榜單。
