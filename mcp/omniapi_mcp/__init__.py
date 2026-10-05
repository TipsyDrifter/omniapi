"""
OmniAPI MCP Server

A unified MCP (Model Context Protocol) server routing requests to multiple AI
provider APIs: image generation/editing, audio transcription, text completion
and chat, speech synthesis, music generation, and headless agent dispatch —
plus the local daemon (REST / WebSocket / web GUI) and the ``omni`` CLI.
"""

__version__ = "1.3.0"
__author__ = "TipsyDrifter"
__description__ = (
    "Unified MCP server and dispatch center: image, transcription, text/chat, speech, music "
    "and agent runs across OpenAI / Anthropic / Google / DeepSeek / OpenRouter / ElevenLabs / Suno (via kie.ai)"
)
