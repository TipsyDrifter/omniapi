# 文字 / 對話 (Chat Completions) — 完整參數參考

> 給 AI agent 讀的精確參考。對應 omniapi 工具：`mcp__omniapi-mcp__complete_text`。
> **參數查證：2026-06-28**（深挖 OpenAI 與 DeepSeek 的 `chat.completions` 全參數）。
> **模型名單／定價／供應商查證：2026-09-25**（各家官方文件與公開榜單）。
> 每節附官方來源 URL。參數名 / model ID / enum / 欄位名保留英文，說明用繁體中文。
> ⚠️ 標 `[需實證]` 的項目官方文件未逐字背書或互相矛盾，呼叫前建議用 `GET /models` 或實際 400 回饋核對，**不要當保證**。

---

## ⓪ omniapi `complete_text` 對接現況（先讀）

`complete_text` 現在路由到 **五家**：OpenAI（GPT-6 / GPT-5.x）、Anthropic（Claude）、Google（Gemini）、DeepSeek、OpenRouter（長尾）。本檔的 A 段（OpenAI）與 B 段（DeepSeek）是深挖過的完整參數；其餘三家的差異整理在 §0.6。

omniapi 的 `complete_text` 暴露：`prompt`★ **或** `messages`(多輪)、`model`、`system`、`temperature`、`top_p`、`seed`、`stop`、`reasoning_effort`、`verbosity`、`max_completion_tokens`、`response_format`、`thinking`、`tools`、`tool_choice`、`parallel_tool_calls`、`store`、`metadata`、`service_tier`、`prompt_cache_key`、`safety_identifier`。

**✅ 陷阱已修（temperature 防呆）：**
- ⚠️→✅ **`temperature` 不再會炸**：GPT-5.x reasoning 模型收到非預設 temperature 本會回 HTTP 400（見 A.3），但 omniapi 現在**偵測到 reasoning 模型會自動丟棄 temperature**（記一筆 warning log，不送出）→ 傳了也安全。DeepSeek 則照常轉送（非 thinking 模式有效、thinking 模式靜默忽略）。`*-chat-latest` 這類非 reasoning 變體會保留 temperature。
- ✅ **`system` 自動轉 `developer`**：對 GPT-5.x reasoning 模型，omniapi 自動把 system role 改成 developer role（見 A.4）；DeepSeek 保留 system。
- ✅ **`max_completion_tokens` 自動對應**：OpenAI 用 `max_completion_tokens`，DeepSeek 自動改送 `max_tokens`（見 §0 心智模型）。
- ✅ **`thinking`**：工具收 bool（`true`/`false`），自動包成 DeepSeek 的 `extra_body={"thinking":{"type":"enabled"|"disabled"}}`；對 OpenAI 自動略過。
- ✅ **`response_format`**：直接傳 dict，如 `{"type":"json_object"}` 或 OpenAI 的 `{"type":"json_schema","json_schema":{...}}`。
- ✅ **回傳強化**：`usage` 現在含 `reasoning_tokens`（OpenAI reasoning）與 `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`（DeepSeek）；reasoning 模型的 CoT 在 `reasoning` 欄位。

**✅ 新增（2026-06-28 大擴充）：**
- ⭐ **Function calling**：`tools`(function 定義陣列)、`tool_choice`(`none`/`auto`/`required`/指定)、`parallel_tool_calls`(OpenAI)。模型呼叫工具時，回傳新增 `tool_calls` 欄位（list of {id, function:{name, arguments}}）。多輪：用 `messages` 把 `{role:"tool", tool_call_id, content}` 餵回續跑。**已實機驗證**（gpt-5.4-mini 正確回 tool_calls）。
- **`messages` 多輪**：可改傳完整對話陣列取代 prompt+system（餵 tool 結果、多輪對話）。
- **取樣 `top_p`/`seed`/`stop`**：⚠️ GPT-5.x reasoning 模型對這三個（連同 temperature）**一律 400**，故 omniapi 偵測 reasoning 模型時**全部自動丟棄**（`stop` 也在內——實機驗證 gpt-5.4-mini 確實拒絕 stop，文件沒寫）；DeepSeek 保留。
- **OpenAI 限定**：`store`/`metadata`(stored completion)、`service_tier`(flex/priority)、`prompt_cache_key`、`safety_identifier` → 自動只送 OpenAI，DeepSeek 略過。

**仍刻意跳過（附原因）：** `n>1`(會破壞單一結果回傳形狀)、`frequency_penalty`/`presence_penalty`(DeepSeek V4 已移除＋OpenAI reasoning 400，現役模型無可用路徑)、`logprobs`/`top_logprobs`/`logit_bias`(reasoning 400)、`stream`/`stream_options`(串流不適合單次回傳)、`user`(已棄用，改 safety_identifier)、DeepSeek beta 的 FIM / Prefix Completion(**不同端點**，屬獨立工具)。

> **現在的安全用法**：`complete_text(prompt=..., model="strong", reasoning_effort="high", verbosity="low", max_completion_tokens=4096)`。結構化輸出加 `response_format`；工具呼叫加 `tools=[...]`＋`tool_choice`；DeepSeek 思考加 `thinking=true/false`。傳 temperature/top_p/seed/stop 也不會炸（reasoning 模型自動丟）。

**回傳欄位**：`text`、`model`（實際用的）、`requested_model`（你傳的原字串）、`provider`、`reasoning`、`finish_reason`、`tool_calls`、`usage`、`cost_usd`、`deprecated`。

---

## 0.5 ⭐ 等級別名：先用別名，別寫死型號

`model` 除了 model id，還吃三個**等級別名**。出廠對應由 catalog 的一張表決定，**使用者可以在網頁設定頁（`/settings`）覆寫**，改了立刻生效；現在實際指到哪裡看 `list_available_models` 回傳的 `tiers`：

| 別名 | 出廠對應（2026-09-25） | 用途 |
|---|---|---|
| `cheap` | `deepseek-flash` | **系統預設**；日常問答、批量整理、便宜跑量 |
| `standard` | `gemini-3.8-flash` | 一般任務，要快要穩 |
| `strong` | `gpt-6-sol` | 最難的推理 / 長文 / 規劃 |

> 換模型只要改 catalog 那張表，寫別名的呼叫端不用跟著改——這是刻意的設計。**guides 與 prompt 一律寫別名**，只有在「就是要這顆模型」時才寫 id。
>
> ⚠️ 預設的實際行為：`cheap` 對應的 provider 有配好 key 才會用它；否則退回「第一個註冊成功的 provider 的預設模型」（註冊順序 openai → deepseek → anthropic → gemini → openrouter）。所以只配了 OpenAI key 的環境，預設會落在 `gpt-5.4-mini`。

## 0.6 五家供應商一覽（名單是即時的）

| 家 | 代表模型 | 端點 / 相容性 | 備註 |
|---|---|---|---|
| **OpenAI** | `gpt-6-astra`（最強）、`gpt-6-sol`、`gpt-6-luna`、`gpt-5.5`/`-pro`、`gpt-5.4`/`-pro`/`-mini`/`-nano`、`gpt-5.3-codex` | 原生 `chat.completions` | reasoning 模型拒收 sampling 參數（A.3） |
| **Anthropic** | `claude-fable-5-1`、`claude-fable-5`、`claude-opus-5-5`、`claude-opus-5`、`claude-opus-4-8`/`4-7`/`4-6`、`claude-sonnet-5`、`claude-haiku-4-5-20251001` | 原生 **Messages API**（omniapi 內部做形狀轉換） | `reasoning_effort` 轉成 extended-thinking budget：low 1024／medium 4096／high 16000／xhigh 32000 tokens |
| **Google** | `gemini-3.8-flash`、`gemini-3.7-flash`、`gemini-3.1-pro-preview` | **OpenAI 相容端點** `…/v1beta/openai/` | function calling / JSON schema / `reasoning_effort` 直通；**sampling 參數照收不 400** |
| **DeepSeek** | `deepseek-flash`、`deepseek-v4-pro` | OpenAI 相容（`api.deepseek.com`） | 見 B 段；另有 Anthropic 相容端點 `…/anthropic` |
| **OpenRouter** | `vendor/model` 命名空間，如 `moonshotai/kimi-k3` | OpenAI 相容 | 名單全靠 discovery；**未知 id 照樣轉發**（長尾每天變） |

> 📡 **名單是即時的**：啟動時向各家 `/models` 問現在哪些在線，再與人工維護的定價／能力覆蓋層合併（24h 快取）。要看現況跑 **`list_available_models(modality="text")`**；`include_retired=true` 看已下架的、`include_snapshots=true` 看帶日期的 snapshot id（如 `gpt-5.5-2026-04-23`，平常隱藏但可叫）、`refresh=true` 強制重抓。
> 別把本檔的表當名單真相——它是**解說**，`list_available_models` 才是現況。

---

## 0. 兩家共通的心智模型（agent 先讀這段）

| 重點 | OpenAI GPT-5.x | DeepSeek V4 |
|---|---|---|
| 端點 | `POST /v1/chat/completions` | `POST /chat/completions` |
| Base URL | `https://api.openai.com/v1` | `https://api.deepseek.com`（beta 功能改 `/beta`） |
| 輸出長度參數 | **`max_completion_tokens`**（舊 `max_tokens` 已棄用、與 reasoning 模型不相容） | 仍用 **`max_tokens`** |
| 推理深度旋鈕 | `reasoning_effort`（`none`/`low`/`medium`/`high`/`xhigh`） | `reasoning_effort`（只有 `high`/`max`）+ `thinking` 開關 |
| 詳盡度旋鈕 | `verbosity`（`low`/`medium`/`high`） | 無 |
| 思考過程欄位 | 隱藏，不回傳內容；只在 usage 報 token 數 | **`reasoning_content`**（會回傳實際 CoT 文字） |
| sampling 參數（temperature 等）在推理時 | **回 HTTP 400 拒絕**（temperature 只收預設 1） | **靜默忽略**（不報錯、無效果） |
| 結構化輸出 | `json_schema` 嚴格模式 ✅ | 只有 `json_object`，**無 `json_schema`** |
| 系統指令 role | **`developer`**（取代 `system`） | `system` |

**對 agent 最重要的 3 個雷區：**
1. **OpenAI 推理模型不要送任何 sampling 參數**（temperature/top_p/penalties/logprobs/logit_bias/seed/n）→ 會 400。要調風格改用 `reasoning_effort` + `verbosity`。
2. **DeepSeek thinking 模式下 sampling 參數會被靜默忽略**（不報錯但無效），別誤以為有生效。
3. **DeepSeek 多輪 + 思考**：歷史訊息裡的 `reasoning_content` 預設要剝掉；只有 tool-call 串接的回合才原樣傳回（否則 V4 回 400；舊 reasoner 任何情況傳回都 400）。

---

# A. OpenAI Chat Completions（GPT-5.x 系列）

> 主要來源（皆 2026-06-28 查證）：
> [Create chat completion reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、
> [Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)、
> [Reasoning best practices](https://developers.openai.com/api/docs/guides/reasoning-best-practices)、
> [Using GPT-5.5](https://developers.openai.com/api/docs/guides/latest-model)、
> [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、
> [Models / all](https://developers.openai.com/api/docs/models/all)。
> ⚠️ `platform.openai.com/docs/api-reference/*` 抓取時回 403，故 reference 改用官方 `developers.openai.com` 鏡像；少數數值預設（range）因抓取頁截斷，依 OpenAI 自 GPT-4o 起的穩定慣例補入並標注 `[慣例]`。

## A.0 端點與 Responses API 提醒

| 項目 | 值 |
|---|---|
| Endpoint | `POST /v1/chat/completions` |
| SDK | `client.chat.completions.create(...)` |
| ⚠️ 官方建議 | Reasoning 模型「在 **Responses API** 上表現更好」；Chat Completions 仍受支援但官方推薦改 Responses 以取得更佳智慧與效能（[Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)）。本參考聚焦 Chat Completions。 |

Responses API 一句話差異：`POST /v1/responses` 是新一代 stateful 端點，原生承載 reasoning（`previous_response_id` 跨輪保留推理、`reasoning.effort` / `text.verbosity` 為巢狀參數、reasoning token 回報在 `output_tokens_details`）。

## A.1 當前 OpenAI model ID（GPT-6 / GPT-5.x）

> 名單與定價來源：[Models](https://developers.openai.com/api/docs/models)、[Pricing](https://developers.openai.com/api/docs/pricing)，**2026-09-25**
> 參數行為來源：[gpt-5.5 model](https://developers.openai.com/api/docs/models/gpt-5.5)，2026-06-28

**GPT-6 世代（現役，全部 current）：**

| Model ID | 定位 | context / max output | 定價（每 1M tok） |
|---|---|---|---|
| **`gpt-6-astra`** | 最強；Artificial Analysis 53 | 1,050,000 / 128,000 | in $10 · cached $1 · out $50（長上下文 in $20 / out $75） |
| **`gpt-6-sol`** ⭐`strong` 別名指向這顆 | 性價比旗艦；AA 48 | 1,050,000 / 128,000 | in $2 · cached $0.2 · out $10（長上下文 $4/$15）**促銷價至少到 2026-11-21** |
| `gpt-6-luna` | 同代輕量 | 1,050,000 / 128,000 | 官方未列靜態價 |

三顆都支援 tools / structured output / reasoning；astra 與 sol 另有 vision。

**GPT-5.x 世代（仍現役）：** `gpt-5.5`、`gpt-5.5-pro`、`gpt-5.4`、`gpt-5.4-pro`、`gpt-5.4-mini`、`gpt-5.4-nano`、`gpt-5.3-codex`。

| Model ID | 類型 | `reasoning_effort` 支援值 | 預設 | 備註 |
|---|---|---|---|---|
| **`gpt-5.5`** | Reasoning | `none`/`low`/`medium`/`high`/`xhigh` | `medium` | snapshot `gpt-5.5-2026-04-23`；context **1,050,000** / max output **128,000**；cutoff 2025-12-01 |
| `gpt-5.5-pro` | Reasoning（深度） | 偏高 effort | — | `[需實證]` **可能僅 Responses API**，調 chat 前先確認 |
| `gpt-5.4` / `-pro` | Reasoning | 同上 `[需實證]` | `medium` `[需實證]` | 「更平價」；cutoff 2025-08-31 |
| `gpt-5.4-mini` | Reasoning | 同上 `[需實證]` | `medium` `[需實證]` | 最強 mini（coding / computer use / subagents）；**只配 OpenAI key 時的預設模型** |
| `gpt-5.4-nano` | Reasoning | 同上 `[需實證]` | `medium` `[需實證]` | 最便宜（簡單高量任務） |
| `gpt-5.3-codex` | Reasoning | 最低 `low` `[需實證]` | — | coding 專用 |

> ⚠️ `[需實證]` **GPT-6 的 `reasoning_effort` 分檔未逐一查證**——上表的 `none`/`low`/`medium`/`high`/`xhigh` 是 GPT-5.5 的官方值，GPT-6 沿用是**推論**。要用極端檔位（`none`/`xhigh`）前先實測。
> ⚠️ `*-chat-latest` 變體 = **非 reasoning 的「Instant」模型**，只支援 `reasoning_effort='medium'`，行為近傳統 chat；它們**保留 temperature**（不在自動丟棄之列）。
> 🕘 更舊的 `gpt-5.2` / `gpt-5.1` / `gpt-5` 系已退出現役名單。要確認某個舊 id 還在不在，跑 `list_available_models(modality="text", include_retired=true)`。

## A.2 ⭐ 完整 `chat.completions.create` 參數表

| 參數 | 型別 | 允許值 / 範圍 | 預設 | 說明 |
|---|---|---|---|---|
| `model` | string | 見 A.1 | 必填 | 模型 ID |
| `messages` | array | 見 A.4 | 必填 | 對話訊息陣列 |
| `max_completion_tokens` | integer | ≥1 | 受模型 max output 限制 | **取代 `max_tokens`**；上限**包含 reasoning token + 可見輸出** |
| `max_tokens` | integer | ≥1 | — | ⚠️ **已棄用**，由 `max_completion_tokens` 取代；**與 reasoning 模型不相容**，GPT-5.x 請勿用 |
| `reasoning_effort` ⭐ | string | `none`/`minimal`/`low`/`medium`/`high`/`xhigh`（依模型，見 A.3） | `medium`(gpt-5.5) | 控制推理深度 |
| `verbosity` ⭐ | string | `low`/`medium`/`high` | `medium` | 控制回應詳盡度 |
| `temperature` | number | 0–2 `[慣例]` | 1 | ⚠️ reasoning 模型**只收預設 1，其餘回 400**（A.3） |
| `top_p` | number | 0–1 `[慣例]` | 1 | Nucleus sampling；⚠️ reasoning **不支援**（400） |
| `frequency_penalty` | number | −2.0–2.0 `[慣例]` | 0 | 依頻率抑制重複；⚠️ reasoning **不支援** |
| `presence_penalty` | number | −2.0–2.0 `[慣例]` | 0 | 鼓勵新主題；⚠️ reasoning **不支援** |
| `stop` | string / array(≤4) | 最多 4 序列 | null | 命中即停止 |
| `n` | integer | ≥1 | 1 | 產生幾個 choice；⚠️ reasoning 一般僅 1 |
| `seed` | integer | 任意整數 | null | 盡量可重現；⚠️ reasoning 建議不送 |
| `stream` | boolean | true/false | false | SSE 串流（以 `data: [DONE]` 結束） |
| `stream_options` | object | `{"include_usage": true}` | null | 串流附加（最後一塊含 usage） |
| `logprobs` | boolean | true/false | false | 回傳 token log 機率；⚠️ reasoning **不支援** |
| `top_logprobs` | integer | 0–20 `[慣例]` | null | 每位置前 N 個 logprob（需 `logprobs:true`）；⚠️ reasoning **不支援** |
| `logit_bias` | map | token→ −100..100 | null | 調整特定 token 機率；⚠️ reasoning **不支援** |
| `response_format` | object | `{type:"text"}` / `{type:"json_object"}` / `{type:"json_schema", json_schema:{...}}` | `{type:"text"}` | 輸出格式（見 A.5） |
| `tools` | array | function 定義陣列 | null | 可呼叫工具（見 A.6） |
| `tool_choice` | string / object | `none`/`auto`/`required` / 指定 function / `allowed_tools` | `auto`（有 tools 時） | 控制是否/呼叫哪個工具 |
| `parallel_tool_calls` | boolean | true/false | true | 單輪是否允許平行多工具呼叫 |
| `store` | boolean | true/false | false | 是否儲存此次 completion（供日後 eval/蒸餾） |
| `metadata` | map(≤16) | key-value 字串對 | null | 附在 stored completion 的自訂中繼資料 |
| `prediction` | object | `{type:"content", content:...}` | null | **Predicted Outputs**：提供預測內容加速（大幅重寫已知文本時） |
| `service_tier` | string | `auto`/`default`/`flex`/`priority` | `auto` | 處理級別（延遲/價格取捨） |
| `prompt_cache_key` | string | 任意字串 | null | 提升 prompt caching 命中率的快取鍵 |
| `safety_identifier` | string | 任意字串 | null | 濫用偵測的終端使用者識別（較新，部分取代 `user`） |
| `user` | string | 任意字串 | null | 終端使用者識別（舊欄位；建議改 `safety_identifier`/`prompt_cache_key`） |

> ✅ 上表參數「存在性」全部見於官方 [chat create reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)（含 `service_tier`/`prompt_cache_key`/`safety_identifier`/`prediction`/`store`/`metadata`）。標 `[慣例]` 的數值 range 因抓取頁截斷，依 GPT-4o 以來慣例補入。

## A.3 ⭐ GPT-5.x reasoning 特殊行為（最重要）

### `reasoning_effort`（[Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)）
- top-level string enum。`gpt-5.5` 預設 `medium`。
- 各值用途：`none`=關推理最快（極延遲敏感）｜`minimal`=極少推理（**僅部分模型如 gpt-5**）｜`low`=高效率小延遲｜`medium`=預設（涉規劃）｜`high`=困難推理/複雜 debug/深度規劃｜`xhigh`=深度研究/agentic（GPT-5.2 系及更新才有）。
- ⚠️ **各檔因模型而異**：`gpt-5.5/5.2/5.1` 有 `none`；原始 `gpt-5` 有 `minimal` 無 `none`；Codex 系常兩者皆無；`xhigh` 僅 5.2+ 才有。逐一分檔 `[需實證]`（來自社群相容矩陣，非 OpenAI 逐一背書）。

### `verbosity`（[Using GPT-5.5](https://developers.openai.com/api/docs/guides/latest-model)）
- top-level string enum `low`/`medium`/`high`，預設 `medium`。低值→更精簡，高值→更詳盡。

### 不支援的 sampling 參數（[多個 GitHub issue / OpenAI 社群](https://community.openai.com/t/gpt-5-models-temperature/1337957)）
GPT-5.x reasoning 模型**回 HTTP 400 拒絕**（非靜默忽略）：
- `temperature`（僅接受預設 1；其他值報 `unsupported_value`）、`top_p`、`frequency_penalty`、`presence_penalty`、`logprobs`、`top_logprobs`、`logit_bias`、`n`(>1)。
- **安全做法：對 reasoning 模型完全不送這些參數**，改用 `reasoning_effort` + `verbosity`。
- `[需實證]` 社群稱 `gpt-5.2-2025-12-11` 在 `reasoning_effort='none'` 時允許 temperature/top_p — 官方未述，**勿依賴**。

### reasoning token 回報（[Reasoning best practices](https://developers.openai.com/api/docs/guides/reasoning-best-practices)）
- **Chat Completions：`usage.completion_tokens_details.reasoning_tokens`**。
- （Responses API 才是 `usage.output_tokens_details.reasoning_tokens`。）
- reasoning token **計入** `completion_tokens`、**計費**、且**消耗 `max_completion_tokens` 預算**（先花在隱藏推理，再產可見輸出）→ `max_completion_tokens` 要留足餘裕。

## A.4 messages 角色

有效 role：`developer` ｜ `system` ｜ `user` ｜ `assistant` ｜ `tool` ｜ （舊）`function`

| role | 用途 | 結構 |
|---|---|---|
| `developer` ⭐ | 開發者指令（最高非平台權限層）。**自 o1 起取代 `system`** | `{role:"developer", content:"..."}` |
| `system` | 傳統系統指令 | `{role:"system", content:"..."}` |
| `user` | 使用者輸入（可含 text/image/audio parts） | `{role:"user", content:"..."}` 或 parts 陣列 |
| `assistant` | 模型回覆；可含 `tool_calls` | `{role:"assistant", content:..., tool_calls:[...]}` |
| `tool` | 工具結果回填 | `{role:"tool", tool_call_id:"...", content:"..."}` |

### ⭐ `developer` vs `system`
- 官方 reference 原文：「With o1 models and newer, **`developer` messages replace the previous `system` messages**.」兩者功能等價，**同一請求不應並用**。
- `[需實證]` **是否自動映射**官方文件矛盾：best-practices 頁稱「無自動轉換、應明確用 developer」；社群普遍觀察「送 `system` 給 GPT-5.x 仍可運作」。
- **安全結論：對 GPT-5.x 優先用 `role:"developer"`**；沿用 `system` 多半仍可，但勿並用。

## A.5 `response_format`（[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)）

| 模式 | `type` | 保證 |
|---|---|---|
| 純文字 | `text` | 無（預設） |
| JSON mode | `json_object` | 合法 JSON，**不保證符合 schema**；需 prompt 明示要 JSON |
| **Structured Outputs** | `json_schema` | **合法 JSON 且嚴格符合所供 schema**（推薦，gpt-5.x 全支援） |

`json_schema` 物件形狀（精確）：
```json
{
  "type": "json_schema",
  "json_schema": {
    "name": "math_response",
    "schema": {
      "type": "object",
      "properties": {
        "steps": { "type": "array", "items": { "type": "string" } },
        "final_answer": { "type": "string" }
      },
      "required": ["steps", "final_answer"],
      "additionalProperties": false
    },
    "strict": true
  }
}
```
規則：`strict:true` 保證嚴格符合；root `type` 必為 `"object"`；**所有欄位都要列入 `required`**（可選欄位用 `["string","null"]` union，不是省略）；**`additionalProperties:false` 必填**；部分 JSON Schema 關鍵字在 strict 下不支援。

## A.6 工具呼叫

`tools`（function 定義範例）：
```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "Get current weather",
    "parameters": { "type":"object", "properties": { "city": {"type":"string"} }, "required": ["city"], "additionalProperties": false },
    "strict": true
  }
}
```
`tool_choice` 全部值：`"none"`（不呼叫）｜`"auto"`（自行決定，有 tools 時預設）｜`"required"`（必須呼叫至少一個）｜`{type:"function", function:{name:"..."}}`（強制指定）｜`{type:"allowed_tools", ...}`（限定子集）。
`parallel_tool_calls`：boolean，預設 `true`。

## A.7 安全調用骨架（gpt-5.5）

```jsonc
{
  "model": "gpt-5.5",
  "messages": [
    { "role": "developer", "content": "系統指令放這（用 developer 不用 system）" },
    { "role": "user", "content": "…" }
  ],
  "reasoning_effort": "medium",      // none/low/medium/high/xhigh
  "verbosity": "medium",             // low/medium/high
  "max_completion_tokens": 4096,     // 要含 reasoning token 空間，留足餘裕
  "response_format": {               // 需結構化輸出時
    "type": "json_schema",
    "json_schema": { "name": "...", "schema": { /*...*/ }, "strict": true }
  }
  // ❌ 不要送：temperature / top_p / frequency_penalty / presence_penalty
  //            / logprobs / top_logprobs / logit_bias / seed / n
}
```
讀回 reasoning token：`usage.completion_tokens_details.reasoning_tokens`。

---

# B. DeepSeek（V4 系列）

> 主要來源（皆 2026-06-28 查證）：
> [Your First API Call](https://api-docs.deepseek.com/)、
> [Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)、
> [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)、
> [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)、
> [Reasoning Model](https://api-docs.deepseek.com/guides/reasoning_model)、
> [Chat Prefix Completion](https://api-docs.deepseek.com/guides/chat_prefix_completion)、
> [FIM Completion](https://api-docs.deepseek.com/guides/fim_completion)、
> [Context Caching](https://api-docs.deepseek.com/guides/kv_cache)、
> [Parameter Settings](https://api-docs.deepseek.com/quick_start/parameter_settings)。

## B.0 ⚠️ Model 名稱（先讀，這節改過兩次）

**✅ 現行結論（2026-09-25，官方 `/models` 實測）：**

| 要叫哪顆 | 正式 id | 別名 |
|---|---|---|
| 便宜、快 | **`deepseek-flash`** | `deepseek-v4-flash`（catalog 會自動映射回正式 id） |
| 強 | **`deepseek-v4-pro`** | — |

- **新的呼叫一律寫 `deepseek-flash`**；`deepseek-v4-flash` 仍然可以送（omniapi 的 catalog 認得它是別名，會解析成 `deepseek-flash`），但回傳的 `model` 欄位會是正式 id。
- 更省事的寫法是用等級別名 **`cheap`**（§0.5），它就指向 `deepseek-flash`。

**🕘 歷史脈絡（別重蹈覆轍）：**
- **2026-04-24**：DeepSeek 發布 V4 系列。當時官方 `/models` 回的是 `deepseek-v4-pro` 與 `deepseek-v4-flash`，所以 **2026-06-28 版的本檔結論是「一律用 `deepseek-v4-flash`」**——那在當時是對的。
- **2026-07-24 15:59 UTC**：舊別名 `deepseek-chat` / `deepseek-reasoner` 依官方公告棄用。它們曾對應 `deepseek-v4-flash` 的 non-thinking（chat）/ thinking（reasoner）。**現在別用這兩個。**
- **2026-09-25**：再查官方 `/models` 與定價頁，flash 那顆的正式 id 已經是 **`deepseek-flash`**（定價頁以 `deepseek-flash` 與 `deepseek-v4-pro` 並列並標 `"Anthropic API: ✓ supported"`），`deepseek-v4-flash` 降為別名。→ 本節結論隨之更新。

> ⚠️ 已知不一致：`config/settings.py` 裡 `DeepSeekSettings.default_model` 仍寫 `"deepseek-v4-flash"`（別名），而 `capabilities/text.py` 的 `DeepSeekTextProvider.DEFAULT_MODEL` 已是 `"deepseek-flash"`。兩者指向同一顆模型，但字串不一致——**明寫 model 就不會踩到**。

> 🔗 DeepSeek 另有 **Anthropic Messages 相容端點** `https://api.deepseek.com/anthropic`（兩顆模型都支援），給 `ANTHROPIC_BASE_URL` 式的 harness 用；omniapi 的 `complete_text` 走的是 OpenAI 相容那條。

## B.1 Base URL / 端點 / SDK 相容

| 項目 | 值 |
|---|---|
| Standard base_url | `https://api.deepseek.com`（或 `…/v1`，純為相容 SDK，與 OpenAI 模型無關） |
| **Beta base_url** | `https://api.deepseek.com/beta`（FIM、Prefix Completion 必須用此） |
| Anthropic 相容 | `https://api.deepseek.com/anthropic` |
| Chat 端點 | `POST /chat/completions` |
| Models 端點 | `GET /models` |
| FIM 端點 | `POST /completions`（需 beta base_url） |
| OpenAI SDK 相容 | ✅ 覆寫 `base_url` 即可；DeepSeek 專屬參數（如 `thinking`）走 `extra_body` |

```python
from openai import OpenAI
client = OpenAI(api_key="<DEEPSEEK_API_KEY>", base_url="https://api.deepseek.com")
resp = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[{"role": "user", "content": "Hello"}],
)
```

## B.2 完整 Chat Completions 參數表

> 來源：[Create Chat Completion](https://api-docs.deepseek.com/api/create-chat-completion)，2026-06-28

| 參數 | 型別 | 必填 | 允許值 / 範圍 | 預設 | 說明 |
|---|---|---|---|---|---|
| `model` | string | ✅ | `deepseek-flash` / `deepseek-v4-pro`（`deepseek-v4-flash` 為別名；`deepseek-chat`/`deepseek-reasoner` 已於 2026/07/24 棄用） | — | model ID |
| `messages` | object[] | ✅ | role：`system`/`user`/`assistant`/`tool` | — | 對話訊息陣列 |
| `max_tokens` | integer | ✗ | 見 B.4 上限 | reasoner 線預設 32K | 單次最大生成 token（thinking 下含 CoT） |
| `temperature` | number | ✗ | 0–2 | 1 | 取樣溫度；⚠️ thinking 模式**靜默忽略**（B.3） |
| `top_p` | number | ✗ | 0–1 | 1 | Nucleus sampling；⚠️ thinking 模式**靜默忽略** |
| `stop` | string / string[] | ✗ | 最多 16 組 | null | 命中即停止 |
| `stream` | boolean | ✗ | true/false | false | SSE 串流 |
| `stream_options` | object | ✗ | `{"include_usage": true}` | null | 串流選項；最後補一筆 usage |
| `response_format` | object | ✗ | `{"type":"text"}` / `{"type":"json_object"}` | `{"type":"text"}` | JSON Output（**無 `json_schema`**） |
| `tools` | object[] | ✗ | 最多 **128** 個 function | null | 可呼叫工具定義 |
| `tool_choice` | string / object | ✗ | `none` / `auto` / `required` / 指定 function | — | 控制工具呼叫 |
| `logprobs` | boolean | ✗ | true/false | false | 回傳 token log 機率；⚠️ reasoner 線**報錯** |
| `top_logprobs` | integer | ✗ | 0–20 | — | 每位置前 N（需開 `logprobs`）；⚠️ reasoner 線**報錯** |
| `thinking` | object | ✗ | `{"type":"enabled"}` / `{"type":"disabled"}` | `enabled` | **DeepSeek 專屬**：開關 thinking（OpenAI SDK 放 `extra_body`） |
| `reasoning_effort` | string | ✗ | `high` / `max`（`low`/`medium`→等同 `high`；`xhigh`→等同 `max`） | `high`（一般）；agent 場景自動 `max` | **DeepSeek 專屬**：推理深度 |
| `user_id` | string | ✗ | `[a-zA-Z0-9\-_]`、≤512 字 | — | 自訂使用者識別碼 |

### ⚠️ V4 已移除的參數
- **`frequency_penalty`** 與 **`presence_penalty`** → 官方標「**no longer supported**」（V3 還能用、V4 已移除）。**別再傳**。

### `response_format` 備註
- 官方只列 `text` 與 `json_object`；**`json_schema`（OpenAI 嚴格 schema）未出現 → 視為不支援**。要結構化輸出用 `json_object` + 在 prompt 內描述 JSON 格式並含「json」字樣。

## B.3 ⭐ Thinking / Reasoning 模式與 `reasoning_content`（最重要）

### 開關方式（V4 = 參數控制，不再換 model）
```python
# 開（預設）
extra_body={"thinking": {"type": "enabled"}}
# 關
extra_body={"thinking": {"type": "disabled"}}
# 深度
reasoning_effort="high"   # 或 "max"
```
- `deepseek-v4-pro` / `deepseek-flash` 兩者都能開關；預設 `enabled`。
- Anthropic 相容介面改用 `output_config: {"effort": "high/max"}`。

### `reasoning_content` 欄位
`reasoning_content`（CoT）與 `content`（最終答案）**同層**，都在 `choices[].message` 下：
```python
msg = resp.choices[0].message
reasoning = msg.reasoning_content   # 思考過程
answer    = msg.content             # 最終回答
```
串流時兩者在不同 delta chunk 分別到達，需各自累積。

### ⭐ 多輪對話的 `reasoning_content` 處理（最容易踩雷）
**(A) V4 thinking（`deepseek-v4-*`）— 有 tool-call 例外：**
- 沒有 tool call 的回合：上一輪 `reasoning_content` **不需併回**；就算傳回 API 也**自動忽略**（不報錯）。
- **有 tool call 的回合**：`reasoning_content` **必須完整傳回**後續所有請求，否則 API **回 400**。

**(B) 舊 `deepseek-reasoner` 介面 — 一律不能傳回：**
- 官方原文：input messages 含 `reasoning_content` → **回 400**。任何情況都不能傳回（舊 reasoner 不支援 function calling，故無 tool-call 例外）。

> **實作建議**：用 V4 介面時，**預設把歷史 `reasoning_content` 剝除**；只有正在 tool-call 串接的回合才保留 → 兩種介面都安全。

### thinking / reasoner 不支援的參數
| 參數 | V4 thinking | 舊 deepseek-reasoner |
|---|---|---|
| `temperature` / `top_p` / `presence_penalty` / `frequency_penalty` | 不支援，**靜默忽略**（不報錯、無效果） | 不支援，靜默忽略 |
| `logprobs` / `top_logprobs` | V4 參數頁列為通用，但 thinking 下行為未明文保證 `[需實證]` | **會報錯** |

官方原文（thinking）：「setting these parameters will not trigger an error but will also have no effect.」
官方原文（reasoner）：`logprobs`/`top_logprobs`「will trigger an error」。

### 最大輸出 / context
| 項目 | V4（pro / flash） | 舊 deepseek-reasoner |
|---|---|---|
| Max output（含 CoT） | **384K** | 預設 32K、最大 64K |
| Context length | **1M** | 64K |

## B.4 ⭐ Beta 功能（base_url 改 `https://api.deepseek.com/beta`）

### Chat Prefix Completion（強制續寫）
最後一筆必須 `role:"assistant"` 且帶 `"prefix": True`，模型接著往下寫：
```python
client = OpenAI(api_key="<key>", base_url="https://api.deepseek.com/beta")
messages = [
    {"role": "user", "content": "Please write quick sort code"},
    {"role": "assistant", "content": "```python\n", "prefix": True},
]
resp = client.chat.completions.create(model="deepseek-v4-pro", messages=messages, stop=["```"])
```

### FIM（Fill-in-the-Middle）
- 端點 `POST /completions`（非 chat），需 beta base_url。
- 參數：`model`、`prompt`（前段）、`suffix`（後段，optional）、`max_tokens`。
- **Max tokens 上限 4K**；**只在 non-thinking 模式可用**。
```python
client = OpenAI(api_key="<key>", base_url="https://api.deepseek.com/beta")
resp = client.completions.create(model="deepseek-v4-pro", prompt="def fib(a):", suffix="    return fib(a-1) + fib(a-2)", max_tokens=128)
print(resp.choices[0].text)
```

### Context Caching（自動）
- **全自動、零程式改動**（預設對所有人開啟）。
- 命中回報在 `usage`：`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
- 命中需**完整前綴吻合**；best-effort，快取數小時～數天自動清除；命中部分以 cache-hit 價計（見 B.5）。

## B.5 ⭐ 定價（每 1M tokens，USD）

> 來源：[Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing)，**2026-09-25**

| Model | Input (Cache **Hit**) | Input (Cache **Miss**) | Output |
|---|---|---|---|
| **deepseek-flash** | **$0.007** | **$0.22** | **$0.66** |
| **deepseek-v4-pro** | 官方未列靜態價（catalog 留白） | — | — |

> ⏰ **離峰／尖峰是真的**（2026-09-25 起）：上表是**離峰價**——台北時間平日 12–14 時、18 時之後，以及整個週末。**尖峰時段是兩倍。** 這與 2026-06-28 版本的結論相反（當時官方頁查無任何時段定價，本檔曾寫「別假設有折扣」），該結論**已過時**，以本節為準。
> ⚠️ `deepseek-v4-pro` 的價目 catalog 是空的——**不要沿用 2026-06-28 的舊數字**（$0.003625/$0.435/$0.87，那是 V4 剛發布時的表）。要算成本請臨場查官方頁。
> ⚠️ 舊 URL `pricing-details-usd` 仍顯示更早的 V3/R1 定價，**不要採用**，以 `quick_start/pricing` 為準。
> 💡 omniapi 會在回傳的 `cost_usd` 幫你算好（catalog 有價才算得出來）。

**其他規格**（2026-06-28 查證，尚未重新核對）：context 1M、max output 384K；並發限制 flash 2500 / pro 500。兩款皆支援 JSON Output / Tool Calls / Prefix Completion；FIM 僅 non-thinking。

## B.6 溫度建議（每情境，[Parameter Settings](https://api-docs.deepseek.com/quick_start/parameter_settings)）

| 情境 | 建議 `temperature` |
|---|---|
| Coding / Math | **0.0** |
| Data Cleaning / Analysis | **1.0** |
| General Conversation | **1.3** |
| Translation | **1.3** |
| Creative Writing / Poetry | **1.5** |

- 預設 1.0。⚠️ 此表**只適用 non-thinking**（thinking 模式 temperature 無效，B.3）。
- `[需實證]` 「API 內部是否對 temperature 做 0.3× 映射」：現行 parameter_settings 頁未提及（V3 舊行為，V4 視為不適用，勿當事實寫入）。

---

# C. 對 agent 的 TL;DR 行動清單

**先於一切：**
0. **能用等級別名就用別名** — `cheap`（省錢）/ `standard`（一般）/ `strong`（最難）。要看現在有哪些模型，跑 `list_available_models(modality="text")`，別憑本檔的表猜。

**OpenAI GPT-6 / GPT-5.x：**
1. Model 用 `gpt-6-sol`（＝`strong`）/ `gpt-6-astra`（最強）/ `gpt-5.4-mini`（性價比）。
2. 用 `max_completion_tokens`（**非** `max_tokens`），含 reasoning token 要留餘裕。
3. 系統指令用 `role:"developer"`（**非** `system`），勿並用。
4. **不送** temperature/top_p/penalties/logprobs/logit_bias/seed/n（會 400）；調控用 `reasoning_effort` + `verbosity`。
5. 結構化輸出 `response_format.type=json_schema` + `strict:true` + `additionalProperties:false` + 全欄位 `required`。
6. reasoning token 看 `usage.completion_tokens_details.reasoning_tokens`。

**DeepSeek V4：**
1. Model 用 `deepseek-v4-pro`（強）/ **`deepseek-flash`**（便宜，正式 id；`deepseek-v4-flash` 只是別名）。別用 `deepseek-chat`/`deepseek-reasoner`（2026/07/24 停用）。
2. 思考開關 `extra_body={"thinking":{"type":"enabled"|"disabled"}}`，深度 `reasoning_effort:"high"|"max"`。
3. 思考時不送 temperature/top_p/penalties（靜默無效）；舊 reasoner 還會因 logprobs 報錯。
4. `frequency_penalty`/`presence_penalty` 在 V4 已移除，別傳。
5. 多輪 + 思考：歷史 `reasoning_content` 預設剝除；只有 tool-call 串接的回合原樣傳回（否則 V4 400；舊 reasoner 任何傳回都 400）。
6. Beta（FIM/Prefix）→ base_url 改 `/beta`；FIM 僅 non-thinking、上限 4K。
7. 快取自動，看 `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。
8. 定價（2026-09-25）：`deepseek-flash` **離峰** $0.007/$0.22/$0.66（hit/miss/output，每 1M USD），**尖峰兩倍**；`deepseek-v4-pro` 官方未列靜態價，要算成本臨場查。

---

# D. 無法 100% 官方確認 / 文件模糊處（誠實標示）

**OpenAI：**
- 數值預設與 range（temperature 0–2、penalty ±2、top_logprobs ≤20）：reference 頁抓取截斷，依慣例補入，標 `[慣例]`；`platform.openai.com/docs/api-reference/*` 全程 403。
- `reasoning_effort` 各模型逐一分檔（none/minimal/xhigh 哪些模型有）：官方僅明列 gpt-5.5；**GPT-6 三顆完全未查證**，本檔沿用 5.5 的檔位屬推論 → `[需實證]`。
- `gpt-6-luna` 定價：官方未列靜態價 → catalog 留白，本檔不編數字。
- `system` 是否自動映射為 `developer`：官方文件矛盾 → 安全用 `developer`。
- `reasoning_effort='none'` 是否解鎖 temperature/top_p：僅社群來源，勿依賴。
- `gpt-5.5-chat-latest` / `gpt-5.5-pro` 是否支援 Chat Completions：需 `GET /v1/models` 實證（`-pro` 系常僅 Responses API）。

**DeepSeek：**
- 🕘 *（2026-06-28 曾判定「V4 目前無 off-peak 折扣」——2026-09-25 查證推翻，現行確有離峰／尖峰兩段價，見 B.5。留此條記錄判斷曾經翻轉。）*
- `deepseek-v4-pro` 現行定價：catalog 留白，本次未取得官方靜態數字 → 要精算成本得臨場查。
- `logprobs`/`top_logprobs` 在 V4 thinking 下行為：參數頁列為通用，thinking 頁未明文保證 → 保守別傳。
- `json_schema`：官方參數頁未出現 → 判定不支援。
- 內部 temperature 0.3× 映射：現行頁未提 → 視為無（V3 舊行為不沿用）。
