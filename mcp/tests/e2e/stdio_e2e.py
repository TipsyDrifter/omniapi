"""End-to-end: spawn the MCP server over stdio exactly like Claude Code does
(uv --directory <mcp> run omniapi-mcp), list tools, call list_available_models
and one cheap complete_text."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_DIR = r"C:\STRIX16\Claude\ClaudeCode\Media\OmniAPI\mcp"
UV = r"C:\Users\User\miniconda3\Scripts\uv.exe"


async def main():
    params = StdioServerParameters(
        command=UV, args=["--directory", MCP_DIR, "run", "omniapi-mcp"]
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("TOOLS", len(names), names)

            r = await session.call_tool("list_available_models", {"modality": "text"})
            data = json.loads(r.content[0].text)
            print("TEXT MODELS", data.get("counts"), "default:", data.get("default_text_model"))
            print("DISCOVERY", data.get("discovery"))
            ids = [m["id"] for m in data["models"]["text"]]
            print("has gpt-6/claude/deepseek:",
                  any(i.startswith("gpt-6") for i in ids),
                  any(i.startswith("claude-opus-5-5") for i in ids),
                  "deepseek-flash" in ids)

            r = await session.call_tool(
                "complete_text",
                {"prompt": "Reply with exactly: PONG", "model": "cheap", "max_completion_tokens": 32},
            )
            print("COMPLETE", r.content[0].text[:300])


asyncio.run(main())
