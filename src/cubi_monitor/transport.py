"""Bounded ordinary chat-completion API; credentials supplied only by host adapter."""
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .contracts import AnalysisError


class ChatTransport:
    def __init__(self, settings, ledger):
        self.settings, self.ledger = settings, ledger
        url = settings.get("url", "")
        if urlsplit(url).scheme != "https" or urlsplit(url).username or not url.endswith("/chat/completions"):
            raise ValueError("invalid_ai_endpoint")
        if not settings.get("model") or not settings.get("key_env"):
            raise ValueError("missing_ai_settings")
        if not 0 < settings.get("cost_cap_usd",0) <= 3:
            raise ValueError("invalid_ai_cost_cap")
        for field in ("input_usd_per_million_ceiling", "output_usd_per_million_ceiling"):
            if not isinstance(settings.get(field),(int,float)) or not math.isfinite(settings[field]) or not 0 < settings[field] <= 100:
                raise ValueError("invalid_ai_price_ceiling")

    async def __call__(self, system, user, max_tokens):
        # UTF-8 bytes bound token count conservatively; twice verified peak Flash price.
        allowance = ((len(system.encode())+len(user.encode())+256)*self.settings["input_usd_per_million_ceiling"] + max_tokens*self.settings["output_usd_per_million_ceiling"])/1_000_000
        consumed = float(self.ledger.meta("ai_reserved_usd") or 0)
        if consumed+allowance > self.settings["cost_cap_usd"]:
            raise AnalysisError("ai_cost_cap_reached")
        token = os.environ.get(self.settings["key_env"])
        if not token:
            raise AnalysisError("missing_ai_credential")
        self.ledger.meta("ai_reserved_usd",consumed+allowance)
        payload = {"model":self.settings["model"],"messages":[{"role":"system","content":system},{"role":"user","content":user}],"thinking":{"type":"disabled"},"response_format":{"type":"json_object"},"max_tokens":max_tokens,"stream":False}
        def request():
            req = Request(self.settings["url"],data=json.dumps(payload).encode(),headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
            try:
                with urlopen(req,timeout=90) as response:
                    raw = response.read(200001)
                if len(raw)>200000:
                    raise AnalysisError("oversized_ai_response")
                value = json.loads(raw)
                choice = value["choices"][0]
                if choice.get("finish_reason") != "stop":
                    raise AnalysisError("incomplete_ai_response")
                return value, choice["message"]["content"]
            except (HTTPError,URLError,TimeoutError,OSError):
                # Never include request headers, response errors or credentials in logs.
                raise AnalysisError("ai_network_failure") from None
            except (ValueError,KeyError,TypeError,IndexError):
                raise AnalysisError("invalid_ai_response") from None
        value, content = await asyncio.to_thread(request)
        self.ledger.meta("ai_last_receipt",json.dumps({"requested_model":self.settings["model"],"served_model":value.get("model"),"usage":value.get("usage",{}),"reserved_usd":allowance}))
        return content


class ClaudeTransport:
    """One tool-free call through the host's existing Claude provider launcher."""

    def __init__(self, settings, ledger):
        self.settings, self.ledger = settings, ledger
        launcher = settings.get("launcher", "")
        if not Path(launcher).is_absolute() or not settings.get("model"):
            raise ValueError("invalid_claude_transport")
        if type(settings.get("timeout", 120)) is not int or not 10 <= settings.get("timeout", 120) <= 180:
            raise ValueError("invalid_claude_timeout")

    async def __call__(self, system, user, max_tokens):
        env = {"HOME": "/home/dev", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "GLM_MODEL": self.settings["model"],
               "GLM_FAST_MODEL": self.settings["model"], "CLAUDE_CODE_EFFORT_LEVEL": "low",
               "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens)}
        command = [self.settings["launcher"], "-p", "--bare", "--safe-mode",
                   "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands",
                   "--no-chrome", "--permission-mode", "dontAsk", "--tools", "",
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--max-turns", "1", "--max-budget-usd", "0.20",
                   "--system-prompt", system, "--output-format", "stream-json", "--verbose"]

        async def request():
            # Anonymous temporary files bound model-visible output and hold no durable copy.
            with tempfile.TemporaryDirectory(prefix="goofish-model-") as cwd, tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                process = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env,
                            stdin=asyncio.subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
                try:
                    await asyncio.wait_for(process.communicate(user.encode()), self.settings.get("timeout", 120))
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise AnalysisError("ai_cli_timeout") from None
                if process.returncode:
                    raise AnalysisError("ai_cli_failed")
                out.seek(0)
                raw = out.read(200001)
                if len(raw) > 200000:
                    raise AnalysisError("oversized_ai_response")
                events = [json.loads(line) for line in raw.splitlines() if line.strip()]
                results = [e for e in events if e.get("type") == "result"]
                init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
                assistants = [e.get("message", {}) for e in events if e.get("type") == "assistant"]
                if len(results) != 1 or len(init) != 1 or not assistants:
                    raise AnalysisError("ai_cli_events_invalid")
                if any(b.get("type") in ("tool_use", "server_tool_use") for a in assistants for b in a.get("content", [])):
                    raise AnalysisError("ai_cli_tool_use_refused")
                return results[0], init[0].get("model"), {a.get("model") for a in assistants}

        try:
            value, client_model, served_models = await request()
            model = self.settings["model"]
            if (value.get("type") != "result" or value.get("subtype") != "success"
                    or value.get("is_error") is not False or value.get("num_turns") != 1
                    or not isinstance(client_model, str) or served_models != {model}
                    or set(value.get("modelUsage", {})) != {client_model}
                    or value.get("permission_denials")):
                raise AnalysisError("ai_cli_identity_or_result_invalid")
            # Claude's cost accounting uses its client profile. The assistant
            # message carries the actual provider model, verified separately.
            usage = value["modelUsage"][client_model]
            if (type(usage.get("outputTokens")) is not int
                    or not 0 < usage["outputTokens"] <= max_tokens
                    or not isinstance(value.get("result"), str)):
                raise AnalysisError("ai_cli_usage_or_content_invalid")
            # Persist only known scalar usage fields; never provider bodies or secrets.
            receipt = {"transport": "claude-cli", "requested_model": model, "served_model": model,
                       "client_model": client_model,
                       "usage": {k: usage[k] for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens") if type(usage.get(k)) is int},
                       "num_turns": 1}
            self.ledger.meta("ai_last_receipt", json.dumps(receipt))
            return value["result"]
        except AnalysisError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise AnalysisError("invalid_ai_cli_response") from None


def configured_transport(settings, ledger):
    kind = settings.get("transport", "chat-completions")
    if kind == "claude-cli":
        return ClaudeTransport(settings, ledger)
    if kind == "chat-completions":
        return ChatTransport(settings, ledger)
    raise ValueError("unsupported_ai_transport")
