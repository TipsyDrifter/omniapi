# OmniAPI MCP Server

> **This file is the MCP tool reference.** OmniAPI is more than the MCP server: a local daemon with a web GUI (live board of agent runs, dispatch, chat, generation, works wall, cost ledgers, model catalog, settings) and the `omni` CLI, installable as a Windows desktop app or from a zip. For what it is, how to install it and how to use the GUI, start with the [root README](../README.md) and the [user guide](../USER_GUIDE.md).

**One MCP server for image, audio transcription, text/chat, speech, music, video and agent dispatch — across OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, ElevenLabs, and Suno.**

Traditional AI chatbot interfaces are limited to text-only interactions, regardless of how powerful their underlying language models are. OmniAPI MCP Server bridges this gap by giving **any LLM-powered chatbot client** one unified interface for image generation & editing, audio transcription, text/chat completion, speech synthesis, music generation and short video generation — through the standardized Model Context Protocol (MCP).

Whether you're using Claude Desktop, a custom ChatGPT interface, Llama-based applications, or any other LLM client that supports MCP, this server democratizes access to **multiple AI models across six modalities** — OpenAI's gpt-image family, Google's Nano Banana and OpenRouter's image models (FLUX, Seedream, Grok Imagine, Qwen…) for pictures, GPT-6 / Claude / Gemini / DeepSeek for text, OpenRouter's video models for short clips, and more — transforming text-only conversations into rich, multimodal experiences.

> **📦 Package Manager**: This project uses [UV](https://docs.astral.sh/uv/) for fast, reliable Python package management. UV provides better dependency resolution, faster installs, and proper environment isolation compared to traditional pip/venv workflows.

## Capabilities

OmniAPI unifies **six modalities** behind one MCP interface (**20 tools total**):

| Capability | Tools | Providers |
|---|---|---|
| 🎨 Image generation & editing | `generate_image`, `edit_image` | OpenAI gpt-image (2.5 / 2 / 1.5 / 1-mini), Google Nano Banana, OpenRouter image models (FLUX, Seedream, Grok Imagine, Qwen, Recraft, MAI…) |
| 🎙️ Audio transcription | `transcribe_audio` | OpenAI `gpt-transcribe` (+ subtitles / timestamps / diarization on the deprecated whisper-1 & gpt-4o-transcribe family) |
| 💬 Text / chat completion | `complete_text`, `chat` (multi-turn, kept in the GUI) | OpenAI GPT-6 & GPT-5.x, Anthropic Claude, Google Gemini, DeepSeek, OpenRouter long tail |
| 🔊 Speech synthesis (TTS) | `generate_speech` | OpenAI, ElevenLabs, Gemini TTS |
| 🎵 Music generation | `generate_music`, `edit_music`, `music_lyrics`, `music_utility`, `compose_music` | Suno V6 (via kie.ai), ElevenLabs Music |
| 🎬 Video generation | `generate_video` (text-to-video, first / last frame) | OpenRouter video models (Wan, Kling, Veo, Grok Imagine, Seedance…); Google Gemini Omni direct with a Google key |

Plus diagnostics and job retrieval: `list_available_models`, `health_check`, `server_info`, `get_job_result`; and agent runs: `run_agent`, `get_run`, `list_runs`, `cancel_run`.

> The four music tools other than `generate_music` are **action-dispatched**: one tool covers several operations, selected by an `action` argument (e.g. `edit_music(action="extend" | "cover" | "separate_vocals" | …)`). See [Available Tools](#available-tools).

> A single **OpenAI** key unlocks image / transcription / text / speech. **Music** needs a **kie.ai** key (Suno) or a paid **ElevenLabs** plan. **Video** needs an **OpenRouter** key (the same key also adds OpenRouter's image models and text long tail). **Anthropic**, **Google** and **DeepSeek** are optional and add text models.

> **Model rosters are live.** Each provider is asked what is online at startup, merged with a curated pricing/capability overlay. Call `list_available_models(modality="text" | "image" | "transcription" | "speech" | "music" | "video")` for the current list — including deprecation and shutdown dates — rather than trusting any hard-coded list. `complete_text` also accepts the tier aliases **`cheap`** / **`standard`** / **`strong`** so callers need not pin a model id.

> **Keys, tiers and defaults can be changed on the GUI's settings page** (`http://127.0.0.1:7788/settings`) and take effect at once, without restarting the daemon or reconnecting MCP clients. What a tier alias points at right now is the `tiers` field of `list_available_models`; the values below are the shipped defaults.

## Available Tools

20 tools across six modalities plus diagnostics, agent runs and chat.

### Diagnostics & jobs

#### `list_available_models`
List every model OmniAPI can call, **across all modalities**, with provider, online status, pricing, deprecation/shutdown info and the tier aliases (`cheap` / `standard` / `strong`). Rosters are live — providers are asked what is online at startup and merged with a curated overlay.

**Parameters**:
- `modality`: `"text"` | `"image"` | `"transcription"` | `"speech"` | `"music"` | `"video"` — omit for everything. OpenRouter image models carry `image_params` (aspect ratios, resolutions, qualities, `max_references`, seed); video models carry `video_params` (durations, resolutions, aspect ratios, frames, audio, seed; Gemini Omni also `audio_fixed` and `last_frame_needs_first`)
- `include_retired` (default `false`): also list models the provider has shut down
- `include_snapshots` (default `false`): also list dated snapshot ids (e.g. `gpt-5.5-2026-04-23`); hidden by default but still callable
- `refresh` (default `false`): force a fresh discovery round instead of the 24h cache

A model that is on an Artificial Analysis leaderboard of its modality also carries `rank` (its best rank there; lower is more popular), `popularity` (`{board: rank}`) and `rank_badge` (the board, label, rank and score the GUI shows as "AA #n"; left out when the id mapping is only a guess). The ranks are a hand-taken snapshot shipped with the release (2026-10-06), not fetched live; a model on no board has none of the three.

**Returns** `models` (by modality), `counts`, `tiers` (the current tier mapping, including any override from the settings page), `configured_providers`, `default_text_model`, `discovery`, and per provider `providers.<name>` with `label`, `harness`, `modalities`, `get_key` (where to get a key) and `suggested_tiers` (that provider's cheap / standard / strong suggestion; none for OpenRouter).

#### `health_check`
Overall server health plus a per-provider ping.

#### `server_info`
Server metadata: version, capabilities, and non-sensitive configuration.

#### `get_job_result`
Fetch the result of a long-running generation that returned a ticket (`{status: "running", task_id}`). If it is still `running`, wait ~20–30s (a video: a minute or two) and call again. A video's ticket (`video_<id>`) is stored with the job, so it still works after the OmniAPI service restarts.

**Parameters**: `task_id` (required).

### Image

#### `generate_image`
Generate images from text descriptions using any supported model.

**Parameters**:
- `prompt` (required): Text description of desired image (1–4000 chars)
- `model` (optional): e.g. `"gpt-image-2.5-sunburst"`, `"gpt-image-2"`, `"gpt-image-1.5"`, `"nano-banana-2"`, or an OpenRouter image model by its OpenRouter id (e.g. `"black-forest-labs/flux-3-image"`). Defaults to `IMAGES__DEFAULT_MODEL`
- `quality`: `"auto"` | `"high"` | `"medium"` | `"low"` (default `"auto"`) — the main cost lever
- `size`: `"auto"`, presets like `1024x1024` / `1536x1024` / `1024x1536` / `3840x2160`, or (for the `gpt-image-2` family) any `WxH` within the model's constraints
- `output_format`: `"png"` | `"jpeg"` | `"webp"` (default `"png"`); `compression` 0–100 for jpeg/webp
- `background`: `"auto"` | `"transparent"` | `"opaque"` — **the gpt-image-2 family does not support transparency** and silently downgrades to `auto`
- `moderation`, `style`, `user`: OpenAI only
- `n`: 1–10 candidates (gpt-image only; Gemini is always 1). `n > 1` adds an `images` list
- **Gemini**: `image_size` (`"512"` / `"1K"` / `"2K"` / `"4K"`, uppercase K), `aspect_ratio` (e.g. `"16:9"`, `"21:9"`, `"4:5"`), `person_generation` (Gemini only)
- **OpenRouter models**: `image_size` (one of the model's `image_params.resolutions`, e.g. `"768"`, `"1.5K"`), `aspect_ratio` (one of its `image_params.aspect_ratios`), `quality` (its `image_params.qualities`), `seed` (when `image_params.seed` is true)
- **Legacy, ignored by every current model**: `safety_filter_level`, `enhance_prompt`, `guidance_scale` — kept only so older callers do not break

**Note**: Parameter availability depends on the selected model. Use `list_available_models(modality="image")` to check capabilities. OpenRouter's image models are listed live (refreshed daily) once an OpenRouter key is set; when the OpenAI or Google key is set, OpenRouter's copies of those vendors' models are not listed. Models that answer with SVG only (Recraft's vector models) are marked unavailable in this version. The reported cost is what OpenRouter charged.

#### `edit_image`
Edit existing images with text instructions.

**Parameters**:
- `image_data` **or** `image_path` (one required): base64 / data URL, or a local filesystem path (preferred on STDIO transport)
- `prompt` (required): Edit instructions
- `mask_data`: Optional mask for targeted editing (applies to the first image only)
- `model`, `size`, `quality`, `output_format`, `compression`, `background`: same as `generate_image`
- `input_fidelity`: `"high"` keeps faces/style close to the source — **gpt-image-1 family only**, ignored by gpt-image-2
- `additional_images` / `additional_image_paths`: extra reference images (up to 16 total)
- `image_size`, `aspect_ratio`: OpenRouter models only, from the model's `image_params`

> **`edit_image` routes by model**: `gpt-image-*` → OpenAI (masks supported); Nano Banana → Gemini (references via `additional_images`, no mask); an OpenRouter id → OpenRouter, for models whose `image_params.max_references` is above 0 (the source and `additional_images` go as reference images, no mask). The default model comes from `IMAGES__DEFAULT_MODEL` — pass `model` explicitly if you care which one runs.

### Transcription

#### `transcribe_audio`
Transcribe an audio file to text via the OpenAI Audio API.

**Parameters**: `audio_path` (preferred) or `audio_data`; `model` (default `gpt-transcribe`), `language`, `prompt`, `response_format`, `temperature`, `timestamp_granularities`, `chunking_strategy`, `include`, `known_speaker_names`, `known_speaker_references`.

> Subtitles (`srt` / `vtt`), `verbose_json` segment timings and word-level timestamps are documented for **`whisper-1`** only, and speaker labels (`diarized_json`) for **`gpt-4o-transcribe-diarize`** only. Both — along with `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` — are **deprecated and shut down on 2027-02-26**. They remain callable and are still the only route to those features.

### Text

#### `complete_text`
Generate a text/chat completion with any configured text model: OpenAI (GPT-6 / GPT-5.x), Anthropic (Claude), Google (Gemini), DeepSeek, or the OpenRouter long tail.

**Parameters**: `prompt` (or a full `messages` list), `model`, `system`, `reasoning_effort`, `verbosity`, `max_completion_tokens`, `response_format`, `thinking` (DeepSeek), `tools` / `tool_choice` / `parallel_tool_calls`, sampling (`temperature`, `top_p`, `seed`, `stop`), and OpenAI extras (`store`, `metadata`, `service_tier`, `prompt_cache_key`, `safety_identifier`).

- `model` accepts a **tier alias** — `cheap` (`deepseek-flash`), `standard` (`gemini-3.8-flash`), `strong` (`gpt-6-sol`), as shipped; the settings page can repoint them — an alias, or a provider model id. Without one, the chat default from the settings page is used (shipped as `cheap`). Namespaced ids (`vendor/model`) route to OpenRouter.
- Sampling parameters are **dropped automatically** for GPT-5.x/6 reasoning models, which reject them with HTTP 400; `system` is remapped to a `developer` message; `max_completion_tokens` becomes `max_tokens` for non-OpenAI dialects.

#### `chat`
Multi-turn chat with any text model. The conversation is stored and appears in the GUI under `/chat`, where it can be read or continued. Use `complete_text` for a one-off completion that should leave no conversation behind, and `run_agent` when the model has to edit files or run commands.

**Parameters**: `message` (required), `conversation_id` (continue a conversation — pass back the id the previous call returned; leave empty to start a new one), `model` (tier alias or model id; a new conversation defaults to the settings page's chat default, shipped as `cheap`; an existing one keeps its last model unless you give another), `system` (sets the system prompt of a new conversation, replaces it on an existing one), `title`, `reasoning_effort`, `temperature`, `max_completion_tokens`.

**Returns**: `{conversation_id, title, state, text, model, requested_model, reasoning?, usage, cost_usd, error?, n_messages, url}` — `url` is the GUI page of the conversation. A reply that takes longer than ~45s comes back as a `get_job_result` ticket that also carries the `conversation_id`. Errors come back as `{error, status}` (404 unknown conversation, 400 unknown model, 409 a reply is still running). The spend is recorded in the chat-and-generation ledger under the tool name `chat`.

### Command line: `omni chat`

The daemon's CLI talks to the same conversations (they show up in the GUI too):

```bash
omni chat "Summarise RFC 9110 in five bullets"           # new conversation, reply printed as it streams
omni chat "And the caching part?" -r <conversation_id>   # continue a conversation
omni chat -m standard -s "You are a strict reviewer"     # no message: interactive session
omni chat "…" --json                                      # final JSON only (for scripts)
```

- The reply goes to stdout; a summary line (model, tokens, cost, conversation id, GUI link) goes to stderr.
- Options: `--model/-m` (default `cheap`; `--resume` keeps the conversation's model), `--system/-s`, `--resume/-r <conversation_id>`, `--no-stream` (wait for the whole reply), `--json` (implies `--no-stream`), `--show-thinking` (reasoning, dimmed, on stderr), `--host`, `--port`.
- Interactive commands: `/model <name>`, `/system <text>`, `/export [path]` (markdown, the daemon's file name in the current directory by default), `/id`, `/exit` (or Ctrl+D). Ctrl+C while a reply is coming cancels that reply; Ctrl+C at the prompt leaves.
- Streaming uses the daemon's `/ws`; if the socket cannot be opened the CLI says so once and waits for whole replies instead. The daemon must be running (`omni serve`).

### Agent runs

Hand a task to a headless coding agent. The vendor's own harness is used, chosen by the model's provider: Claude Code for Anthropic-compatible models (Claude, DeepSeek, OpenRouter), Codex CLI for OpenAI, Gemini CLI for Google. The matching CLI must be installed and logged in. Runs appear on the GUI board and under `/runs/<run_id>`, where they can also be followed up or cancelled.

> An agent run edits files and runs commands in the working directory you give it, and it costs money. Only dispatch when the user asked for it.

#### `run_agent`
Start a run. Returns a `run_id` immediately; the agent keeps working in the background.

**Parameters**:
- `task` (required): the brief — goal, files, constraints, what to report back
- `model` (default `cheap`): tier alias (`cheap` / `standard` / `strong`) or a model id
- `cwd`: absolute working directory
- `title`: short title for the board (defaults to the first 40 characters of the task)
- `yolo` (default `false`): skip every permission prompt. The default lets the agent edit files and use Bash / web fetch / search without asking
- `search` (default `true`): attach web-search MCP servers (Claude Code harness only)
- `harness`: force `claude` / `codex` / `gemini` instead of routing by model
- `max_turns`: cap the number of agentic turns
- `resume_run_id`: continue an earlier run's session; `task` becomes the follow-up
- `dispatcher`: who dispatched it (shown on the board)
- `billing`: Claude models only — `subscription` (default: the Claude Code login, no per-token charge) or `api` (Anthropic API key)

#### `get_run`
State (`starting` / `running` / `done` / `error` / `cancelled` / `dead`), turns, cost, the final `result`, and optionally the events.

**Parameters**: `run_id` (required), `after_event` (only events with a larger id — pass the last id you saw to get the increment), `include_events`, `max_events`.

#### `list_runs`
Recent runs with state, model, turns and cost. **Parameters**: `limit`, `state`.

#### `cancel_run`
Stop a running run. **Parameters**: `run_id` (required).

The same from a terminal: `omni run "<task>" --model cheap --cwd <dir>`, `omni run "<follow-up>" --resume <run_id>`, `omni runs`, `omni run-log <run_id>`.

### Speech

#### `generate_speech`
Synthesize speech from text using ElevenLabs, OpenAI or Gemini TTS; saves an audio file and returns its path.

**Parameters**: `text`, `voice`, `model`, `output_format`, `instructions` (gpt-4o-mini-tts only), `speed` (tts-1 / tts-1-hd only), and the ElevenLabs set (`voice_settings`, `language_code`, `seed`, `previous_text`, `next_text`, `apply_text_normalization`, `enable_logging`).

> Gemini TTS only emits PCM, so it returns WAV (or raw PCM for a `pcm_*` format) whatever you request.

### Music

#### `generate_music`
Generate music from a text description. ElevenLabs Music (`music_v1` / `music_v2` / `music_v2_5`, synchronous) or Suno via kie.ai (`V6` / `V6_MINI` / `V6_WILD`, supports vocals and custom mode).

**Parameters**: `prompt`, `model`, `instrumental`, `output_format` / `music_length_ms` (ElevenLabs), `custom_mode` + `style` + `title` + `vocal_gender` (Suno), `negative_tags`.

**Returns** `audio_path`, `audio_url`, `provider`, `model`, `title`, `duration`, `bytes`; Suno also `audio_id` / `task_id` for chaining. **A Suno job makes two songs** (so do `edit_music`'s extend / cover / upload_extend / add_instrumental / add_vocals): both are saved (the second as `<first file>_2`), the top-level fields describe the first, `tracks` lists every song (`audio_path`, `audio_id`, `title`, `duration`, `bytes`; a song that failed to download has `audio_id` + `error`) and `track_count` says how many. Each song is its own work on the works wall, the job's cost split between them. `music.suno_all_tracks: false` in `settings.json` (env `MUSIC__SUNO_ALL_TRACKS=false`) keeps only the first.

#### `edit_music(action)`
Transform existing audio with Suno. Each `action` needs a different subset of parameters:

| `action` | Required | Plus, when `custom_mode=true` |
|---|---|---|
| `extend` | `audio_id` | `prompt`, `style`, `title`, `continue_at` |
| `cover` | `upload_url`, `prompt` | `style`, `title` |
| `upload_extend` | `upload_url` | `prompt`, `style`, `title`, `continue_at` |
| `add_instrumental` | `upload_url`, `title`, `tags`, `negative_tags` | — |
| `add_vocals` | `upload_url`, `prompt`, `title`, `style`, `negative_tags` | — |
| `separate_vocals` | `task_id`, `audio_id`, `separation_type` | — |

`upload_url` must be a publicly reachable audio URL of at most 8 minutes. `add_instrumental` and `add_vocals` accept only the V6 family.

#### `music_lyrics(action)`
- `generate` — write lyrics from a theme/mood/style. Needs `prompt` (≤200 chars). Returns the text and saves a `.txt`.
- `timestamped` — word-level timings plus waveform data for a track you already generated. Needs `task_id` + `audio_id`. Synchronous.

#### `music_utility(action)`
Both actions need `task_id` + `audio_id` from a prior generation.
- `convert_to_wav` — high-quality WAV file.
- `create_music_video` — MP4; optional `author` (cover signature) and `domain_name` (bottom watermark), ≤50 chars each.

#### `compose_music(action)`
ElevenLabs composition-plan workflow — section-level control over a song (paid plan).
- `create_plan` — turn a prompt into an editable plan (sections, styles, lyrics, durations), returned as JSON. Needs `prompt`; optional `music_length_ms`, `source_composition_plan`.
- `compose` — render audio plus the plan used and song metadata. Needs **exactly one** of `prompt` or `composition_plan`.

### Video

#### `generate_video`
Generate a short video from a text prompt, optionally starting from a first frame (and, for models that take one, ending on a last frame). Video models go through OpenRouter, and Google's Gemini Omni (`gemini-omni-1.1-flash`) is called directly with the Google key (its project needs a paid Gemini API tier). **A video is billed the moment it is sent and the provider cannot cancel it.**

**Parameters**:
- `prompt` (required): what happens — subject, motion, camera, light, sound (1–4000 chars)
- `model`: an OpenRouter video model id (e.g. `"alibaba/wan-3.0"`, `"kwaivgi/kling-v3.0-std"`, `"x-ai/grok-imagine-video"`) or `"gemini-omni-1.1-flash"`. Default: the settings page's video default (shipped as `alibaba/wan-3.0`)
- `duration`: seconds, one of the model's `video_params.durations` (default 5, or the length it takes closest to 5)
- `resolution`: one of `video_params.resolutions` (e.g. `"480p"`, `"720p"`, `"1080p"`); omitted, the model's own default is used and the estimate becomes a range
- `aspect_ratio`: one of `video_params.aspect_ratios`
- `generate_audio`: sound on or off, for models whose `video_params.audio` is not false (on by default for models that make sound)
- `seed`: for models whose `video_params.seed` is true
- `first_frame` / `last_frame`: an image work's id, a local image path or a data URL; only for models whose `video_params.frames` lists `first_frame` / `last_frame`
- `max_cost_usd`: allow this video up to this many US dollars (estimated)

**Per-video limit.** A video whose estimate is above the per-video limit (default **$1**; `video.mcp_max_usd` on the settings page, or no limit at all) is **not sent**: the call returns `{status: "refused", reason: "over_limit", estimate_usd, limit_usd, message}`, and the message says which `max_cost_usd` would allow it. A model whose price cannot be computed up front (priced per token, e.g. Seedance) is refused the same way (`reason: "price_unknown"`) unless the call passes `max_cost_usd`. The GUI's generate page asks for confirmation on every video instead and is not bound by this limit.

**Waiting.** The call waits ~45 s, then returns a ticket (`status: "running"`, `task_id: "video_<id>"`) for `get_job_result`. The remote job id is stored when the video is sent, so a service restart does not lose it: the job is picked up again and the ticket still works. After `video.max_wait_minutes` (default 20) the job is reported as `waited_too_long`; it can be asked about again from the GUI without resending or paying again.

**Returns** (completed): `status`, `generation_id`, `artifact_id`, `file_path`, `video_url` (file://), `duration_s`, `width`, `height`, `has_audio` (read from the file), `fps`, `cost_usd` (what the provider charged — OpenRouter sometimes charges less than its listed price), `estimate`, `waited_s`. The video lands on the works wall.

**Gemini Omni** (direct, Google key): always makes sound (`generate_audio=false` is refused), 3–10 s, `360p` / `720p` (default) / `1080p`, `16:9` / `9:16`; a `last_frame` needs a `first_frame`. 720p is estimated at about $0.10 per second; other resolutions cannot be priced up front (`price_unknown`, needs `max_cost_usd`); the cost recorded is the one Google reports. While a Google key is set, OpenRouter's `google/gemini-omni*` and `google/veo-*` rows are not listed. This version waits for an Omni video in one held request, so **a service restart while it is being made loses that video** (sent, probably billed); it is marked as such, not waited on.

Not supported in this version: video extension, video editing, lip-sync, upscaling, multiple reference images.

## Available Resources

- `generated-images://{image_id}` - Access specific generated images
- `image-history://recent` - Browse recent generation history
- `storage-stats://overview` - Storage usage and statistics
- `model-info://{model_id}` - Model capabilities and pricing for any configured image model (e.g. `model-info://gpt-image-2`)

## Prompt Templates

Built-in templates for common use cases:

- **Creative Image**: Artistic image generation
- **Product Photography**: Commercial product images
- **Social Media Graphics**: Platform-optimized posts
- **Blog Headers**: Article header images
- **OG Images**: Social media preview images
- **Hero Banners**: Website hero sections
- **Email Headers**: Newsletter headers
- **Video Thumbnails**: YouTube/video thumbnails
- **Infographics**: Data visualization images
- **Artistic Style**: Specific art movement styles
- **Drawing References**: Structural references for pencil drawing practice
- **Gesture Drawing**: Movement and gesture capture studies
- **Basic Shapes Study**: Geometric form construction exercises
- **Contour Drawing**: Edge and form observation practice
- **Value Study**: Light and shadow rendering exercises

## Configuration

Everything is configured with environment variables, named `SECTION__SUBSECTION__FIELD`. Put them in `mcp/.env` (copy `.env.example`, which lists every setting with its default) or in `~/.omniapi/.env`. Restart the daemon after changing them.

Each provider has an API key and an on/off switch; a provider is used only when both are set:

| Provider | Variables | Unlocks |
|---|---|---|
| OpenAI | `PROVIDERS__OPENAI__API_KEY`, `PROVIDERS__OPENAI__ENABLED` | images, transcription, text, speech; Codex CLI agent runs |
| Anthropic | `PROVIDERS__ANTHROPIC__API_KEY`, `PROVIDERS__ANTHROPIC__ENABLED` | text (agent runs on Claude models use the Claude Code login by default, not this key) |
| Google Gemini | `PROVIDERS__GEMINI__API_KEY`, `PROVIDERS__GEMINI__ENABLED` | text, Nano Banana images, TTS; Gemini CLI agent runs. An AI Studio key — a plain string, not a file path |
| DeepSeek | `PROVIDERS__DEEPSEEK__API_KEY`, `PROVIDERS__DEEPSEEK__ENABLED` | text; agent runs through Claude Code |
| OpenRouter | `PROVIDERS__OPENROUTER__API_KEY`, `PROVIDERS__OPENROUTER__ENABLED` | the long tail of text models, OpenRouter's image models, video; agent runs through Claude Code |
| ElevenLabs | `PROVIDERS__ELEVENLABS__API_KEY`, `PROVIDERS__ELEVENLABS__ENABLED` | speech, music |
| kie.ai | `PROVIDERS__KIE__API_KEY`, `PROVIDERS__KIE__ENABLED` | Suno music |

In `.env.example` OpenAI is enabled by default; switch it off if you have no OpenAI key.

Other settings you may want:

| Variable | Default | Meaning |
|---|---|---|
| `STORAGE__BASE_PATH` | `./storage` | where generated images and audio are saved |
| `STORAGE__CLEANUP_ENABLED` | `false` | opt-in retention sweep; by default generated files are kept forever |
| `IMAGES__DEFAULT_MODEL` / `IMAGES__DEFAULT_SIZE` / `IMAGES__DEFAULT_QUALITY` | `gpt-image-2` / `1536x1024` / `auto` | image defaults |
| `OMNIAPI_HOME` | `~/.omniapi` | data home: database, logs, pid file, model-roster cache |
| `OMNIAPI_DEV` | unset | `1` adds the `echo` models and the `replay` harness (no vendor call) |
| `OMNIAPI_OFFLINE` | unset | `1` refuses every call that would reach a paid vendor |
| `OMNIAPI_GUI_DIST` | the repo's `gui/dist` | a built GUI folder to serve (for a copy of the service outside the repo) |
| `OMNIAPI_LOG_MAX_MB` / `OMNIAPI_LOG_BACKUPS` | `5` / `5` | size cap and kept copies of `<data home>/logs/daemon.log` |
| `DEFAULTS__CHAT` / `DEFAULTS__DISPATCH` | `cheap` / `cheap` | model for new chats / agent runs that name none (also `DEFAULTS__SPEECH`, `__MUSIC`, `__TRANSCRIPT`, `__VIDEO`) |
| `VIDEO__MCP_MAX_USD` / `VIDEO__MCP_UNLIMITED` | `1.0` / `false` | per-video estimate limit for `generate_video` (above it, or unpriceable, the call needs `max_cost_usd`); `true` removes the limit |
| `VIDEO__MAX_WAIT_MINUTES` | `20` | how long a video is waited on before it is reported as waited too long |
| `MUSIC__SUNO_ALL_TRACKS` | `true` | keep both songs of a Suno job (each its own file and work, the cost split); `false` keeps only the first. Not on the settings page: `settings.json` (`"music": {"suno_all_tracks": false}`) or this variable |
| `VIDEO__KEEP_COLLECTING` | `true` | after "stop waiting" in the GUI, keep collecting the video and its real cost in the background; `false` leaves it uncollected (cost unknown) |
| `TIERS__CHEAP` / `TIERS__STANDARD` / `TIERS__STRONG` | from `catalog.json` | which model each tier means |

**Settings without a restart.** Keys, provider switches, tiers, default models, the video settings and `music.suno_all_tracks` can also live in `<data home>/settings.json`, which wins over `.env` and the environment. Change it through the running daemon — `GET /api/settings` shows each key only as set / last four characters / where it came from; `PATCH /api/settings` (same shape as the file, `null` removes an entry) applies at once; `POST /api/settings/test-key` checks a key with a free call. Example: `curl -X PATCH http://127.0.0.1:7788/api/settings -H "Content-Type: application/json" -d "{\"providers\":{\"deepseek\":{\"api_key\":\"sk-…\"}}}"`. Only requests from this computer may change anything (non-GET `/api/*` and WebSockets need a loopback `Host` and, if sent, a loopback `Origin`).

**Running outside the repo.** When the package has no `pyproject.toml` beside it (a copied or packaged install), the background daemon works in the data home and, unless `STORAGE__BASE_PATH` is set, saves works to `Documents\OmniAPI`. `omni stop` asks the daemon to shut down cleanly (`POST /api/shutdown`) and only kills it when it has not exited after 15 seconds.

## Version History

### v1.4.0 — Video, and OpenRouter's image models 🎬
- **New modality: video.** `generate_video` makes short clips through OpenRouter's video models — text-to-video, or from a first frame (and a last frame where the model takes one). Options follow each model's `video_params`. A per-video estimate limit (default $1, settings `video.mcp_max_usd` / `video.mcp_unlimited`) keeps MCP callers from sending expensive or unpriceable videos without `max_cost_usd`. Long waits return a `video_<id>` ticket that survives a service restart; the remote job is resumed after a restart. The GUI has a video tab with a confirmation sheet before every send, waiting tickets, a player and video cards on the works wall.
- **OpenRouter's image models** (FLUX, Seedream, Grok Imagine, Qwen, Recraft, MAI…) work with an existing OpenRouter key in `generate_image` and `edit_image`, by their OpenRouter ids; list and prices are fetched live and refreshed daily. `edit_image` gained `image_size` and `aspect_ratio` for them. (This page also corrects an old note: `edit_image` has routed Nano Banana models to Gemini since v1.0.0-alpha.1.)
- **Gemini Omni direct.** `gemini-omni-1.1-flash` is called with the Google key (paid Gemini API tier): always with sound, 720p priced up front, the cost Google reports recorded. A restart while it is being made loses that video in this version. OpenRouter's Google video rows are hidden while the Google key is set.
- **Suno keeps both songs.** `generate_music` and the song-making `edit_music` actions return `tracks` / `track_count`; each song is a work, the cost split. `music.suno_all_tracks` switches back to the first song only. Eight Suno operations (generate, extend, cover, add_instrumental, add_vocals, lyrics, WAV, music video) moved to kie.ai's unified endpoints after a real-key run; upload_extend and stem separation stay on the old ones.
- **`list_available_models` rows carry `rank`** (plus `popularity` and `rank_badge`) from a snapshot of the Artificial Analysis leaderboards; the GUI's model pickers sort by it.
- **Claude thinking.** Claude 4.7+ get adaptive thinking with `output_config.effort` (and no `temperature` / `top_p`), 4.6 the same with `xhigh` lowered to `high`; 4.7+ default to `max_tokens` 16000; Fable 5.1 / Opus 5.5 / Sonnet 5.5 get `tool_choice: auto` instead of a forced tool (with a warning); OpenRouter's Claude models get OpenRouter's `reasoning`. Not verified against a real account.
- **kie.ai**: a task that fails upstream with "please try again later" is reported as a generation failure, not a bad request.
- **Speech**: ElevenLabs `eleven_v4` and `eleven_v4_turbo`. Shutdown dates added for several older image and speech models.
- **Desktop extension**: optional OpenRouter key field (video and OpenRouter's image models).
- 20 tools.

### v1.0.0 — Dispatch center: daemon, web GUI, CLI 🗂️
- **One background daemon, three doors.** `omni serve` runs a single process on `127.0.0.1:7788` that serves the MCP endpoint (`/mcp`, streamable HTTP), a REST + WebSocket API, and the web GUI. Every entry point writes to the same SQLite store (`~/.omniapi/omniapi.db`), so a run dispatched from Claude Code shows up on the board. The stdio server is kept for Claude Desktop.
- **Agent dispatch** — `run_agent` / `get_run` / `list_runs` / `cancel_run` hand a task to the vendor's own headless harness (Claude Code for Anthropic-compatible models including DeepSeek and OpenRouter, Codex CLI for OpenAI, Gemini CLI for Google), with one unified event stream, resume, and cancel. Claude models default to the Claude Code subscription login; API billing is opt-in.
- **Multi-turn chat** — `chat` keeps conversations in the store, streams replies, and records the model per reply so you can switch models mid-conversation. Text providers gained streaming.
- **Web GUI** — live board (stacked run tickets, activity feed, history with filters, two cost ledgers), dispatch form with model / working-directory pickers, follow-ups on the same page, streaming chat with markdown, markdown export, themes.
- **CLI `omni`** — `serve` / `status` / `stop` / `autostart`, `run` / `runs` / `run-log`, `chat`, `mcp-config`.
- **Offline sandbox** — `OMNIAPI_DEV=1` adds an `echo` model and a `replay` harness; `OMNIAPI_OFFLINE=1` refuses every call that would reach a paid vendor.
- 19 tools.

### v1.0.0-alpha.1 — Tool consolidation + live model rosters 🧭
- **The tool surface shrank from twenty-one tools to 14.** The thirteen music tools collapsed into five, four of them dispatched by an `action` argument: `generate_music`, `edit_music(action)`, `music_lyrics(action)`, `music_utility(action)`, `compose_music(action)`. Every operation survives — `extend`, `cover`, `upload_extend`, `add_instrumental`, `add_vocals` and `separate_vocals` are now `edit_music` actions; lyrics, WAV/MP4 rendering and the ElevenLabs composition plan moved to the other three. Fewer, sharper tools cost the client less context.
- **`list_available_models` is now cross-modal.** It lists text, image, transcription, speech and music models together, with provider, online status, pricing, deprecation/shutdown dates and the tier aliases. New flags: `modality`, `include_retired`, `include_snapshots`, `refresh`. Rosters are **live** — each provider is asked what is online at startup (24h cache) and merged with a curated pricing/capability overlay, so a newly released model is callable without a code change.
- **`complete_text` gained three providers and tier aliases.** Anthropic (native Messages API), Google Gemini (OpenAI-compatible endpoint) and OpenRouter (the long tail: Kimi, GLM, MiniMax, Qwen, Mistral, Llama…) join OpenAI and DeepSeek. `model` now accepts **`cheap`** / **`standard`** / **`strong`**, resolved through the catalog, so callers stop pinning model ids.
- **Model rosters refreshed.** Text: OpenAI GPT-6 (`astra` / `sol` / `luna`) alongside GPT-5.x; Claude, Gemini and DeepSeek rosters updated — DeepSeek's cheap model's canonical id is now **`deepseek-flash`**, with `deepseek-v4-flash` kept as an alias. Images: `gpt-image-2.5-sunburst` / `-flare` added; `gpt-image-1` is deprecated and **shuts down 2026-10-23**. Transcription: **`gpt-transcribe`** is the new default; `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` and `gpt-4o-transcribe-diarize` are deprecated with a **2027-02-26** shutdown (and remain the only route to subtitles, word timestamps and diarization). Speech: Gemini TTS added, ElevenLabs roster refreshed, `eleven_turbo_v2_5` deprecated. Music: Suno **V6 / V6_MINI / V6_WILD**; V4 through V5.5 are retired upstream, still accepted but warned about.
- **Google moved to an AI Studio API key.** `PROVIDERS__GEMINI__API_KEY` is now a plain key string; the retired enterprise image path and its service-account JSON file are gone, and a file path is rejected at startup. The remaining Google image models are the Nano Banana family.
- **Deprecated models are not deleted, they are labelled.** Anything `deprecated` stays callable (with a warning and, where announced, a shutdown date in the result metadata); only `retired` models leave the default listing.

### v0.4.7 — Desktop extension timeout fix ⏲️
- **Fixed: high-quality image generation/editing always timed out on the Desktop extension.** The OpenAI provider's default request timeout was 30s, but gpt-image-2 high-quality jobs run 90–150s+ — any install without an explicit `PROVIDERS__OPENAI__TIMEOUT` (i.e. every `.dxt` install) failed with "Request timed out". The default is now **300s** (aligned with the other providers and `.env.example`), and the extension manifest also pins `PROVIDERS__OPENAI__TIMEOUT=300` explicitly. Note this is the *server-side provider* timeout — distinct from the client-side ~60s tool-call limit already handled by the v0.4.0 ticket mechanism; the two fixes compose.

### v0.4.6 — edit_image fix + local file paths 🖼️
- **Fixed: `edit_image` was 100% broken (HTTP 400).** The server-layer validators hand enum members to the editing tool, which passed them straight to the OpenAI SDK — form-encoded as `ImageQuality.HIGH` etc. and rejected by the API. Parameters are now normalized to plain strings (mirroring `generate_image`), with a regression test.
- **New: `edit_image` accepts `image_path` / `additional_image_paths`** — local filesystem paths (STDIO transport, where the server shares the client's filesystem). Full-resolution source/reference images with no base64 inlining; `image_data` is now optional (provide either one).

### v0.4.5 — Maintenance: bug fixes + performance 🧹
- **Generated files are now kept forever by default.** The background retention sweep had a bug that made it a silent no-op for production images (it only scanned the legacy `metadata/` layout). The sweep is fixed — and, more importantly, it is now **opt-in** (`STORAGE__CLEANUP_ENABLED=true`): generated images are precious output, so nothing is auto-deleted unless you explicitly ask for it. Upgrading is safe — no existing files will start disappearing.
- **Fixed: server crashed at startup without an OpenAI key.** `ImageEditingTool` required OpenAI settings at construction time, so Gemini-only / music-only configurations couldn't boot. Editing now degrades gracefully (`edit_image` returns a clear error until a key is added), matching the other capability tools.
- **Fixed: Gemini health check blocked the event loop** (synchronous google-auth credential refresh) — now runs in a worker thread.
- **Fixed: `created_at` was always null** in `generate_image` / `edit_image` result metadata.
- **Fixed: Nano Banana aspect ratios** — `1536x1024` / `1024x1536` now map to the native `3:2` / `2:3` (previously distorted to the legacy `4:3` / `3:4` approximation inherited from Google's older image path).
- **Performance: connection reuse.** Suno (kie.ai), ElevenLabs Music, and ElevenLabs TTS each hold one long-lived HTTP client instead of re-handshaking TLS per call; the editing tool shares the generation tool's OpenAI provider (one client instead of two).
- **Performance: no more event-loop stalls.** Music/speech files are written asynchronously (multi-MB payloads used to block every in-flight job); storage stats / retention scans run in worker threads; the full OpenAI image response (which duplicated every base64 image) is no longer dumped into unused metadata.
- **Reliability: clean shutdown.** All providers (including previously-leaked OpenAI clients) are closed exactly once; JobManager waits for cancelled background jobs instead of abandoning them.
- Internal: version string now has a single source of truth (`omniapi_mcp.__version__`); dead config keys / legacy helpers removed; image-URL building deduplicated; lint/type targets aligned to Python 3.10.

### v0.4.0 — Timeout-proof async jobs ⏱️
- **Call-now / fetch-later for every generation tool.** Any tool that doesn't finish within ~45s now returns a `task_id` ticket and keeps running in the background; fetch the result with the new **`get_job_result`** tool. This defeats the hard ~60s tool-call timeout in Claude Desktop / Cowork (which server-side settings can't change), so Suno music and high-quality / 4K images no longer time out. Fast tools (text, low-quality images, ElevenLabs) still return inline — no UX change.
- New tool: `get_job_result(task_id)`. Implemented as a generic `JobManager` wrapping all 18 generation tools, so the behavior is uniform across every model/provider.

### v0.3.1 — Skill packaging fix
- Trimmed the OmniAPI skill `description` to fit the 1024-character limit (the v0.3.0 music triggers had pushed it to 1033, blocking skill install/upload). No tool or functional changes.

### v0.3.0 — Music generation 🎵
- **New capability: music generation** (5th modality) — 13 new tools, since consolidated into 5 in v1.0.0-alpha.1.
- **Suno** (via the kie.ai aggregator): prompt-to-song generation plus extending, covering, adding backing or vocals to an uploaded file, stem separation, lyrics (plain and word-timestamped), WAV conversion and MP4 rendering. Async jobs are polled to completion internally — no public webhook required. The Suno models of the day were the V4/V5 generation, all since retired upstream.
- **ElevenLabs Music**: prompt-based generation, plus the composition-plan workflow (editable song blueprint → audio + metadata).
- Live-verified end-to-end via Suno (kie.ai). ElevenLabs paths verified to reach the API and error-handle correctly; returning audio needs a paid ElevenLabs plan.

### v0.2.x — Four capabilities + DXT packaging
- **v0.2.0**: unified into OmniAPI — added audio transcription (Whisper / gpt-4o-transcribe), text/chat completion (OpenAI GPT-5.x + DeepSeek V4), and speech synthesis (OpenAI + ElevenLabs) on top of image generation. Packaged as a Claude Desktop DXT extension.
- **v0.2.1–v0.2.4**: DXT manifest fixes (uv server type for manifest_version 0.4, absolute storage_path default).

### v0.1.0 — Image generation
- Forked from image-gen-mcp: OpenAI gpt-image plus Google's image models of the day, text-to-image and mask editing, up to 4K, multiple candidates per call.

## License

MIT License — see [LICENSE](../LICENSE).
