"""Codex CLI harness (OpenAI's coding agent) via ``codex exec --json``.

Auth is the OpenAI API key from settings (``OPENAI_API_KEY``); ``CODEX_HOME``
points at ``~/.omniapi/harness/codex`` so the user's own ~/.codex login and
config stay untouched. Resume: ``codex exec resume <thread_id> <prompt>``.

JSONL events (non-interactive mode docs): thread.started, turn.started,
item.started/updated/completed (agent_message, reasoning, command_execution,
file_change, mcp_tool_call, web_search, todo_list), turn.completed(usage),
turn.failed(error), error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from ..catalog import catalog
from .base import HEADLESS_SYSTEM_APPEND, HarnessAdapter, clean_env, harness_home, which_cli
from .events import RunEvent, RunSpec

logger = logging.getLogger(__name__)


class CodexHarness(HarnessAdapter):
    name = "codex"
    supports_resume = True

    def __init__(self, settings: Any):
        super().__init__(settings)
        self._proc: Optional[asyncio.subprocess.Process] = None

    def build_command(self, spec: RunSpec) -> tuple[list[str], dict[str, str]]:
        cli = which_cli("codex")
        if not cli:
            raise RuntimeError("codex CLI not found on PATH (npm install -g @openai/codex)")
        model = spec.resolved_model or spec.model
        args = [cli, "exec"]
        resuming = bool(spec.resume_session_id)
        if resuming:
            # `exec resume` takes no -C / -s: the session remembers its cwd and
            # sandbox; we still pass cwd to the subprocess itself.
            args += ["resume", spec.resume_session_id]
        args += ["--json", "-m", model, "--skip-git-repo-check"]
        if spec.cwd and not resuming:
            args += ["-C", spec.cwd]
        if spec.yolo:
            args += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            # Windows has no Codex OS sandbox: `workspace-write` silently
            # degrades to read-only ("workspace is read-only", verified
            # 2026-09-25). Run unsandboxed like the other harnesses — the
            # agent is still confined by cwd and exec mode never prompts.
            # `exec resume` has no -s flag but honours the config override.
            mode = "danger-full-access" if os.name == "nt" else "workspace-write"
            args += ["-c", f'sandbox_mode="{mode}"'] if resuming else ["-s", mode]
        args += ["-"]  # prompt from stdin (safe for quotes/newlines)

        env = clean_env()
        env["OPENAI_API_KEY"] = self.settings.providers.openai.api_key
        env["CODEX_HOME"] = str(harness_home("codex"))
        env.pop("CLAUDE_CONFIG_DIR", None)
        return args, env

    def ensure_auth(self, env: dict[str, str]) -> None:
        """Codex ignores OPENAI_API_KEY for auth; the isolated CODEX_HOME needs a
        one-time `codex login --with-api-key` (writes auth.json)."""
        import subprocess

        home = harness_home("codex")
        if (home / "auth.json").exists():
            return
        cli = which_cli("codex")
        key = self.settings.providers.openai.api_key
        r = subprocess.run([cli, "login", "--with-api-key"], input=key, capture_output=True, text=True, env=env, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"codex login failed: {(r.stderr or r.stdout).strip()[:300]}")
        logger.info("codex: logged the isolated CODEX_HOME in with the API key")

    @staticmethod
    def _item_events(item: dict[str, Any], tool_ids: dict[str, str]) -> list[RunEvent]:
        t = item.get("type")
        iid = item.get("id") or ""
        if t == "agent_message":
            return [RunEvent("text", {"text": item.get("text", "")})]
        if t == "reasoning":
            return [RunEvent("thinking", {"text": item.get("text", "")})]
        if t == "command_execution":
            evs = [RunEvent("tool_call", {"id": iid, "name": "Bash", "input": {"command": item.get("command", "")}})]
            if item.get("status") in ("completed", "failed") or "exit_code" in item:
                evs.append(
                    RunEvent(
                        "tool_result",
                        {
                            "id": iid,
                            "name": "Bash",
                            "output": HarnessAdapter.truncate(item.get("aggregated_output", "")),
                            "is_error": bool(item.get("exit_code")),
                        },
                    )
                )
            return evs
        if t == "file_change":
            changes = item.get("changes") or []
            return [
                RunEvent(
                    "tool_call",
                    {"id": iid, "name": "Edit", "input": {"file_path": ", ".join(c.get("path", "") for c in changes), "kinds": [c.get("kind") for c in changes]}},
                ),
                RunEvent("tool_result", {"id": iid, "name": "Edit", "output": f"{len(changes)} file(s) changed", "is_error": item.get("status") == "failed"}),
            ]
        if t == "mcp_tool_call":
            name = f"mcp__{item.get('server', '?')}__{item.get('tool', '?')}"
            evs = [RunEvent("tool_call", {"id": iid, "name": name, "input": item.get("arguments") or {}})]
            if item.get("status") in ("completed", "failed"):
                evs.append(RunEvent("tool_result", {"id": iid, "name": name, "output": HarnessAdapter.truncate(item.get("result") or item.get("error")), "is_error": item.get("status") == "failed"}))
            return evs
        if t == "web_search":
            return [RunEvent("tool_call", {"id": iid, "name": "WebSearch", "input": {"query": item.get("query", "")}})]
        if t == "error":
            return [RunEvent("status", {"message": item.get("message", "error item")})]
        return []

    async def start(self, spec: RunSpec) -> AsyncIterator[RunEvent]:
        args, env = self.build_command(spec)
        self.ensure_auth(env)
        model = spec.resolved_model or spec.model
        prompt = spec.prompt
        if spec.system_append or not spec.resume_session_id:
            prompt = f"{HEADLESS_SYSTEM_APPEND}\n{spec.system_append or ''}\n\n任務：\n{spec.prompt}"
        thread_id: Optional[str] = None
        last_text = ""
        usage_total: dict[str, int] = {}
        seen_completed: set[str] = set()
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
                if et == "thread.started":
                    thread_id = ev.get("thread_id")
                    yield RunEvent("session_start", {"session_id": thread_id, "model": model})
                elif et == "turn.started":
                    yield RunEvent("status", {"message": "turn started"})
                elif et in ("item.completed", "item.updated", "item.started"):
                    item = ev.get("item") or {}
                    key = f"{item.get('id')}:{item.get('type')}:{et}"
                    if et != "item.completed" and item.get("type") in ("agent_message", "reasoning"):
                        continue  # only emit prose once, when completed
                    if key in seen_completed:
                        continue
                    seen_completed.add(key)
                    for e in self._item_events(item, {}):
                        if e.type == "text":
                            last_text = e.payload.get("text", "") or last_text
                        yield e
                elif et == "turn.completed":
                    u = ev.get("usage") or {}
                    for k, v in u.items():
                        if isinstance(v, int):
                            usage_total[k] = usage_total.get(k, 0) + v
                elif et == "turn.failed":
                    yield RunEvent("error", {"message": (ev.get("error") or {}).get("message", "turn failed")})
                elif et == "error":
                    yield RunEvent("error", {"message": ev.get("message", "error")})
                else:
                    yield RunEvent("status", {"message": et})

            rc = await self._proc.wait()
            stderr = (await self._proc.stderr.read()).decode("utf-8", "replace") if self._proc.stderr else ""
            if self._cancelled:
                yield RunEvent("error", {"message": "cancelled"})
                return
            norm = {
                "prompt_tokens": usage_total.get("input_tokens", 0),
                "completion_tokens": usage_total.get("output_tokens", 0),
                "cached_tokens": usage_total.get("cached_input_tokens", 0),
                "raw": usage_total,
            }
            yield RunEvent(
                "result",
                {
                    "text": last_text,
                    "usage": norm,
                    "cost_usd": catalog.estimate_text_cost(model, norm),
                    "session_id": thread_id,
                    "is_error": rc != 0,
                    "exit_code": rc,
                    "stderr": self.truncate(stderr, 2000) if rc != 0 else None,
                },
            )
        except Exception as e:
            logger.exception("codex harness failed")
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
