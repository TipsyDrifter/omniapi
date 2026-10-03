"""Gemini CLI harness via ``gemini -p … --output-format stream-json``.

Auth: ``GEMINI_API_KEY`` from settings. Sessions: we pre-assign a UUID with
``--session-id`` so a follow-up can ``--resume`` it (documented as accepting
"latest"/index; UUID acceptance is verified at M3 test time — see the
decision record's open items).

stream-json events (docs/cli/headless.md): init, message, tool_use,
tool_result, error, result{response, stats}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Optional

from ..capabilities.text import fold_unreported_output
from ..catalog import catalog
from .base import HEADLESS_SYSTEM_APPEND, HarnessAdapter, clean_env, which_cli
from .events import RunEvent, RunSpec

logger = logging.getLogger(__name__)


class GeminiHarness(HarnessAdapter):
    name = "gemini"
    supports_resume = True

    def __init__(self, settings: Any):
        super().__init__(settings)
        self._proc: Optional[asyncio.subprocess.Process] = None

    def build_command(self, spec: RunSpec) -> tuple[list[str], dict[str, str], str]:
        cli = which_cli("gemini")
        if not cli:
            raise RuntimeError("gemini CLI not found on PATH (npm install -g @google/gemini-cli)")
        model = spec.resolved_model or spec.model
        session_id = spec.resume_session_id or str(uuid.uuid4())
        args = [cli, "-o", "stream-json", "-m", model, "--skip-trust", "--approval-mode", "yolo" if spec.yolo else "auto_edit"]
        if spec.resume_session_id:
            args += ["--resume", spec.resume_session_id]
        else:
            args += ["--session-id", session_id]
        # The prompt itself goes through stdin; -p carries the short instruction.
        args += ["-p", "Follow the task given on stdin."]
        env = clean_env()
        env["GEMINI_API_KEY"] = self.settings.providers.gemini.api_key
        env.pop("CLAUDE_CONFIG_DIR", None)
        return args, env, session_id

    async def start(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        args, env, session_id = self.build_command(spec)
        model = spec.resolved_model or spec.model
        prompt = f"{HEADLESS_SYSTEM_APPEND}\n{spec.system_append or ''}\n\n任務：\n{spec.prompt}"
        last_text = ""
        stats: dict[str, Any] = {}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=spec.cwd or None,
            )
            assert self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(prompt.encode("utf-8"))
            await self._proc.stdin.drain()
            self._proc.stdin.close()
            yield RunEvent("session_start", {"session_id": session_id, "model": model})

            async for raw in self._proc.stdout:
                if self._cancelled:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    yield RunEvent("status", {"message": line[:300]})
                    continue
                et = ev.get("type", "")
                if et == "init":
                    yield RunEvent("status", {"message": "init", "data": {k: ev.get(k) for k in ("model", "session_id") if k in ev}})
                elif et == "message":
                    role = ev.get("role")
                    content = ev.get("content")
                    if isinstance(content, list):
                        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                    if role == "assistant" and content:
                        last_text = content
                        yield RunEvent("text", {"text": content})
                elif et == "tool_use":
                    yield RunEvent("tool_call", {"id": ev.get("id") or ev.get("tool_id") or "", "name": ev.get("name") or ev.get("tool_name") or "tool", "input": ev.get("input") or ev.get("args") or ev.get("parameters") or {}})
                elif et == "tool_result":
                    out = ev.get("output") or ev.get("result") or ev.get("content") or ""
                    yield RunEvent("tool_result", {"id": ev.get("id") or ev.get("tool_id") or "", "name": ev.get("name") or ev.get("tool_name"), "output": self.truncate(out), "is_error": bool(ev.get("is_error") or ev.get("error"))})
                elif et == "error":
                    yield RunEvent("error", {"message": ev.get("message") or json.dumps(ev)[:300]})
                elif et == "result":
                    if ev.get("response"):
                        last_text = ev["response"]
                    stats = ev.get("stats") or {}
                else:
                    yield RunEvent("status", {"message": et})

            rc = await self._proc.wait()
            stderr = (await self._proc.stderr.read()).decode("utf-8", "replace") if self._proc.stderr else ""
            if self._cancelled:
                yield RunEvent("error", {"message": "cancelled"})
                return
            # The CLI's stats: input_tokens includes the cached part ("cached"), and thinking
            # is only visible as total - input - output — billed as output, so fold it in.
            norm = fold_unreported_output(
                {
                    "prompt_tokens": stats.get("input_tokens") or stats.get("prompt_tokens") or 0,
                    "completion_tokens": stats.get("output_tokens") or stats.get("candidates_tokens") or 0,
                    "total_tokens": stats.get("total_tokens") or 0,
                    "cached_tokens": stats.get("cached") or stats.get("cached_tokens") or stats.get("cached_content_tokens") or 0,
                    "raw": stats,
                }
            )
            yield RunEvent(
                "result",
                {
                    "text": last_text,
                    "usage": norm,
                    "cost_usd": catalog.estimate_text_cost(model, norm),
                    "session_id": session_id,
                    "is_error": rc != 0,
                    "exit_code": rc,
                    "stderr": self.truncate(stderr, 2000) if rc != 0 else None,
                },
            )
        except Exception as e:
            logger.exception("gemini harness failed")
            yield RunEvent("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            self._proc = None

    async def cancel(self) -> None:
        await super().cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except Exception:
                pass
