"""
llm.py — the one AI client every stage uses.

Talks to any OpenAI-compatible chat API, chosen in .env:
  Groq (default)  LLM_BASE_URL=https://api.groq.com/openai/v1
  xAI Grok        LLM_BASE_URL=https://api.x.ai/v1
  OpenAI          LLM_BASE_URL=https://api.openai.com/v1
  local Ollama    LLM_BASE_URL=http://localhost:11434/v1
with LLM_API_KEY / LLM_MODEL / LLM_VISION_MODEL to match.

complete(...) never raises: it returns (result, error_code).
Error codes:
  "ai_disabled"         switched off on purpose            (not retryable)
  "ai_not_configured"   no API key in .env                 (not retryable: fix .env)
  "ai_unavailable"      `openai` package missing / client failed to start
  "ai_rate_limited"     still rate-limited after retries   (retry later)
  "ai_call_failed"      network / API error after retries  (retry later)
  "ai_bad_response"     never returned a usable answer     (retry later)
"""
import time

from . import activity, config

RETRYABLE_ERRORS = {"ai_unavailable", "ai_rate_limited", "ai_call_failed", "ai_bad_response"}

_client = None
_client_config = None
_warned = set()


def _warn_once(key, message):
    if key not in _warned:
        _warned.add(key)
        print(message)


def availability():
    """(True, None) if the AI can be called, else (False, error_code)."""
    if config.ai_disabled():
        return False, "ai_disabled"
    if not config.llm_api_key():
        _warn_once("no-key", "AI is OFF: LLM_API_KEY is not set (in .env locally, or as a secret on the "
                             "host) - running rules + OCR only.")
        return False, "ai_not_configured"
    return True, None


def get_client():
    """Cached OpenAI-compatible client; rebuilt if the key/URL changes."""
    global _client, _client_config
    wanted = (config.llm_base_url(), config.llm_api_key())
    if _client is None or _client_config != wanted:
        from openai import OpenAI
        _client = OpenAI(base_url=wanted[0], api_key=wanted[1], max_retries=0, timeout=60)
        _client_config = wanted
    return _client


def reset():
    """Forget the cached client (tests, or after changing .env at runtime)."""
    global _client, _client_config
    _client, _client_config = None, None


def _error_code(exc):
    text = str(exc).lower()
    if "429" in text or "rate_limit" in text or "rate limit" in text:
        return "ai_rate_limited"
    return "ai_call_failed"


def _wait(seconds, why):
    """Sleep in 1-second steps so the UI can show a live countdown."""
    activity.ai_waiting(seconds)
    try:
        for left in range(int(seconds), 0, -1):
            activity.step(f"{why} - retrying in {left}s", mode="ai_wait")
            time.sleep(1)
    finally:
        activity.ai_waiting(0)
    activity.step("Trying the AI again", mode="ai")


def complete(messages, parse, model=None, max_retries=3, pace=0.0, label=""):
    """
    Send `messages`, turn the reply into a result with `parse(text)`.
    `parse` returns the result, or None if the reply is unusable (-> retried).
    Returns (result, None) or (None, error_code). Never raises.
    """
    ok, code = availability()
    if not ok:
        return None, code
    try:
        client = get_client()
    except Exception as e:
        _warn_once("client", f"AI unavailable ({type(e).__name__}: {e}). Is `openai` installed?")
        return None, "ai_unavailable"

    last = "ai_call_failed"
    for attempt in range(max_retries):
        if pace:
            time.sleep(pace)
        activity.ai_begin()
        error = None
        try:
            response = client.chat.completions.create(
                model=model or config.llm_model(), messages=messages, temperature=0)
            text = response.choices[0].message.content if response and response.choices else None
            try:
                result = parse(text)
            except Exception:
                result = None
            if result is not None:
                return result, None
            last = error = "ai_bad_response"
            why, wait = "The AI gave an unusable answer", 2
            print(f"AI {label} gave an unusable answer (attempt {attempt + 1}/{max_retries})")
        except Exception as e:
            last = error = _error_code(e)
            why, wait = (("The AI provider is rate-limiting us", 15) if last == "ai_rate_limited"
                         else ("The AI call failed", 5))
            print(f"AI {label} call failed (attempt {attempt + 1}/{max_retries}): {e}")
        finally:
            activity.ai_end(error)
        if attempt < max_retries - 1:
            _wait(wait, f"{why} (attempt {attempt + 1}/{max_retries})")
    activity.step("AI unavailable - continuing without it", mode="local")
    return None, last
