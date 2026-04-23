"""
Central LLM factory. Every part of the backend that needs an LLM goes through
get_llm() so the provider/model can be swapped from one place.

Default provider: Groq (OpenAI-compatible API, free tier).
  - Sign up: https://console.groq.com/keys
  - Base URL: https://api.groq.com/openai/v1
  - API keys start with "gsk_"
  - Model used: llama-3.3-70b-versatile (fast, good at JSON + summarization)

The same API key field (`gemini_api_key` on the user's settings in MongoDB) is
reused so no database migration is needed — it just holds the Groq key now.
The UI label in Settings has been updated to say "Groq API Key".
"""

from __future__ import annotations

from typing import Optional


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


def get_llm(api_key: str, *, model: Optional[str] = None, temperature: float = 0.0):
    """Return a configured LangChain chat model backed by Groq.

    Raises ValueError if api_key is empty.
    Raises ImportError if langchain-openai isn't installed.
    """
    if not api_key or not api_key.strip():
        raise ValueError("LLM API key is required")

    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:
        raise ImportError(
            "langchain-openai is not installed. Run: pip install langchain-openai"
        ) from e

    return ChatOpenAI(
        model=model or DEFAULT_MODEL,
        api_key=api_key.strip(),
        base_url=GROQ_BASE_URL,
        temperature=temperature,
    )
