import requests
from requests.adapters import HTTPAdapter
import time
import json
import os
import sys
import re
import logging
import threading
from dotenv import load_dotenv

from modellink import relay

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("Omniilink")

# ==========================================
# PyInstaller path resolution
# ==========================================
def _base_path():
    """Return the base directory for resource files.
    When frozen by PyInstaller, sys._MEIPASS points to the temp extraction folder.
    When running normally, it's the directory containing this script's parent."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _resource_path(relative_path):
    """Resolve a path relative to the app root.
    When frozen, files are next to the exe. When dev, files are relative to project root."""
    base = _base_path()
    return os.path.join(base, relative_path)

# Load .env from the correct location. Explicit process environment always
# wins over the file (override=False) — otherwise tests and container env
# could never override local .env values.
env_path = _resource_path(".env")
if os.path.isfile(env_path):
    load_dotenv(dotenv_path=env_path, override=False)
else:
    load_dotenv(override=False)

MODEL_NAME = "omniilink"

MODEL_RATE_LIMITS = {
    "gemini-3.6-flash":             {"rpm": 10,  "tpm": 250000},
    "gemini-3.5-flash":             {"rpm": 10,  "tpm": 250000},
    "gemini-3.5-flash-lite":        {"rpm": 15,  "tpm": 250000},
    "gemini-3.1-flash-lite":        {"rpm": 15,  "tpm": 250000},
    "gemini-3-flash-preview":       {"rpm": 10,  "tpm": 250000},
    "gemini-3.1-pro-preview":       {"rpm": 5,   "tpm": 250000},
    "gemini-flash-latest":          {"rpm": 10,  "tpm": 250000},
    "gemini-flash-lite-latest":     {"rpm": 15,  "tpm": 250000},
    "gemma-4-26b-a4b-it":          {"rpm": 30,  "tpm": 15000},
    "gpt-4o":                       {"rpm": 5,   "tpm": 150000},
    "gpt-4o-mini":                  {"rpm": 15,  "tpm": 200000},
    "gpt-4-turbo":                  {"rpm": 5,   "tpm": 100000},
    "gpt-3.5-turbo":                {"rpm": 15,  "tpm": 200000},
    "claude-sonnet-4-20250514":     {"rpm": 5,   "tpm": 80000},
    "claude-3-5-sonnet-20241022":   {"rpm": 5,   "tpm": 80000},
    "claude-3-5-haiku-20241022":    {"rpm": 10,  "tpm": 100000},
    "claude-3-haiku-20240307":      {"rpm": 10,  "tpm": 100000},
    "command-r-plus":               {"rpm": 10,  "tpm": 100000},
    "command-r":                    {"rpm": 20,  "tpm": 100000},
    "mistral-large-latest":         {"rpm": 10,  "tpm": 100000},
    "mistral-small-latest":         {"rpm": 15,  "tpm": 150000},
    "open-mistral-nemo":            {"rpm": 15,  "tpm": 150000},
    "llama-3.3-70b-versatile":      {"rpm": 15,  "tpm": 100000},
    "llama-3.1-8b-instant":         {"rpm": 30,  "tpm": 150000},
    "openai/gpt-oss-120b":          {"rpm": 10,  "tpm": 100000},
    "openai/gpt-oss-20b":           {"rpm": 15,  "tpm": 150000},
    "qwen/qwen3.6-27b":             {"rpm": 15,  "tpm": 100000},
    "qwen/qwen3.8-27b":             {"rpm": 15,  "tpm": 100000},
    "allam-2-7b":                   {"rpm": 30,  "tpm": 150000},
    "deepseek-chat":                {"rpm": 10,  "tpm": 100000},
    "deepseek-reasoner":            {"rpm": 5,   "tpm": 50000},
    "grok-3":                       {"rpm": 5,   "tpm": 50000},
    "grok-3-mini":                  {"rpm": 10,  "tpm": 100000},
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": {"rpm": 10, "tpm": 100000},
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": {"rpm": 20, "tpm": 100000},
    "meta-llama/Meta-Llama-3-70B-Instruct-Turbo": {"rpm": 5, "tpm": 50000},
    "google/gemma-2-27b-it":        {"rpm": 15,  "tpm": 100000},
    "qwen/qwen-2.5-72b-instruct":  {"rpm": 10,  "tpm": 100000},
}

def _default_ollama_base_url() -> str:
    """Server-wide Ollama address. OMNIILINK_OLLAMA_URL (set from the admin
    panel) wins; the legacy OLLAMA_BASE_URL still works; else localhost."""
    return (os.environ.get("OMNIILINK_OLLAMA_URL")
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434").strip().rstrip("/")


OLLAMA_BASE_URL = _default_ollama_base_url()
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")


def set_ollama_base_url(url: str) -> str:
    """Point the server-wide Ollama fallback at `url` at runtime (no restart).

    Read dynamically by scan_ollama_models() and the fallback, so the admin
    panel takes effect immediately."""
    global OLLAMA_BASE_URL
    OLLAMA_BASE_URL = (url or "").strip().rstrip("/") or _default_ollama_base_url()
    return OLLAMA_BASE_URL

def _load_settings_language():
    settings_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r") as f:
                s = json.load(f)
                if "language" in s:
                    return s["language"]
        except: pass
    return os.environ.get("LANGUAGE", "English")

LANGUAGE = _load_settings_language()


def _content_text(content):
    """Plain text out of a message body, whether str or OpenAI-style parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) for p in content
                          if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _content_images(content):
    """(mime, b64) pairs out of OpenAI-style image_url parts (data: URLs)."""
    out = []
    if not isinstance(content, list):
        return out
    for p in content:
        if not isinstance(p, dict):
            continue
        url = ((p.get("image_url") or {}).get("url", "")) if isinstance(p.get("image_url"), dict) else ""
        m = re.match(r"^data:([\w\-+./]+);base64,([A-Za-z0-9+/=]+)$", url or "")
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def _anthropic_parts(content):
    """Anthropic content blocks translated from OpenAI-style parts."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    parts = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"type": "text", "text": str(p.get("text", ""))})
    for mime, b64 in _content_images(content):
        parts.append({"type": "image",
                      "source": {"type": "base64", "media_type": mime, "data": b64}})
    return parts or [{"type": "text", "text": ""}]


def _gemini_parts(content):
    """Gemini content parts translated from OpenAI-style parts."""
    if isinstance(content, str):
        return [{"text": content}]
    parts = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"text": str(p.get("text", ""))})
    for mime, b64 in _content_images(content):
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    return parts or [{"text": ""}]


def set_language(lang):
    global LANGUAGE
    LANGUAGE = lang


def get_language():
    return LANGUAGE

PROVIDERS = {
    "gemini": {
        "env_prefix": "GEMINI_API_KEY",
        "api_base": "https://generativelanguage.googleapis.com/v1beta",
        "models": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-flash-latest", "gemini-flash-lite-latest"],
        "headers": lambda key: {"Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: (
            lambda _sys: {
                "contents": [{"role": "user" if m["role"] == "user" else "model", "parts": _gemini_parts(m["content"])} for m in msgs if m["role"] != "system"],
                **({"systemInstruction": {"parts": [{"text": _sys}]} } if _sys else {}),
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4000}
            }
        )(_content_text(next((m["content"] for m in msgs if m.get("role") == "system"), ""))),
        "stream_url": lambda model, key: f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={key}",
        "url": lambda model, key: f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
    },
    "openai": {
        "env_prefix": "OPENAI_API_KEY",
        "api_base": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: f"https://api.openai.com/v1/chat/completions",
        "url": lambda model, key: f"https://api.openai.com/v1/chat/completions",
    },
    "anthropic": {
        "env_prefix": "ANTHROPIC_API_KEY",
        "api_base": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
        "headers": lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": [{"role": m["role"], "content": _anthropic_parts(m["content"])} for m in msgs if m["role"] != "system"],
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://api.anthropic.com/v1/messages",
        "url": lambda model, key: "https://api.anthropic.com/v1/messages",
    },
    "openrouter": {
        "env_prefix": "OPENROUTER_API_KEY",
        "api_base": "https://openrouter.ai/api/v1",
        "models": ["anthropic/claude-3.5-sonnet", "openai/gpt-4o", "google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-72b-instruct"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "HTTP-Referer": "https://modellink.local"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://openrouter.ai/api/v1/chat/completions",
        "url": lambda model, key: "https://openrouter.ai/api/v1/chat/completions",
    },
    "groq": {
        "env_prefix": "GROQ_API_KEY",
        "api_base": "https://api.groq.com/openai/v1",
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://api.groq.com/openai/v1/chat/completions",
        "url": lambda model, key: "https://api.groq.com/openai/v1/chat/completions",
    },
    "mistral": {
        "env_prefix": "MISTRAL_API_KEY",
        "api_base": "https://api.mistral.ai/v1",
        "models": ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://api.mistral.ai/v1/chat/completions",
        "url": lambda model, key: "https://api.mistral.ai/v1/chat/completions",
    },
    "together": {
        "env_prefix": "TOGETHER_API_KEY",
        "api_base": "https://api.together.xyz/v1",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "google/gemma-2-27b-it", "qwen/qwen-2.5-72b-instruct"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://api.together.xyz/v1/chat/completions",
        "url": lambda model, key: "https://api.together.xyz/v1/chat/completions",
    },
    "deepseek": {
        "env_prefix": "DEEPSEEK_API_KEY",
        "api_base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://api.deepseek.com/v1/chat/completions",
        "url": lambda model, key: "https://api.deepseek.com/v1/chat/completions",
    },
    "cohere": {
        "env_prefix": "COHERE_API_KEY",
        "api_base": "https://api.cohere.ai/v2",
        "models": ["command-r-plus", "command-r"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
        },
        "stream_url": lambda model, key: "https://api.cohere.ai/v2/chat",
        "url": lambda model, key: "https://api.cohere.ai/v2/chat",
    },
    "opencode": {
        "env_prefix": "OPENCODE_API_KEY",
        "api_base": "https://opencode.ai/zen/v1",
        "models": ["opencode-default"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": "opencode-default",
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: os.environ.get("OPENCODE_API_URL", "https://opencode.ai/zen/v1/chat/completions"),
        "url": lambda model, key: os.environ.get("OPENCODE_API_URL", "https://opencode.ai/zen/v1/chat/completions"),
    },
    "opencode-go": {
        "env_prefix": "OPENCODE_GO_API_KEY",
        "api_base": "https://opencode.ai/zen/go/v1",
        "models": [
            "Ling 3.0 Flash Fin Free", "Ling 3.1 Flash Free", "LongCat 2.5 Preview Free",
            "MiMo-V2.6-Flash Free", "Muse Spark 1.3 Free", "Nemotron 3.5 Lightning Free",
            "DeepSeek V4 Pro (New)", "DeepSeek V4.1 Flash", "GLM-5.3", "GLM-5.3-Flash",
            "GPT-6 Luna", "Grok 4.7", "Hy4 preview", "Kimi K2.7 Code", "Kimi K3",
            "MiMo V2.5", "MiMo V2.5 Pro", "MiMo-V2.6-Pro", "MiniMax-M3",
            "Muse Spark 1.3 Contributor", "Qwen3.7 Plus", "Qwen3.8 Flash", "Qwen3.8 Max",
        ],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": model,
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: "https://opencode.ai/zen/go/v1/chat/completions",
        "url": lambda model, key: "https://opencode.ai/zen/go/v1/chat/completions",
    },
}


# ==========================================
# Web search tool (function calling)
# ==========================================
def web_search(query, limit=8):
    """Free web search via DuckDuckGo HTML (no API key needed).

    Returns a list of {"title","url","snippet"}. Best-effort: returns []
    on any failure so a search outage never breaks a chat turn.
    """
    import re
    from html import unescape
    results = []
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10,
        )
        if resp.status_code != 200:
            return results
        html = resp.text
        titles = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            href = m.group(1)
            title = unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if title:
                titles.append((title, href))
        snippets = [unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
                    for m in re.finditer(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)]
        for i, (title, href) in enumerate(titles[:limit]):
            results.append({
                "title": title,
                "url": href,
                "snippet": snippets[i] if i < len(snippets) else "",
            })
    except Exception:
        pass
    return results


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information. Use when the answer may require up-to-date facts, recent events, or information beyond your training data.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query"}},
            "required": ["query"],
        },
    },
}

_OPENAI_TOOL_PROVIDERS = {"openai", "groq", "mistral", "together", "deepseek", "openrouter", "opencode", "opencode-go", "ollama"}


def _add_tools_to_body(provider_name, body, tools):
    """Attach tool definitions to a provider payload in its native format."""
    if not tools or not isinstance(body, dict):
        return
    if provider_name in _OPENAI_TOOL_PROVIDERS:
        body["tools"] = tools
        body.setdefault("tool_choice", "auto")
    elif provider_name == "anthropic":
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "input_schema": t["function"]["parameters"]}
            for t in tools
        ]
    elif provider_name == "gemini":
        body["tools"] = [{"functionDeclarations": [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "parameters": t["function"]["parameters"]}
            for t in tools
        ]}]
    elif provider_name == "cohere":
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "parameter_definitions": t["function"]["parameters"].get("properties", {})}
            for t in tools
        ]


def _extract_tool_calls(provider_name, response):
    """Pull tool calls out of a response.

    _collect_response normalises every provider to the OpenAI shape, so check
    that first; fall back to the raw anthropic/gemini payload shapes.
    """
    calls = []
    if not response or not isinstance(response, dict):
        return calls
    choices = response.get("choices", []) or []
    if choices:
        msg = choices[0].get("message", {}) or {}
        for tc in msg.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            calls.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "args": args or {}})
        if calls:
            return calls
    if provider_name == "anthropic":
        for block in response.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                              "args": block.get("input", {}) or {}})
        return calls
    if provider_name == "gemini":
        for cand in response.get("candidates", []) or []:
            for part in (cand.get("content", {}) or {}).get("parts", []) or []:
                fn = part.get("functionCall") or part.get("function_call")
                if fn:
                    calls.append({"id": "", "name": fn.get("name", ""), "args": fn.get("args", {}) or {}})
        return calls
    return calls


def _response_to_openai_message(response):
    """Best-effort conversion of a provider response into an OpenAI assistant message."""
    if not response or not isinstance(response, dict):
        return {"role": "assistant", "content": ""}
    choices = response.get("choices", []) or []
    if choices:
        msg = choices[0].get("message", {}) or {}
        out = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        return out
    return {"role": "assistant", "content": ""}


def _execute_tool_call(name, args):
    """Run a tool call and return a readable text result for the model."""
    if name == "web_search":
        query = str((args or {}).get("query", "")).strip()
        if not query:
            return "Error: empty query"
        try:
            results = web_search(query)
        except Exception as e:
            return f"Error: {e}"
        if not results:
            return f"No web results found for '{query}'."
        lines = [f"Web results for '{query}':"]
        for r in results[:8]:
            title = r.get("title", "")
            snippet = (r.get("snippet", "") or "")[:200]
            lines.append(f"- {title}: {snippet}")
        return "\n".join(lines)
    return f"Error: unknown tool: {name}"


def run_tool_loop(messages, model="modellink", temperature=0.7, max_tokens=4000,
                  user_keys=None, opts=None, max_iters=2):
    """Run a tool-use loop: call the model, execute any tool calls, feed back.

    Tool results are fed back as a user message so every provider understands
    them (gemini/anthropic don't accept OpenAI-style tool messages). If the
    loop exhausts without a text answer, fall back to a direct generation
    (no tools) so the model still replies. Returns (response, used_model).
    """
    msgs = list(messages)
    last_response = None
    last_used = model
    for _ in range(max_iters):
        response, used_model = generate(
            messages=msgs, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=False, user_keys=user_keys, opts=opts,
        )
        last_response = response
        last_used = used_model
        provider = used_model.split("/", 1)[0] if "/" in used_model else "unknown"
        calls = _extract_tool_calls(provider, response)
        if not calls:
            return response, used_model
        result_texts = []
        for call in calls:
            result = _execute_tool_call(call["name"], call["args"])
            result_texts.append(result)
        # Keep the conversation alternating (user/assistant) — consecutive user
        # messages make the model re-call the tool until the loop exhausts.
        msgs.append({"role": "assistant", "content": ""})
        msgs.append({"role": "user", "content": "Web search results:\n" + "\n\n".join(result_texts)})
    # The loop exhausted without a text answer — reply directly (no tools).
    content = ""
    try:
        content = last_response["choices"][0]["message"].get("content") or ""
    except Exception:
        pass
    if not content.strip():
        clean_opts = {k: v for k, v in (opts or {}).items() if k != "tools"}
        response, used_model = generate(
            messages=msgs, model=model, temperature=temperature,
            max_tokens=max_tokens, stream=False, user_keys=user_keys, opts=clean_opts,
        )
        return response, used_model
    return last_response, last_used


# ==========================================
# Caller-supplied generation options
# (temperature / max_tokens / effort / thinking)
# ==========================================
EFFORT_LEVELS = ("minimum", "low", "medium", "high", "xhigh", "xxhigh", "max", "auto")
# Reasoning budget in tokens, per level, for budget-based providers
# (Gemini thinkingConfig, Anthropic thinking).
THINKING_BUDGET = {"minimum": 0, "low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "xxhigh": 65536, "max": 131072}
# OpenAI-style reasoning_effort only accepts these literal strings.
OPENAI_EFFORT = {"minimum": "minimal", "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "xxhigh": "high", "max": "high"}
# Providers whose reasoning knob is a token budget rather than an effort string.
BUDGET_PROVIDERS = {"gemini", "anthropic"}


def normalize_level(value, default="auto"):
    """Map any caller value onto a known level (invalid/missing -> default)."""
    if not isinstance(value, str):
        return default
    v = value.strip().lower()
    return v if v in EFFORT_LEVELS else default


def resolve_level(effort, thinking):
    """Single effective level.

    `thinking` is the finer-grained knob: when the user picks anything other
    than auto it wins; otherwise the `effort` level is used. `auto` for both
    means "do not touch the provider default" (byte-identical payloads).
    """
    th = normalize_level(thinking)
    if th != "auto":
        return th
    return normalize_level(effort)


def apply_request_opts(provider_name, body, opts):
    """Merge caller-supplied generation options into a provider payload.

    Returns (body, changed). `changed` is True only when the payload differs
    from the stock one, so the caller can retry once with a plain body if the
    provider rejects an optional knob (e.g. an unsupported reasoning_effort).
    Stock values (temperature 0.7 / max_tokens 4000) are NEVER injected into
    a provider that did not carry them: with default options every payload is
    byte-identical to what the code sent before.
    """
    if not opts or not isinstance(body, dict):
        return body, False
    before = json.dumps(body, sort_keys=True, default=str)
    is_gemini = provider_name == "gemini"
    gc = body.get("generationConfig") if is_gemini else None

    temp = opts.get("temperature")
    if isinstance(temp, (int, float)) and not isinstance(temp, bool):
        t = max(0.0, min(2.0, float(temp)))
        # Keep stock 0.7 payloads untouched; only write when the caller asked
        # for something else, or the provider already carries the knob.
        if isinstance(gc, dict):
            if t != 0.7 or "temperature" in gc:
                gc["temperature"] = t
        elif t != 0.7 or "temperature" in body:
            body["temperature"] = t

    mt = opts.get("max_tokens")
    if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0:
        mt = min(int(mt), 32000)
        if isinstance(gc, dict):
            if mt != 4000 or "maxOutputTokens" in gc:
                gc["maxOutputTokens"] = mt
        elif mt != 4000 or "max_tokens" in body:
            body["max_tokens"] = mt

    level = resolve_level(opts.get("effort"), opts.get("thinking"))
    if level != "auto":
        budget = THINKING_BUDGET.get(level, 0)
        if provider_name == "gemini":
            if isinstance(gc, dict):
                cfg = gc.get("thinkingConfig")
                if not isinstance(cfg, dict):
                    cfg = {}
                    gc["thinkingConfig"] = cfg
                cfg["thinkingBudget"] = int(budget)
        elif provider_name == "anthropic":
            if budget > 0:
                body["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}
                # Anthropic hard-requires max_tokens > thinking budget.
                body["max_tokens"] = max(int(body.get("max_tokens") or 4000), int(budget) + 1024)
            # "minimum" leaves thinking off, which is Anthropic's default.
        else:
            effort_str = OPENAI_EFFORT.get(level)
            if effort_str:
                body["reasoning_effort"] = effort_str
    rm = (opts or {}).get("response_modalities")
    if rm and isinstance(gc, dict):
        gc["responseModalities"] = list(rm)
    return body, json.dumps(body, sort_keys=True, default=str) != before


# ==========================================
# Provider health (per key scope)
# ==========================================
# Records the outcome of every model attempt so the sidebar can colour a
# provider dot: red when every model tried on that key failed, orange when
# only some failed, green otherwise. Scoped by `opts["health_scope"]` so one
# user's dead key never tints another user's dot (admin uses "server").
_health_lock = threading.Lock()
_model_health: dict = {}


def record_model_outcome(scope, provider, model, outcome, detail=""):
    if not isinstance(provider, str) or not provider:
        return
    if not isinstance(model, str) or not model:
        return
    scope = scope or "server"
    now = time.time()
    with _health_lock:
        prov = _model_health.setdefault(scope, {}).setdefault(provider, {})
        entry = prov.setdefault(model, {"ok": 0, "fail": 0, "last": "", "ts": 0.0, "detail": ""})
        if outcome == "ok":
            entry["ok"] += 1
            entry["last"] = "ok"
            entry["detail"] = ""
        elif outcome == "fail":
            entry["fail"] += 1
            entry["last"] = "fail"
            if detail:
                entry["detail"] = str(detail)[:120]
        elif outcome == "rate":
            # Rate limited: the key itself is fine, only refresh the stamp so
            # a model that never got a real answer stays out of the count.
            if entry["last"] not in ("ok", "fail"):
                entry["last"] = "rate"
        entry["ts"] = now
        if len(prov) > 64:
            for m in list(prov):
                if m != model and now - prov[m]["ts"] > 86400:
                    prov.pop(m, None)


def provider_health(scope):
    """Roll per-model outcomes up to one status per provider."""
    scope = scope or "server"
    out = {}
    with _health_lock:
        provs = _model_health.get(scope, {})
        for provider, models in provs.items():
            tried = ok = failed = 0
            detail = ""
            newest = 0.0
            for model, st in models.items():
                if st["last"] not in ("ok", "fail"):
                    continue
                tried += 1
                newest = max(newest, st["ts"])
                if st["last"] == "ok":
                    ok += 1
                else:
                    failed += 1
                    if not detail and st.get("detail"):
                        detail = f"{model}: {st['detail']}"
            if tried == 0:
                state = "ok"
            elif failed == tried:
                state = "fail"
            elif failed > 0:
                state = "warn"
            else:
                state = "ok"
            out[provider] = {
                "state": state,
                "tried": tried,
                "ok": ok,
                "fail": failed,
                "ts": newest,
                "detail": detail,
            }
    return out


class RateLimiter:
    def __init__(self):
        self._request_times: dict = {}
        self._token_counts: dict = {}
        self._cooldowns: dict = {}
        self._lock = threading.Lock()

    def can_request(self, model: str) -> bool:
        with self._lock:
            now = time.time()
            if model in self._cooldowns and now < self._cooldowns[model]:
                return False
            limits = MODEL_RATE_LIMITS.get(model, {"rpm": 10, "tpm": 100000})
            times = self._request_times.get(model, [])
            recent = [t for t in times if now - t < 60]
            if len(recent) >= limits["rpm"]:
                return False
            return True

    def record_request(self, model: str, tokens: int = 0):
        with self._lock:
            now = time.time()
            if model not in self._request_times:
                self._request_times[model] = []
            self._request_times[model].append(now)
            self._request_times[model] = [t for t in self._request_times[model] if now - t < 60]
            if tokens > 0:
                if model not in self._token_counts:
                    self._token_counts[model] = []
                self._token_counts[model].append((now, tokens))
                self._token_counts[model] = [(t, tk) for t, tk in self._token_counts[model] if now - t < 60]

    def set_cooldown(self, model: str, seconds: int):
        with self._lock:
            self._cooldowns[model] = time.time() + seconds
            logger.warning(f"[COOLDOWN] {model} set for {seconds}s")

    def get_status(self) -> dict:
        status = {}
        with self._lock:
            now = time.time()
            for key, times in self._request_times.items():
                recent = [t for t in times if now - t < 60]
                status[key] = f"{len(recent)} RPM"
        return status


def load_all_keys():
    load_dotenv(override=False)
    provider_keys = {}
    placeholder_prefixes = ("your_", "sk-test", "placeholder", "xxx", "changeme")
    for env_key, value in os.environ.items():
        if "_API_KEY" in env_key and value and len(value) > 10:
            if not any(value.lower().startswith(p) for p in placeholder_prefixes):
                for prov_name, prov_cfg in PROVIDERS.items():
                    if env_key.startswith(prov_cfg["env_prefix"]):
                        if prov_name not in provider_keys:
                            provider_keys[prov_name] = []
                        provider_keys[prov_name].append({
                            "env_key": env_key,
                            "key": value,
                        })
                        break
    return provider_keys


def scan_ollama_models(base_url=None, agent_id=None, force=False):
    """List Ollama models. base_url overrides OLLAMA_BASE_URL; agent_id scans
    a paired device through the relay instead of the server's own Ollama.

    The relay variant never raises on soft refreshes: a stale cache (or an
    empty list) keeps the model menu stable while the device is offline. With
    force=True the relay error propagates so status probes can show it."""
    if agent_id is not None:
        return _relay_scan_models(agent_id, force=force)
    url = (base_url or "").strip().rstrip("/") or OLLAMA_BASE_URL
    try:
        resp = requests.get(f"{url}/api/tags", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


def get_backup_model():
    return OLLAMA_MODEL


# Vision-capability cache: {tag: {"vision": bool}}, refreshed from /api/show.
# Basis for the vision flags in /api/discover so the UI only offers image
# input on models that can actually see. Short TTL so freshly pulled models
# show up without a restart.
_ollama_caps = {"ts": 0.0, "base": "", "caps": {}}
OLLAMA_CAPS_TTL = 300


# Per-device relay scan cache: {agent_id: {"ts","caps_ts","tags","vision"}}.
# Tags refresh fast (menu + the per-request installed check); vision follows
# the same 300s TTL as the direct scan. Offline devices keep serving the
# last-known list so their menu entries stay put.
_relay_scan = {}
RELAY_SCAN_TTL = 60


def _relay_scan_models(agent_id, force=False):
    aid = str(agent_id)
    now = time.time()
    cached = _relay_scan.get(aid) or {}
    if (not force and cached.get("ts")
            and now - cached["ts"] < RELAY_SCAN_TTL):
        return list(cached.get("tags") or [])
    try:
        resp = relay.open_response(aid, "/api/tags", None,
                                   read_timeout=20.0, first_timeout=12.0)
        try:
            if resp.status_code != 200:
                raise relay.RelayError(f"HTTP {resp.status_code}")
            data = resp.json()
            tags = [m.get("name") for m in data.get("models", [])
                    if isinstance(m, dict) and m.get("name")]
        finally:
            resp.close()
    except (relay.RelayError, relay.RelayTimeout) as e:
        if force:
            raise
        logger.info(f"[RELAY] tag scan for device {aid} failed: {e}")
        return list(cached.get("tags") or [])
    _relay_scan[aid] = dict(cached, ts=now, tags=tags)
    return list(tags)


def ollama_model_capabilities(force=False, agent_id=None):
    """{tag: {"vision": bool}} for installed models (cached, best-effort).

    With agent_id, capabilities are probed on that paired device through the
    relay (same TTL); soft refreshes fall back to the cache when the device
    is unreachable, force=True propagates the error."""
    if agent_id is not None:
        return _relay_capabilities(agent_id, force=force)
    base = OLLAMA_BASE_URL
    now = time.time()
    if (not force and _ollama_caps["base"] == base
            and now - _ollama_caps["ts"] < OLLAMA_CAPS_TTL):
        return _ollama_caps["caps"]
    caps = {}
    for tag in scan_ollama_models():
        caps[tag] = {"vision": False}
        try:
            r = requests.post(f"{base}/api/show", json={"name": tag}, timeout=5)
            if r.status_code == 200:
                info = r.json()
                have = info.get("capabilities") or []
                caps[tag]["vision"] = any(str(x).lower() == "vision" for x in have)
        except Exception:
            pass
    _ollama_caps.update(ts=now, base=base, caps=caps)
    return caps


def _relay_capabilities(agent_id, force=False):
    aid = str(agent_id)
    now = time.time()
    cached = _relay_scan.get(aid) or {}
    if (not force and cached.get("caps_ts")
            and now - cached["caps_ts"] < OLLAMA_CAPS_TTL):
        return dict(cached.get("vision") or {})
    caps = {}
    try:
        for tag in _relay_scan_models(aid, force=force):
            caps[tag] = {"vision": False}
            try:
                r = relay.open_response(aid, "/api/show", {"name": tag},
                                        read_timeout=20.0, first_timeout=12.0)
                try:
                    if r.status_code == 200:
                        info = r.json()
                        have = info.get("capabilities") or []
                        caps[tag]["vision"] = any(
                            str(x).lower() == "vision" for x in have)
                finally:
                    r.close()
            except Exception:
                pass
    except (relay.RelayError, relay.RelayTimeout):
        if force:
            raise
        return dict(cached.get("vision") or {})
    _relay_scan[aid] = dict(cached, caps_ts=now, vision=caps)
    return caps


def _ollama_native_messages(messages):
    """OpenAI-style messages -> Ollama /api/chat messages.

    Flattens text parts to a plain string and moves data:-URL image parts
    into the native `images` list (raw base64, no data: prefix). Remote
    http(s) image URLs can't be fetched by Ollama, so they are folded into
    the text as a reference instead of being dropped silently.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        if role not in ("system", "user", "assistant"):
            role = "user"
        entry = {"role": role, "content": _content_text(m.get("content", ""))}
        imgs = [b64 for _mime, b64 in _content_images(m.get("content", ""))]
        if imgs and role in ("user", "system"):
            entry["images"] = imgs
        refs = []
        if isinstance(m.get("content"), list):
            for p in m["content"]:
                if not isinstance(p, dict):
                    continue
                iu = p.get("image_url")
                url = (iu.get("url", "") if isinstance(iu, dict) else "")
                if url and not url.startswith("data:"):
                    refs.append(url)
        if refs:
            entry["content"] = (entry["content"] + "\n[Attached image URLs Ollama cannot fetch: "
                                + ", ".join(refs[:4]) + "]").strip()
        out.append(entry)
    return out or [{"role": "user", "content": ""}]


def _ollama_error_text(resp):
    """Best-effort readable message out of an Ollama error response."""
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str) and err.strip():
            return err.strip()[:300]
    try:
        txt = (resp.text or "").strip()
    except Exception:
        txt = ""
    if txt:
        return txt[:300]
    return f"HTTP {getattr(resp, 'status_code', '?')}"


def _relay_guard(gen, resp):
    """Wrap a relay-backed stream generator so the relay is closed when the
    consumer finishes, errors, or abandons it (client disconnect)."""
    try:
        for chunk in gen:
            yield chunk
    finally:
        try:
            resp.close()
        except Exception:
            pass


def generate(messages, model="modellink", temperature=0.7, max_tokens=4000, stream=True, user_keys=None, opts=None):
    global lb
    return lb.generate(messages, model, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)


# ==========================================
# Task Classification & Smart Routing
# ==========================================

# Keywords/patterns that indicate complex tasks
CODING_KEYWORDS = re.compile(
    r'\b(code|program|function|class|debug|error|bug|implement|refactor|compile|'
    r'script|algorithm|api|database|sql|html|css|javascript|python|java|c\+\+|'
    r'git|deploy|regex|parse|json|yaml|docker|kubernetes|async|thread|'
    r'write.*code|code.*write|create.*file|file.*create|build|test|lint|'
    r'syntax|import|export|module|package|dependency|npm|pip|cargo|'
    r'fix|broken|crash|stack.*trace|exception|traceback|debugging|'
    r'review|explain.*code|code.*review|optimize|performance)\b',
    re.IGNORECASE
)

ANALYSIS_KEYWORDS = re.compile(
    r'\b(analyze|analysis|compare|contrast|evaluate|assess|critique|review|'
    r'pros.*cons|advantages|disadvantages|trade.?offs|tradeoff|'
    r'research|investigate|study|examine|explain.*why|why.*does|'
    r'summarize|summary|tldr|overview|deep.?dive|comprehensive|'
    r'math|calculate|equation|formula|proof|theorem|statistics|probability|'
    r'strategy|plan|architect|design.*system|system.*design|'
    r'step.*by.*step|detailed|thorough|in.?depth)\b',
    re.IGNORECASE
)

WRITING_KEYWORDS = re.compile(
    r'\b(write|draft|essay|article|blog|story|novel|poem|'
    r'email|letter|proposal|report|document|documentation|readme|'
    r'creative|narrative|fiction|nonfiction|copywriting|marketing|'
    r'long.*form|detailed.*response|comprehensive.*answer|'
    r'professional.*email|formal|business.*writing)\b',
    re.IGNORECASE
)

COMPLEXITY_BOOSTERS = re.compile(
    r'\b(multiple|several|all|every|each|list.*all|enumerate|'
    r'comprehensive|thorough|detailed|complete|full|entire|'
    r'complex|complicated|advanced|sophisticated|'
    r'with.*example|example.*with|including|such.*as.*for|'
    r'and.*also|both.*and|not.*only.*but|either.*or)\b',
    re.IGNORECASE
)

SIMPLE_INDICATORS = re.compile(
    r'^(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|yes|no|sure|'
    r'cool|nice|great|good|bad|what|how|when|where|who|'
    r'tldr|tl;dr|\?{1,3}|!{1,3}|ok\?|right\?|sure\?)$',
    re.IGNORECASE
)


def classify_task(messages):
    """Classify the task complexity from conversation messages.
    Returns: 'simple', 'medium', or 'complex'
    """
    if not messages:
        return 'simple'

    last_user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "")
            break

    if not last_user_msg:
        return 'simple'

    text = last_user_msg.strip()
    word_count = len(text.split())

    if SIMPLE_INDICATORS.match(text):
        return 'simple'

    score = 0

    has_code_block = '```' in text
    if has_code_block:
        score += 2

    coding_hits = CODING_KEYWORDS.findall(text)
    if coding_hits:
        if word_count > 8:
            score += 3
        elif len(coding_hits) >= 2:
            score += 2
        else:
            score += 1

    if ANALYSIS_KEYWORDS.search(text):
        score += 2
    if WRITING_KEYWORDS.search(text):
        score += 1
    if COMPLEXITY_BOOSTERS.search(text):
        score += 1

    if word_count > 100:
        score += 2
    elif word_count > 50:
        score += 1

    if text.count('\n') > 5:
        score += 1

    if score >= 3:
        return 'complex'
    elif score >= 1:
        return 'medium'
    return 'simple'


# Model routing tiers: (provider, model) ordered by preference
# Falls through to next tier if first choice fails
MODEL_TIERS = {
    'simple': [
        ('gemini', 'gemini-3.6-flash'),
        ('gemini', 'gemini-3.5-flash-lite'),
        ('gemini', 'gemini-flash-latest'),
        ('groq', 'openai/gpt-oss-20b'),
        ('groq', 'llama-3.1-8b-instant'),
        ('mistral', 'open-mistral-nemo'),
    ],
    'medium': [
        ('gemini', 'gemini-3.6-flash'),
        ('gemini', 'gemini-3.5-flash'),
        ('gemini', 'gemini-flash-latest'),
        ('groq', 'openai/gpt-oss-120b'),
        ('mistral', 'mistral-small-latest'),
        ('groq', 'llama-3.3-70b-versatile'),
    ],
    'complex': [
        ('gemini', 'gemini-3.1-pro-preview'),
        ('gemini', 'gemini-3.6-flash'),
        ('groq', 'openai/gpt-oss-120b'),
        ('mistral', 'mistral-large-latest'),
        ('groq', 'llama-3.3-70b-versatile'),
    ],
}


# Thread-local pooled HTTP sessions (requests.Session is not thread-safe
# to share, so each worker thread keeps its own per-provider pool).
_thread_sessions = threading.local()


class MultiProviderBalancer:
    def __init__(self):
        self.rate_limiter = RateLimiter()
        self.provider_keys = load_all_keys()
        self.model_cache = {}
        self.cache_time = 0
        self.CACHE_TTL = 300

    def _session_for(self, provider_name):
        """Thread-local pooled session per provider: reuses TCP+TLS
        connections instead of a fresh handshake on every chat message."""
        stash = getattr(_thread_sessions, "sessions", None)
        if stash is None:
            stash = {}
            _thread_sessions.sessions = stash
        if provider_name not in stash:
            sess = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
            stash[provider_name] = sess
        return stash[provider_name]

    def _get_models(self, provider_name, api_key):
        cfg = PROVIDERS.get(provider_name)
        if not cfg:
            return []
        return cfg.get("models", [])

    def _try_provider(self, provider_name, provider_cfg, messages, model, temperature, max_tokens, stream, user_keys=None, opts=None):
        keys = []
        if user_keys is not None:
            # User-scoped keys: ONLY this user's keys, never the server's keys
            if provider_name in user_keys:
                keys = [{"env_key": f"USER_{provider_name.upper()}", "key": user_keys[provider_name]}]
            else:
                keys = []
        else:
            keys = self.provider_keys.get(provider_name, [])
        if not keys:
            return None

        scope = (opts or {}).get("health_scope") or "server"
        for key_info in keys:
            api_key = key_info["key"]
            if not self.rate_limiter.can_request(model):
                continue

            try:
                headers = provider_cfg["headers"](api_key)
                body = provider_cfg["build_body"](messages, model, api_key)
                body, opts_applied = apply_request_opts(provider_name, body, opts)
                if (opts or {}).get("tools"):
                    _add_tools_to_body(provider_name, body, opts["tools"])
                if provider_name != "gemini":
                    body["stream"] = stream

                url = provider_cfg["stream_url"](model, api_key)
                # Function-calling providers answer tool calls in a plain JSON
                # body; gemini only does that on its non-SSE endpoint, so switch
                # to it when tools are on (its stream_url is SSE-only).
                if (opts or {}).get("tools"):
                    plain_url = provider_cfg.get("url", provider_cfg["stream_url"])(model, api_key)
                    if plain_url != url:
                        url = plain_url
                # Pooled session (no fresh TLS handshake); 10s connect cap
                # so dead endpoints fail fast, 60s read budget for streams.
                sess = self._session_for(provider_name)
                resp = sess.post(url, json=body, headers=headers, stream=True, timeout=(10, 60))

                if resp.status_code in (400, 422) and opts_applied:
                    # The provider rejected an optional generation knob
                    # (unsupported reasoning_effort, budget, etc). Retry the
                    # SAME request once with stock parameters rather than
                    # failing the turn.
                    logger.warning(
                        f"[PARAMS] {provider_name}/{model} rejected generation options "
                        f"({resp.status_code}), retrying with defaults: {resp.text[:160]}"
                    )
                    body = provider_cfg["build_body"](messages, model, api_key)
                    if provider_name != "gemini":
                        body["stream"] = stream
                    resp = sess.post(url, json=body, headers=headers, stream=True, timeout=(10, 60))

                if resp.status_code == 200:
                    self.rate_limiter.record_request(model)
                    record_model_outcome(scope, provider_name, model, "ok")
                    return resp
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    self.rate_limiter.set_cooldown(model, retry_after)
                    record_model_outcome(scope, provider_name, model, "rate", "rate limited")
                    logger.warning(f"[RATE] {provider_name}/{model} rate limited, cooldown {retry_after}s")
                elif resp.status_code in (401, 403):
                    # Dead/revoked key: cool down so every later message
                    # doesn't waste a full round trip re-proving it.
                    self.rate_limiter.set_cooldown(model, 300)
                    record_model_outcome(scope, provider_name, model, "fail", f"auth rejected ({resp.status_code})")
                    logger.warning(f"[ERROR] {provider_name}/{model} auth rejected ({resp.status_code}), cooldown 300s")
                elif resp.status_code >= 500:
                    self.rate_limiter.set_cooldown(model, 30)
                    record_model_outcome(scope, provider_name, model, "fail", f"server error {resp.status_code}")
                    logger.warning(f"[ERROR] {provider_name}/{model} server error {resp.status_code}")
                else:
                    record_model_outcome(scope, provider_name, model, "fail", f"http {resp.status_code}")
                    logger.warning(f"[ERROR] {provider_name}/{model} error {resp.status_code}: {resp.text[:200]}")
            except requests.exceptions.ConnectionError:
                self.rate_limiter.set_cooldown(model, 60)
                record_model_outcome(scope, provider_name, model, "fail", "connection failed")
                logger.warning(f"[ERROR] {provider_name}/{model} connection failed, cooldown 60s")
            except requests.exceptions.Timeout:
                self.rate_limiter.set_cooldown(model, 30)
                record_model_outcome(scope, provider_name, model, "fail", "timeout")
                logger.warning(f"[ERROR] {provider_name}/{model} timeout, cooldown 30s")
            except Exception as e:
                record_model_outcome(scope, provider_name, model, "fail", str(e))
                logger.warning(f"[ERROR] {provider_name}/{model} exception: {e}")

        return None

    def _raw_tool_calls(self, provider_name, raw):
        """Extract tool calls from a raw provider JSON payload (OpenAI shape)."""
        calls = []
        if not isinstance(raw, dict):
            return calls
        if provider_name == "anthropic":
            for block in raw.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append({"id": block.get("id", ""), "type": "function",
                                  "function": {"name": block.get("name", ""),
                                               "arguments": json.dumps(block.get("input", {}) or {})}})
            return calls
        if provider_name == "gemini":
            for cand in raw.get("candidates", []) or []:
                for part in (cand.get("content", {}) or {}).get("parts", []) or []:
                    fn = part.get("functionCall") or part.get("function_call")
                    if fn:
                        calls.append({"id": "", "type": "function",
                                      "function": {"name": fn.get("name", ""),
                                                   "arguments": json.dumps(fn.get("args", {}) or {})}})
            return calls
        choices = raw.get("choices", []) or []
        if choices:
            msg = choices[0].get("message", {}) or {}
            for tc in msg.get("tool_calls", []) or []:
                calls.append({"id": tc.get("id", ""), "type": "function",
                              "function": {"name": tc.get("function", {}).get("name", ""),
                                           "arguments": tc.get("function", {}).get("arguments", "{}")}})
        return calls

    def _raw_text(self, provider_name, raw):
        """Extract plain text from a raw provider JSON payload."""
        if not isinstance(raw, dict):
            return ""
        if provider_name == "anthropic":
            return "".join(b.get("text", "") for b in raw.get("content", []) or []
                           if isinstance(b, dict) and b.get("type") == "text")
        if provider_name == "gemini":
            out = []
            for cand in raw.get("candidates", []) or []:
                for part in (cand.get("content", {}) or {}).get("parts", []) or []:
                    t = part.get("text", "")
                    if t:
                        out.append(t)
            return "".join(out)
        choices = raw.get("choices", []) or []
        if choices:
            return (choices[0].get("message", {}) or {}).get("content") or ""
        return ""

    def _collect_response(self, resp, provider_name, model):
        """Read a provider response once and return an OpenAI-shaped completion.

        Preserves tool_calls so function-calling providers can drive a tool
        loop. JSON payloads are parsed once for text + tool calls; SSE (gemini)
        falls back to the streaming normaliser for text.
        """
        parts = []
        tool_calls = []
        ctype = (resp.headers.get("Content-Type") or "").lower()
        is_sse = "event-stream" in ctype
        if not is_sse:
            try:
                raw_bytes = resp.content
            except Exception:
                raw_bytes = b""
            if raw_bytes:
                try:
                    raw = json.loads(raw_bytes)
                    tool_calls = self._raw_tool_calls(provider_name, raw)
                    txt = self._raw_text(provider_name, raw)
                    if txt:
                        parts.append(txt)
                except Exception:
                    pass
        if not parts:
            for chunk in self._stream_response(resp, provider_name, model):
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif hasattr(chunk, "text"):
                    parts.append(chunk.text)
        text = "".join(parts)
        if (not text.strip() or "[Stream error from" in text) and not tool_calls:
            raise RuntimeError(f"{provider_name}/{model} returned no usable content")
        result = self._make_response(text, f"{provider_name}/{model}")
        if tool_calls:
            result["choices"][0]["message"]["tool_calls"] = tool_calls
        return result

    def _make_response(self, content, model=MODEL_NAME):
        return {
            "id": f"chatcmpl-{MODEL_NAME}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    def generate(self, messages, model=MODEL_NAME, temperature=0.7, max_tokens=4000, stream=True, user_keys=None, opts=None):
        messages = self._inject_system_prompt(messages, opts)
        if model == MODEL_NAME or model == "omnilink":
            task = classify_task(messages)
            logger.info(f"[ROUTING] Task classified as '{task}'")
            return self._smart_generate(messages, task, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)
        else:
            return self._direct_generate(messages, model, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)

    def _inject_system_prompt(self, messages, opts=None):
        # Per-request language wins over the process-wide default so one
        # account's language setting never leaks into another account's replies.
        lang = ((opts or {}).get("language") or LANGUAGE)
        if not isinstance(lang, str) or not lang.strip():
            lang = LANGUAGE
        system_msg = (
            f"You are a helpful AI assistant. "
            f"Always respond in {lang} regardless of the language the user writes in. "
            f"Do not use any other language."
        )
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_msg + "\n\n" + _content_text(messages[0].get("content", ""))
        else:
            messages = [{"role": "system", "content": system_msg}] + list(messages)
        return messages

    def _smart_generate(self, messages, task, temperature, max_tokens, stream, user_keys=None, opts=None):
        tier = MODEL_TIERS.get(task, MODEL_TIERS['medium'])
        for provider_name, model_name in tier:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                continue
            resp = self._try_provider(provider_name, provider_cfg, messages, model_name, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)
            if resp is not None:
                logger.info(f"[ROUTING] Using {provider_name}/{model_name}")
                used_model = f"{provider_name}/{model_name}"
                if stream:
                    return (self._stream_response(resp, provider_name, model_name), used_model)
                else:
                    return (self._collect_response(resp, provider_name, model_name), used_model)

        fallback_order = ["groq", "gemini", "openai", "anthropic", "openrouter", "mistral", "together", "deepseek", "cohere"]
        return self._fallback_generate(messages, fallback_order, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)

    def _fallback_generate(self, messages, fallback_order, temperature, max_tokens, stream, user_keys=None, opts=None):
        """Try every model of every listed provider in order. Shared by the
        smart router and direct-model requests, so picking one model that
        happens to be down no longer reports every provider unavailable
        while other providers hold working keys."""
        for provider_name in fallback_order:
            provider_cfg = PROVIDERS.get(provider_name)
            if not provider_cfg:
                continue
            for pm in provider_cfg["models"]:
                resp = self._try_provider(provider_name, provider_cfg, messages, pm, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)
                if resp is not None:
                    logger.info(f"[ROUTING] Fallback to {provider_name}/{pm}")
                    used_model = f"{provider_name}/{pm}"
                    if stream:
                        return (self._stream_response(resp, provider_name, pm), used_model)
                    else:
                        return (self._collect_response(resp, provider_name, pm), used_model)

        return self._ollama_fallback(messages, temperature, max_tokens, stream, opts=opts)

    def _direct_generate(self, messages, model, temperature, max_tokens, stream, user_keys=None, opts=None):
        # First-class Ollama selection: "ollama/<tag>" always goes to the
        # server's Ollama with exactly that model — never the cloud fallback.
        if isinstance(model, str) and model.startswith("ollama/"):
            return self._ollama_chat(model.split("/", 1)[1], messages, temperature,
                                     max_tokens, stream, opts=opts)
        # Paired-device Ollama: "device/<agent_id>/<tag>" relays through the
        # companion app on that computer — never the cloud fallback.
        if isinstance(model, str) and model.startswith("device/"):
            parts = model.split("/", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return self._ollama_chat(parts[2], messages, temperature,
                                         max_tokens, stream, opts=opts,
                                         agent_id=parts[1])
        for provider_name, provider_cfg in PROVIDERS.items():
            models = provider_cfg.get("models", [])
            if model in models:
                resp = self._try_provider(provider_name, provider_cfg, messages, model, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)
                if resp is not None:
                    used_model = f"{provider_name}/{model}"
                    if stream:
                        return (self._stream_response(resp, provider_name, model), used_model)
                    else:
                        return (self._collect_response(resp, provider_name, model), used_model)
                # The picked model failed: fall through to the other
                # providers instead of giving up while working keys exist.
                break

        # Legacy: a bare installed tag from an older menu ("llama3.1:8b").
        if isinstance(model, str) and "/" not in model and model:
            try:
                if model in scan_ollama_models():
                    return self._ollama_chat(model, messages, temperature,
                                             max_tokens, stream, opts=opts)
            except Exception:
                pass

        fallback_order = ["groq", "gemini", "openai", "anthropic", "openrouter", "mistral", "together", "deepseek", "cohere"]
        return self._fallback_generate(messages, fallback_order, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)

    def generate_custom(self, messages, name, base_url, api_key, endpoint_model, temperature=0.7, max_tokens=4000, stream=True, opts=None):
        """One-shot request against a caller-supplied OpenAI-compatible endpoint.

        Returns (result, used_model) with the same contract as generate(): a
        generator of text chunks when stream=True, a response dict otherwise.
        Raises RuntimeError when the endpoint yields nothing usable.
        """
        base = (base_url or "").strip().rstrip("/")
        url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        label = f"custom/{name}"
        provider_cfg = {
            "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            "build_body": lambda msgs, model, key: {"model": endpoint_model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens},
            "stream_url": lambda model, key: url,
            "url": lambda model, key: url,
        }
        resp = self._try_provider("custom", provider_cfg, messages, label, temperature, max_tokens, stream, user_keys={"custom": api_key}, opts=opts)
        if resp is None:
            raise RuntimeError(f"custom provider '{name}' did not return a usable response")
        if stream:
            return self._stream_response(resp, "custom", endpoint_model), label
        try:
            result = self._collect_response(resp, "custom", endpoint_model)
        except Exception as e:
            raise RuntimeError(f"custom provider '{name}' returned a non-JSON response: {e}")
        result["model"] = label
        return result, label

    def _ollama_chat(self, tag, messages, temperature, max_tokens, stream, opts=None, agent_id=None):
        """First-class Ollama call for an explicitly chosen installed model.

        Plain requests use the native /api/chat endpoint (text + images[],
        options.num_predict, boolean think). Requests carrying OpenAI tools go
        through Ollama's OpenAI-compatible /v1/chat/completions so function
        calling, vision image_urls and SSE reuse the shared normalisers.

        With agent_id the HTTP hop is relayed through the companion app on a
        paired device (its Ollama, its localhost) instead of the server's own
        Ollama — everything else, including streaming and error handling, is
        identical. Raises RuntimeError with a clean, actionable message on any
        failure. Returns (result, "ollama/<tag>") or (result,
        "device/<agent_id>/<tag>") with the generate() contract.
        """
        scope = (opts or {}).get("health_scope") or "server"
        device = str(agent_id) if agent_id is not None else None
        label = f"device/{device}/{tag}" if device else f"ollama/{tag}"
        if opts:
            o_temp = opts.get("temperature", temperature)
            o_mt = opts.get("max_tokens", max_tokens)
            if isinstance(o_temp, (int, float)) and not isinstance(o_temp, bool):
                temperature = max(0.0, min(2.0, float(o_temp)))
            if isinstance(o_mt, int) and not isinstance(o_mt, bool) and o_mt > 0:
                max_tokens = min(int(o_mt), 32000)
        if device:
            dev_name = relay.device_name(device)
            installed = scan_ollama_models(agent_id=device)
        else:
            dev_name = None
            installed = scan_ollama_models()
        if tag not in installed:
            record_model_outcome(scope, "ollama", label, "fail", "not installed")
            avail = ", ".join(installed[:6]) + ("..." if len(installed) > 6 else "")
            if device:
                hint = f" Installed there: {avail}." if avail else " Nothing is installed on it yet."
                raise RuntimeError(
                    f"Ollama model '{tag}' is not installed on {dev_name}.{hint}"
                    f" Run `ollama pull {tag}` on that device, then try again.")
            hint = f" Installed here: {avail}." if avail else " Nothing is installed here yet."
            raise RuntimeError(
                f"Ollama model '{tag}' is not installed on the server.{hint}"
                f" Run `ollama pull {tag}` on the server, then try again.")
        if not self.rate_limiter.can_request(label):
            raise RuntimeError(f"Ollama model '{tag}' is cooling down after a recent failure — try again shortly.")
        base = OLLAMA_BASE_URL
        sess = self._session_for("ollama")
        use_tools = bool((opts or {}).get("tools"))
        resp = None
        try:
            if use_tools:
                # OpenAI-compatible path: tools + vision + SSE.
                body = {"model": tag, "messages": messages, "stream": stream,
                        "temperature": temperature, "max_tokens": max_tokens}
                _add_tools_to_body("ollama", body, (opts or {})["tools"])
                if device:
                    resp = relay.open_response(device, "/v1/chat/completions", body,
                                               read_timeout=180.0)
                else:
                    resp = sess.post(base + "/v1/chat/completions", json=body,
                                     stream=True, timeout=(10, 180))
            else:
                body = {"model": tag, "messages": _ollama_native_messages(messages),
                        "stream": stream,
                        "options": {"temperature": temperature, "num_predict": max_tokens}}
                # Ollama's reasoning knob is a boolean `think` flag.
                level = resolve_level((opts or {}).get("effort"), (opts or {}).get("thinking"))
                if level != "auto":
                    body["think"] = level != "minimum"
                if device:
                    resp = relay.open_response(device, "/api/chat", body,
                                               read_timeout=300.0)
                else:
                    resp = sess.post(base + "/api/chat", json=body,
                                     stream=stream, timeout=(10, 300))
            if resp.status_code == 200:
                self.rate_limiter.record_request(label)
                record_model_outcome(scope, "ollama", label, "ok")
                if stream:
                    if use_tools:
                        gen = self._stream_response(resp, "ollama", label)
                    else:
                        gen = self._stream_ollama(resp, label)
                    if device:
                        # The guard closes the relay when the consumer
                        # finishes or abandons the stream (-> 410 to the
                        # companion, which stops the local Ollama read).
                        gen = _relay_guard(gen, resp)
                    return (gen, label)
                if use_tools:
                    result = self._collect_response(resp, "ollama", label)
                else:
                    data = resp.json()
                    content = data.get("message", {}).get("content", "")
                    result = self._make_response(content, label)
                if device:
                    resp.close()
                return (result, label)
            if resp.status_code == 400 and use_tools:
                detail = _ollama_error_text(resp)
                if "does not support tools" in detail.lower():
                    # Small models (e.g. smollm2) reject tool definitions
                    # outright: answer directly instead of failing the turn.
                    # Recursion is safe: tools are stripped, so use_tools is
                    # False on the retry and this branch cannot re-trigger.
                    try:
                        resp.close()
                    except Exception:
                        pass
                    logger.info(f"[OLLAMA] {label} does not support tools; retrying plain")
                    no_tools = dict(opts or {})
                    no_tools.pop("tools", None)
                    return self._ollama_chat(tag, messages, temperature, max_tokens,
                                             stream, opts=no_tools, agent_id=agent_id)
            if resp.status_code == 429:
                self.rate_limiter.set_cooldown(label, 60)
            elif resp.status_code >= 500:
                self.rate_limiter.set_cooldown(label, 30)
            detail = _ollama_error_text(resp)
            status = resp.status_code
            if device:
                resp.close()
                resp = None
            record_model_outcome(scope, "ollama", label, "fail", f"http {status}")
            raise RuntimeError(f"Ollama model '{tag}' failed (HTTP {status}): {detail}")
        except RuntimeError:
            if device and resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            raise
        except (relay.RelayError, relay.RelayTimeout) as e:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            self.rate_limiter.set_cooldown(label, 30)
            record_model_outcome(scope, "ollama", label, "fail", str(e)[:80])
            if getattr(e, "ready", False):
                raise RuntimeError(str(e))
            raise RuntimeError(f"Ollama on {dev_name} failed: {e}")
        except requests.exceptions.ConnectionError:
            self.rate_limiter.set_cooldown(label, 60)
            record_model_outcome(scope, "ollama", label, "fail", "connection failed")
            raise RuntimeError(
                f"Could not reach Ollama at {base} — is `ollama serve` running on the server?")
        except requests.exceptions.Timeout:
            self.rate_limiter.set_cooldown(label, 30)
            record_model_outcome(scope, "ollama", label, "fail", "timeout")
            raise RuntimeError(
                f"Ollama model '{tag}' timed out — the model may still be loading (first run pulls it into memory). Try again in a minute.")
        except Exception as e:
            record_model_outcome(scope, "ollama", label, "fail", str(e))
            raise RuntimeError(f"Ollama model '{tag}' failed: {e}")

    def _ollama_fallback(self, messages, temperature, max_tokens, stream, opts=None):
        try:
            ollama_models = scan_ollama_models()
            if ollama_models:
                target = OLLAMA_MODEL if OLLAMA_MODEL in ollama_models else ollama_models[0]
                return self._ollama_chat(target, messages, temperature, max_tokens, stream, opts=opts)
        except Exception as e:
            logger.warning(f"[OLLAMA] Fallback failed: {e}")

        if stream:
            def error_gen():
                yield "Sorry, all providers are currently unavailable. Configure your API keys in the Omniilink Settings page or check your server connection."
            return (error_gen(), "none")
        else:
            return (self._make_response("Sorry, all providers are currently unavailable. Configure your API keys in the Omniilink Settings page or check your server connection."), "none")

    def _stream_response(self, resp, provider_name, model):
        def generate_stream():
            try:
                for line in resp.iter_lines():
                    if line:
                        line = line.decode("utf-8")
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                # OpenAI-style: choices[].delta.content
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content")
                                    if content:
                                        yield content
                                        continue
                                    # Anthropic-style text deltas
                                    if delta.get("type") == "text_delta" and delta.get("text"):
                                        yield delta["text"]
                                        continue
                                    if delta.get("text") and not delta.get("thinking"):
                                        yield delta["text"]
                                        continue
                                # Gemini-style: candidates[].content.parts[].text
                                candidates = chunk.get("candidates", [])
                                if candidates:
                                    parts = candidates[0].get("content", {}).get("parts", [])
                                    for part in parts:
                                        text = part.get("text", "")
                                        if text:
                                            yield text
                            except json.JSONDecodeError:
                                continue
                        else:
                            try:
                                data = json.loads(line)
                                # OpenAI non-streaming fallback
                                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                                if content:
                                    yield content
                                    return
                                # Gemini non-streaming fallback
                                candidates = data.get("candidates", [])
                                if candidates:
                                    parts = candidates[0].get("content", {}).get("parts", [])
                                    for part in parts:
                                        text = part.get("text", "")
                                        if text:
                                            yield text
                                    return
                            except json.JSONDecodeError:
                                continue
            except (relay.RelayError, relay.RelayTimeout):
                # Surface the relay's clean message (device offline,
                # stalled) as a real stream error, not placeholder text.
                raise
            except Exception as e:
                logger.warning(f"[STREAM] {provider_name}/{model} error: {e}")
                yield f"\n[Stream error from {provider_name}/{model}]"
        return generate_stream()

    def _stream_ollama(self, resp, model):
        def generate_stream():
            try:
                for line in resp.iter_lines():
                    if line:
                        try:
                            chunk = json.loads(line)
                            content = chunk.get("message", {}).get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
            except (relay.RelayError, relay.RelayTimeout):
                raise
            except Exception as e:
                logger.warning(f"[OLLAMA STREAM] error: {e}")
                yield f"\n[Ollama stream error]"
        return generate_stream()


lb = MultiProviderBalancer()
