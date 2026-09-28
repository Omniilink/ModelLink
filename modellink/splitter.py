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

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:3b")

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
                "contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in msgs if m["role"] != "system"],
                **({"systemInstruction": {"parts": [{"text": _sys}]} } if _sys else {}),
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4000}
            }
        )(next((m["content"] for m in msgs if m.get("role") == "system"), None)),
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
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs if m["role"] != "system"],
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
        "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "allam-2-7b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
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
        "api_base": "https://opencode.ai/zen/go/v1",
        "models": ["opencode-default"],
        "headers": lambda key: {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        "build_body": lambda msgs, model, key: {
            "model": "opencode-default",
            "messages": msgs,
            "temperature": 0.7,
            "max_tokens": 4000
        },
        "stream_url": lambda model, key: os.environ.get("OPENCODE_API_URL", "https://opencode.ai/zen/go/v1/chat/completions"),
        "url": lambda model, key: os.environ.get("OPENCODE_API_URL", "https://opencode.ai/zen/go/v1/chat/completions"),
    },
}


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
    return body, json.dumps(body, sort_keys=True, default=str) != before



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


def scan_ollama_models():
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        pass
    return []


def get_backup_model():
    return OLLAMA_MODEL


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
        ('groq', 'allam-2-7b'),
        ('groq', 'qwen/qwen3.6-27b'),
        ('mistral', 'open-mistral-nemo'),
    ],
    'medium': [
        ('gemini', 'gemini-3.6-flash'),
        ('gemini', 'gemini-3.5-flash'),
        ('gemini', 'gemini-flash-latest'),
        ('groq', 'openai/gpt-oss-120b'),
        ('groq', 'qwen/qwen3.8-27b'),
        ('mistral', 'mistral-small-latest'),
        ('groq', 'llama-3.3-70b-versatile'),
    ],
    'complex': [
        ('gemini', 'gemini-3.1-pro-preview'),
        ('gemini', 'gemini-3.6-flash'),
        ('groq', 'openai/gpt-oss-120b'),
        ('groq', 'qwen/qwen3.8-27b'),
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

        for key_info in keys:
            api_key = key_info["key"]
            if not self.rate_limiter.can_request(model):
                continue

            try:
                headers = provider_cfg["headers"](api_key)
                body = provider_cfg["build_body"](messages, model, api_key)
                body, opts_applied = apply_request_opts(provider_name, body, opts)
                if provider_name != "gemini":
                    body["stream"] = stream

                url = provider_cfg["stream_url"](model, api_key)
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
                    return resp
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    self.rate_limiter.set_cooldown(model, retry_after)
                    logger.warning(f"[RATE] {provider_name}/{model} rate limited, cooldown {retry_after}s")
                elif resp.status_code in (401, 403):
                    # Dead/revoked key: cool down so every later message
                    # doesn't waste a full round trip re-proving it.
                    self.rate_limiter.set_cooldown(model, 300)
                    logger.warning(f"[ERROR] {provider_name}/{model} auth rejected ({resp.status_code}), cooldown 300s")
                elif resp.status_code >= 500:
                    self.rate_limiter.set_cooldown(model, 30)
                    logger.warning(f"[ERROR] {provider_name}/{model} server error {resp.status_code}")
                else:
                    logger.warning(f"[ERROR] {provider_name}/{model} error {resp.status_code}: {resp.text[:200]}")
            except requests.exceptions.ConnectionError:
                self.rate_limiter.set_cooldown(model, 60)
                logger.warning(f"[ERROR] {provider_name}/{model} connection failed, cooldown 60s")
            except requests.exceptions.Timeout:
                self.rate_limiter.set_cooldown(model, 30)
                logger.warning(f"[ERROR] {provider_name}/{model} timeout, cooldown 30s")
            except Exception as e:
                logger.warning(f"[ERROR] {provider_name}/{model} exception: {e}")

        return None

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
        if model == MODEL_NAME:
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
            messages[0]["content"] = system_msg + "\n\n" + messages[0].get("content", "")
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
                    result = resp.json()
                    result["model"] = used_model
                    return (result, used_model)

        fallback_order = ["groq", "gemini", "openai", "anthropic", "openrouter", "mistral", "together", "deepseek", "cohere"]
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
                        result = resp.json()
                        result["model"] = used_model
                        return (result, used_model)

        return self._ollama_fallback(messages, temperature, max_tokens, stream, opts=opts)

    def _direct_generate(self, messages, model, temperature, max_tokens, stream, user_keys=None, opts=None):
        for provider_name, provider_cfg in PROVIDERS.items():
            models = provider_cfg.get("models", [])
            if model in models:
                resp = self._try_provider(provider_name, provider_cfg, messages, model, temperature, max_tokens, stream, user_keys=user_keys, opts=opts)
                if resp is not None:
                    used_model = f"{provider_name}/{model}"
                    if stream:
                        return (self._stream_response(resp, provider_name, model), used_model)
                    else:
                        result = resp.json()
                        result["model"] = used_model
                        return (result, used_model)

        return self._ollama_fallback(messages, temperature, max_tokens, stream, opts=opts)

    def _ollama_fallback(self, messages, temperature, max_tokens, stream, opts=None):
        ollama_models = scan_ollama_models()
        if ollama_models:
            target_model = OLLAMA_MODEL if OLLAMA_MODEL in ollama_models else ollama_models[0]
            try:
                if opts:
                    o_temp = opts.get("temperature", temperature)
                    o_mt = opts.get("max_tokens", max_tokens)
                    if isinstance(o_temp, (int, float)) and not isinstance(o_temp, bool):
                        temperature = max(0.0, min(2.0, float(o_temp)))
                    if isinstance(o_mt, int) and not isinstance(o_mt, bool) and o_mt > 0:
                        max_tokens = min(int(o_mt), 32000)
                body = {
                    "model": target_model,
                    "messages": messages,
                    "stream": stream,
                    "options": {"temperature": temperature, "num_predict": max_tokens}
                }
                # Ollama's reasoning knob is a boolean `think` flag.
                level = resolve_level((opts or {}).get("effort"), (opts or {}).get("thinking"))
                if level != "auto":
                    body["think"] = level != "minimum"
                resp = requests.post(f"{OLLAMA_BASE_URL}/api/chat", json=body, stream=stream, timeout=120)
                if resp.status_code == 200:
                    used_model = f"ollama/{target_model}"
                    if stream:
                        return (self._stream_ollama(resp, target_model), used_model)
                    else:
                        data = resp.json()
                        content = data.get("message", {}).get("content", "")
                        return (self._make_response(content, target_model), used_model)
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
            except Exception as e:
                logger.warning(f"[OLLAMA STREAM] error: {e}")
                yield f"\n[Ollama stream error]"
        return generate_stream()


lb = MultiProviderBalancer()
