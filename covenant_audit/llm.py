"""LLM provider interface. The extractor depends only on `LLMProvider`, so the model can be swapped
without touching the deterministic core.

Safety properties of every provider here:
* No tools, functions or code execution are ever offered to the model: the request has only a system
  prompt and a user prompt, and the response is text. There is nothing for an injected instruction to call.
* Output is untrusted text. Nothing in this module parses or trusts it (see extraction.py).
* Secrets come from the environment (OPENROUTER_API_KEY, OPENROUTER_MODEL); none is stored in the repo.
* Timeouts and rate limits are handled with bounded, backed-off retries and surface as typed errors.

Providers:
* `OpenRouterProvider` - a real, live model call over HTTPS (stdlib only).
* `RecordedProvider`  - replays a response saved in a file. It is NOT a model call: `is_live` is False and
  its provenance is carried into every extraction record so a replay can never pass as a live run.
* `ScriptedProvider`  - returns canned strings; for tests only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class LLMProviderError(Exception):
    """Base class for provider failures."""


class LLMConfigurationError(LLMProviderError):
    """Missing configuration, e.g. no API key."""


class LLMTimeoutError(LLMProviderError):
    """The provider did not answer in time, after retries."""


class LLMRateLimitError(LLMProviderError):
    """The provider kept rate-limiting us, after retries."""


class LLMResponseError(LLMProviderError):
    """The provider answered, but the answer is unusable (HTTP error, empty or incomplete completion)."""


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    max_tokens: int = 8000
    temperature: float = 0.0


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    live: bool  # True only for a real model call
    finish_reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=dict)
    note: str = ""  # provenance, e.g. who recorded a replayed response


class LLMProvider(Protocol):
    name: str
    model: str
    is_live: bool

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# A transport posts JSON and returns (status, headers, body-bytes). Injectable so tests need no network.
Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]]


def _urllib_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMTimeoutError(f"network error or timeout: {exc}") from exc


class OpenRouterProvider:
    """OpenRouter chat-completions client. Reads OPENROUTER_API_KEY and OPENROUTER_MODEL from the environment."""

    name = "openrouter"
    is_live = True
    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 3,
        transport: Transport = _urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model if model is not None else os.environ.get("OPENROUTER_MODEL", "")
        if not self._api_key:
            raise LLMConfigurationError("OPENROUTER_API_KEY is not set; live extraction requires configuration")
        if not self.model:
            raise LLMConfigurationError("OPENROUTER_MODEL is not set (for example a free-tier model id from openrouter.ai/models)")
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport = transport
        self._sleep = sleep

    def complete(self, request: LLMRequest) -> LLMResponse:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                status, resp_headers, payload = self._transport(self.ENDPOINT, headers, body, self._timeout)
            except LLMTimeoutError as exc:
                last_error = exc
                self._backoff(attempt, None)
                continue
            if status == 429 or status >= 500:
                last_error = LLMRateLimitError(f"HTTP {status}") if status == 429 else LLMResponseError(f"HTTP {status}")
                self._backoff(attempt, resp_headers.get("Retry-After") or resp_headers.get("retry-after"))
                continue
            if status != 200:
                raise LLMResponseError(f"HTTP {status}: {payload[:300].decode('utf-8', 'replace')}")
            return self._parse(payload)
        if isinstance(last_error, LLMProviderError):
            raise last_error
        raise LLMTimeoutError("provider unavailable after retries")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self._max_retries:
            return
        try:
            delay = min(float(retry_after), 60.0) if retry_after else 2.0**attempt
        except ValueError:
            delay = 2.0**attempt
        self._sleep(delay)

    def _parse(self, payload: bytes) -> LLMResponse:
        try:
            data = json.loads(payload)
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"unexpected provider response shape: {exc}") from exc
        if not isinstance(text, str) or not text.strip():
            raise LLMResponseError("provider returned an empty completion")
        finish = choice.get("finish_reason")
        if finish == "length":
            raise LLMResponseError("completion was cut off at the token limit (incomplete JSON); raise max_tokens or narrow the task")
        usage = {k: int(v) for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
        return LLMResponse(text=text, provider=self.name, model=data.get("model", self.model), live=True, finish_reason=finish, usage=usage)


class RecordedProvider:
    """Replays a response recorded in a JSON file. NOT a model call."""

    name = "recorded"
    is_live = False

    def __init__(self, path: Path) -> None:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        self._text: str = record["response_text"]
        self.model = record.get("authored_by", "unknown")
        self._note = record.get("provenance", "")

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self._text, provider=self.name, model=self.model, live=False, note=self._note)


class ScriptedProvider:
    """Test double: returns the given strings in order."""

    name = "scripted"
    model = "scripted-test-model"
    is_live = False

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise LLMResponseError("scripted provider has no more responses")
        return LLMResponse(text=self._responses.pop(0), provider=self.name, model=self.model, live=False, note="test double")
