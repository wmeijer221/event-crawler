"""
Taken and adapted from https://github.com/PELAB-LiU/CRML/blob/main/LLM/utils.py (no specified license).
"""

"""LLM client abstraction for CRML generation experiments.

Three backends (Ollama, OpenAI, Anthropic) sit behind a uniform interface.
AgentBase always works with canonical OpenAI-style message dicts; each backend
translates to its native wire format before the API call and normalises the
response back to LLMResponse + ResponseMetrics.
"""

import json
import time
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, asdict
from typing import Optional, List, ClassVar, Tuple, Any, Literal
import itertools
import httpx
import ollama
from mcp import ClientSession, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.client.stdio import stdio_client


# ── Uniform return types ──────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ResponseMetrics:
    """All fields are optional — providers expose different subsets."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    duration_s: float | None = None
    tokens_per_s: float | None = None
    cache_read_tokens: int | None = None   # Anthropic / OpenAI prompt-cache reads
    cache_write_tokens: int | None = None  # Anthropic cache-creation tokens


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    metrics: ResponseMetrics = field(default_factory=ResponseMetrics)
    options: dict = field(default_factory=dict)
    exceptions: list = field(default_factory=list)


# ── Ollama options ────────────────────────────────────────────────────────────


@dataclass
class ModelOptions(ABC):
    """
    Data class for model options. Allows specifying regular
    LLM options that can be passed through the LLM lib's 
    `options` parameter when calling its respective `chat`
    method (temperature, seed, etc.), and "special" parameters
    that are passed directly as arguments to the chat
    (e.g., think).
    """

    _special_fields: ClassVar[set[str]] = set()

    def to_dict(self) -> dict:
        """
        Converts the dataclass to a dictionary, 
        stripping out any keys where the value is None.
        """
        dict_ = {
            k: v for k, v in asdict(self).items()
            if v is not None
        }
        return dict_

    def to_options_dict(self) -> dict:
        """
        Converts the dataclass to a dictionary, 
        stripping out any keys where the value is None,
        and fields that are marked as special.
        """
        dict_ = {
            k: v for k, v in self.to_dict().items()
            if k not in self._special_fields
        }
        return dict_

    def to_special_dict(self) -> dict:
        """
        Converts the dataclass to a dictionary, 
        stripping out any keys where the value is None,
        and fields that are not marked as special.
        """
        dict_ = {
            k: v for k, v in self.to_dict().items()
            if k in self._special_fields
        }
        return dict_


@dataclass
class OllamaModelOptions(ModelOptions):
    """
    Captures the parameters that can be passed to the `options` argument 
    in the Ollama Python client.
    """
    _special_fields = {'think', 'format'}

    # Context and Execution Parameters
    num_ctx: Optional[int] = None
    num_predict: Optional[int] = None
    num_batch: Optional[int] = None
    num_thread: Optional[int] = None
    num_keep: Optional[int] = None

    # Sampling / Creativity Parameters
    temperature: Optional[float] = None
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    min_p: Optional[float] = None
    tfs_z: Optional[float] = None
    typical_p: Optional[float] = None
    seed: Optional[int] = None

    # Penalty Parameters
    repeat_last_n: Optional[int] = None
    repeat_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    penalize_newline: Optional[bool] = None

    # Mirostat (Dynamic Temperature) Parameters
    mirostat: Optional[int] = None
    mirostat_tau: Optional[float] = None
    mirostat_eta: Optional[float] = None

    # GPU / Hardware Parameters
    numa: Optional[bool] = None
    num_gpu: Optional[int] = None
    main_gpu: Optional[int] = None
    low_vram: Optional[bool] = None
    f16_kv: Optional[bool] = None
    use_mmap: Optional[bool] = None
    use_mlock: Optional[bool] = None

    # Output/Other Parameters
    stop: Optional[List[str]] = None
    logits_all: Optional[bool] = None
    vocab_only: Optional[bool] = None

    # Special Parameters
    think: Optional[bool | str] = None
    format: Optional[str] = None


# ── Backend ABC ───────────────────────────────────────────────────────────────


class Backend(ABC):
    """Translate between canonical messages/tools and a specific provider's API."""

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """Single non-streaming completion. Messages and tools are canonical."""
        ...

    def make_assistant_tool_call_message(self, response: LLMResponse) -> dict:
        """Canonical assistant message carrying tool calls (appended to history)."""
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in response.tool_calls
            ],
        }

    def make_tool_result_message(self, tool_call: ToolCall, content: str) -> dict:
        """Canonical tool-result message (appended after each tool execution)."""
        return {"role": "tool", "tool_call_id": tool_call.id, "content": content}


# ── Ollama backend ────────────────────────────────────────────────────────────

class OllamaBackend(Backend):
    def __init__(self, model: str, client: ollama.AsyncClient, max_retries: int = 3, options: Optional[OllamaModelOptions] = None):
        self._model = model
        self._client = client
        self._max_retries = max_retries
        self._options = options if options is not None else OllamaModelOptions()

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        last_exc: BaseException | None = None
        excs: list[BaseException | None] = list()
        attempt_idx = -1
        for attempt_idx in range(self._max_retries + 1):
            try:
                t0 = time.monotonic()
                response = await self._client.chat(
                    model=self._model,
                    messages=[_to_ollama_msg(m) for m in messages],
                    tools=tools,
                    options=self._options.to_options_dict(),
                    **self._options.to_special_dict()
                )
                duration_s = time.monotonic() - t0
                break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                excs.append(last_exc)
                if attempt_idx < self._max_retries:
                    print(
                        f"[WARNING] Ollama request failed (attempt {attempt_idx + 1}/{self._max_retries + 1}): {exc}. Retrying...")
                else:
                    raise

        msg = response.message
        tool_calls = []
        for i, tc in enumerate(msg.tool_calls or []):
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            # Ollama omits tool-call IDs; synthesise one for canonical bookkeeping
            tool_calls.append(ToolCall(
                id=f"call_{tc.function.name}_{i}",
                name=tc.function.name,
                arguments=args,
            ))

        pc, ec, ed = response.prompt_eval_count, response.eval_count, response.eval_duration
        metrics = ResponseMetrics(
            input_tokens=pc,
            output_tokens=ec,
            total_tokens=(
                pc or 0) + (ec or 0) if (pc is not None or ec is not None) else None,
            duration_s=duration_s,
            tokens_per_s=(ec / (ed / 1e9)) if ec and ed else None,
        )

        options = self.get_options()

        resp = LLMResponse(content=msg.content, tool_calls=tool_calls,
                           metrics=metrics, options=options, exceptions=excs)
        return resp

    def get_options(self) -> dict[str, Any]:
        """
        Returns the model options used by this backend, and 
        whether whether they are a default setting, model setting,
        or user-specified setting.
        """
        opt_vals = dict()
        opt_srcs = dict()
        for key, value in _get_ollama_default_options().items():
            opt_vals[key] = value
            opt_srcs[key] = "default"
        model_details = ollama.show(model=self._model)
        if 'parameters' in model_details:
            for key, value in itertools.batched(model_details['parameters'].split(), 2):
                opt_vals[key] = value
                opt_srcs[key] = "model"
        for key, value in self._options.to_dict().items():
            opt_vals[key] = value
            opt_srcs[key] = 'user'
        options_ = {
            'values': opt_vals,
            'sources': opt_srcs
        }
        return options_


def _to_ollama_msg(msg: dict) -> dict:
    """Strip canonical fields Ollama doesn't understand."""
    role = msg["role"]
    if role == "tool":
        return {"role": "tool", "content": msg["content"]}
    if role == "assistant" and msg.get("tool_calls"):
        return {
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": [
                {
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": (
                            json.loads(tc["function"]["arguments"])
                            if isinstance(tc["function"]["arguments"], str)
                            else tc["function"]["arguments"]
                        ),
                    }
                }
                for tc in msg["tool_calls"]
            ],
        }
    return msg


def _get_ollama_default_options() -> dict:
    # Copied from: https://docs.ollama.com/modelfile (sep 10th, 2026)
    # because these can't be automatically extracted from the API, so
    # this is only correct if ollama doesn't change its default.
    options = {
        "num_ctx": 2048,
        "repeat_last_n": 64,
        "repeat_penalty": 1.0,
        "temperature": 0.8,
        "seed": 0,
        "num_predict": -1,
        "draft_num_predict": 4,
        "top_k": 40,
        "top_p": 0.9,
        "min_p": 0.0
    }
    return options

# ── OpenAI backend ────────────────────────────────────────────────────────────


class OpenAIBackend(Backend):
    def __init__(self, model: str, client):  # openai.AsyncOpenAI
        self._model = model
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        t0 = time.monotonic()
        kwargs: dict = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        response = await self._client.chat.completions.create(**kwargs)
        duration_s = time.monotonic() - t0

        msg = response.choices[0].message
        tool_calls = []
        for tc in msg.tool_calls or []:
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args)
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        u = response.usage
        cache_read = None
        if u and getattr(u, "prompt_tokens_details", None):
            cache_read = getattr(u.prompt_tokens_details,
                                 "cached_tokens", None)
        metrics = ResponseMetrics(
            input_tokens=u.prompt_tokens if u else None,
            output_tokens=u.completion_tokens if u else None,
            total_tokens=u.total_tokens if u else None,
            duration_s=duration_s,
            cache_read_tokens=cache_read,
        )
        return LLMResponse(content=msg.content, tool_calls=tool_calls, metrics=metrics)


# ── Anthropic backend ─────────────────────────────────────────────────────────

class AnthropicBackend(Backend):
    def __init__(self, model: str, client):  # anthropic.AsyncAnthropic
        self._model = model
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        t0 = time.monotonic()
        kwargs: dict = {
            "model": self._model,
            "max_tokens": 8192,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)
        response = await self._client.messages.create(**kwargs)
        duration_s = time.monotonic() - t0

        content_text = None
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=block.input))

        u = response.usage
        metrics = ResponseMetrics(
            input_tokens=u.input_tokens if u else None,
            output_tokens=u.output_tokens if u else None,
            total_tokens=(u.input_tokens + u.output_tokens) if u else None,
            duration_s=duration_s,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", None),
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", None),
        )
        return LLMResponse(content=content_text, tool_calls=tool_calls, metrics=metrics)


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Translate canonical message list to Anthropic's format.

    Key differences:
    - No 'tool' role: consecutive tool results fold into a single user message
      with tool_result content blocks.
    - Assistant tool calls become tool_use content blocks, not a tool_calls key.
    - System messages are dropped here (pass separately via system= if needed).
    """
    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg["role"]

        if role == "system":
            i += 1

        elif role in ("user", "assistant") and not msg.get("tool_calls"):
            result.append({"role": role, "content": msg.get("content") or ""})
            i += 1

        elif role == "assistant" and msg.get("tool_calls"):
            content = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                args = tc["function"]["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                content.append({"type": "tool_use", "id": tc["id"],
                                "name": tc["function"]["name"], "input": args})
            result.append({"role": "assistant", "content": content})
            i += 1

        elif role == "tool":
            # Collect consecutive tool results → one user message
            tool_results = []
            while i < len(messages) and messages[i]["role"] == "tool":
                m = messages[i]
                tool_results.append({"type": "tool_result",
                                     "tool_use_id": m["tool_call_id"],
                                     "content": m["content"]})
                i += 1
            result.append({"role": "user", "content": tool_results})

        else:
            i += 1

    return result


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"]["parameters"],
        }
        for t in tools
    ]


# ── Factory ───────────────────────────────────────────────────────────────────

async def create_backend(
    provider: str,
    model: str,
    *,
    host: str | None = None,
    api_key: str | None = None,
    headers: dict | None = None,
    timeout_s: float = 300.0,
    max_retries: int = 3,
    options: ModelOptions | None = None
) -> Backend:
    """Create and validate a backend.

    Examples::

        # Ollama (local or proxied)
        backend = await create_backend("ollama", "qwen3:14b",
                                        host="https://...", headers=AUTH)

        # OpenAI (or any OpenAI-compatible endpoint)
        backend = await create_backend("openai", "gpt-4o",
                                        api_key=os.environ["OPENAI_API_KEY"])

        # Anthropic
        backend = await create_backend("anthropic", "claude-sonnet-4-6",
                                        api_key=os.environ["ANTHROPIC_API_KEY"])
    """
    provider = provider.lower()

    if provider == "ollama":
        if not (options is None or isinstance(options, OllamaModelOptions)):
            raise TypeError(
                f"`options` must be `OllamaModelOptions`or `None`, not `{type(options)}`.")
        client = ollama.AsyncClient(
            host=host, headers=headers, timeout=timeout_s)
        async with httpx.AsyncClient(headers=headers) as http:
            r = await http.get(f"{host}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            print(f"Ollama is up. Available models: {models}")
            if not any(model in m for m in models):
                raise ValueError(
                    f"Model '{model}' not found. Run: ollama pull {model}")
            print(f"✓ Model '{model}' is ready.")
        return OllamaBackend(model, client, max_retries=max_retries, options=options)

    elif provider == "openai":
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=host,
            timeout=httpx.Timeout(timeout_s),
            max_retries=max_retries,
        )
        print(f"OpenAI backend ready (model={model})")
        return OpenAIBackend(model, client)

    elif provider == "anthropic":
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(
            api_key=api_key,
            timeout=httpx.Timeout(timeout_s),
            max_retries=max_retries,
        )
        print(f"Anthropic backend ready (model={model})")
        return AnthropicBackend(model, client)

    else:
        raise ValueError(
            f"Unknown provider '{provider}'. Choose: ollama | openai | anthropic")


# ── Agent base ────────────────────────────────────────────────────────────────

DEFAULT_MAX_TOOL_CALLS: int = 20


class ToolCallLimitExceeded(RuntimeError):
    pass


class AgentBase:
    def __init__(self, backend: Backend, verbose: bool = True, max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS):
        self.backend = backend
        self.verbose = verbose
        self.max_tool_calls = max_tool_calls
        self.messages: list[dict] = []
        self.metrics_log: list[ResponseMetrics] = []
        self.tool_call_limit_exceeded: bool = False
        self._sessions: list[ClientSession] = []
        self._stacks: list[AsyncExitStack] = []
        self._tool_to_session: dict[str, ClientSession] = {}
        self._tools: list[dict] = []       # canonical function-calling format
        self._tool_names: list[str] = []
        self.responses: list[LLMResponse] = []

    async def _add_http_session(self, url: str):
        stack = AsyncExitStack()
        self._stacks.append(stack)
        read, write, _ = await stack.enter_async_context(streamable_http_client(url))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions.append(session)

    async def _add_stdio_session(self, server_params: StdioServerParameters):
        stack = AsyncExitStack()
        self._stacks.append(stack)
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions.append(session)

    async def _init_tools(self):
        for session in self._sessions:
            mcp_tools = await session.list_tools()
            for t in mcp_tools.tools:
                self._tool_names.append(t.name)
                self._tool_to_session[t.name] = session
                self._tools.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    },
                })

    async def _close_all(self):
        for stack in self._stacks:
            await stack.aclose()

    async def chat(self, user_message: str, system_message: str | None = None) -> str:
        if system_message is not None:
            self.messages.append({'role': 'system', 'content': system_message})

        self.messages.append(
            {"role": "user", "content": user_message}
        )

        if self.verbose:
            print(f"{'='*100}")
            print(f"User: {user_message}")
            print(f"Tools available: {self._tool_names}\n")

        tool_calls_made = 0
        last_content = ""
        while True:
            response = await self.backend.complete(self.messages, self._tools)
            self.responses.append(response)
            self.metrics_log.append(response.metrics)

            if not response.tool_calls:
                self.messages.append(
                    {"role": "assistant", "content": response.content})
                if self.verbose:
                    print(f"{'-'*100}")
                    print(f"Assistant: {response.content}")
                return response.content

            last_content = response.content or ""
            self.messages.append(
                self.backend.make_assistant_tool_call_message(response))

            for tool_call in response.tool_calls:
                if self.max_tool_calls is not None and tool_calls_made >= self.max_tool_calls:
                    self.tool_call_limit_exceeded = True
                    if self.verbose:
                        print(
                            f"[WARNING] Tool call limit of {self.max_tool_calls} reached; returning last content.")
                    return last_content

                if self.verbose:
                    print(
                        f"→ Tool call: '{tool_call.name}' args={tool_call.arguments}")

                result = await self._tool_to_session[tool_call.name].call_tool(
                    tool_call.name, tool_call.arguments
                )
                result_text = str(result.content)
                tool_calls_made += 1

                if self.verbose:
                    preview = result_text[:300] + \
                        ("..." if len(result_text) > 300 else "")
                    print(f"← Result: {preview}\n")

                self.messages.append(
                    self.backend.make_tool_result_message(
                        tool_call, result_text)
                )

    def reset(self):
        self.messages = []
        self.metrics_log = []
        self.tool_call_limit_exceeded = False

    @property
    def cumulative_metrics(self) -> dict:
        """Sum all logged ResponseMetrics into a single dict; missing fields are None."""
        def _sum(vals):
            non_null = [v for v in vals if v is not None]
            return sum(non_null) if non_null else None

        inp = _sum(m.input_tokens for m in self.metrics_log)
        out = _sum(m.output_tokens for m in self.metrics_log)
        tot = _sum(m.total_tokens for m in self.metrics_log)
        dur = _sum(m.duration_s for m in self.metrics_log)
        cr = _sum(m.cache_read_tokens for m in self.metrics_log)
        cw = _sum(m.cache_write_tokens for m in self.metrics_log)
        tps = (out / dur) if (out and dur) else None

        return {
            "model":                     self.backend.model,
            "api_calls":                 len(self.metrics_log),
            "input_tokens":              inp,
            "output_tokens":             out,
            "total_tokens":              tot,
            "duration_s":                round(dur, 3) if dur is not None else None,
            "tokens_per_s":              round(tps, 1) if tps is not None else None,
            "cache_read_tokens":         cr,
            "cache_write_tokens":        cw,
            "tool_call_limit_exceeded":  self.tool_call_limit_exceeded,
        }

    async def __aenter__(self):
        raise NotImplementedError

    async def __aexit__(self, *args):
        raise NotImplementedError


# ── Concrete agents ───────────────────────────────────────────────────────────

class HttpAgent(AgentBase):
    def __init__(self, url: str, backend: Backend, verbose: bool = True, max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS):
        super().__init__(backend, verbose, max_tool_calls)
        self.url = url

    async def __aenter__(self):
        await self._add_http_session(self.url)
        await self._init_tools()
        return self

    async def __aexit__(self, *args):
        await self._close_all()


class StdioAgent(AgentBase):
    def __init__(self, server_params: StdioServerParameters, backend: Backend, verbose: bool = True, max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS):
        super().__init__(backend, verbose, max_tool_calls)
        self.server_params = server_params

    async def __aenter__(self):
        await self._add_stdio_session(self.server_params)
        await self._init_tools()
        return self

    async def __aexit__(self, *args):
        await self._close_all()


class MultiAgent(AgentBase):
    def __init__(
        self,
        transports: list[str | StdioServerParameters],
        backend: Backend,
        verbose: bool = True,
        max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS,
    ):
        super().__init__(backend, verbose, max_tool_calls)
        self.transports = transports

    async def __aenter__(self):
        for transport in self.transports:
            if isinstance(transport, str):
                await self._add_http_session(transport)
            else:
                await self._add_stdio_session(transport)
        await self._init_tools()
        return self

    async def __aexit__(self, *args):
        await self._close_all()
