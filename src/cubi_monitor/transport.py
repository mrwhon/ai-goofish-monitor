"""Bounded ordinary chat-completion API; credentials supplied only by host adapter."""
import asyncio
import json
import math
import os
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
