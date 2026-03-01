# -*- coding: utf-8 -*-
"""LLM message logger — records all requests sent to and responses received
from the language model for debugging and auditing purposes.

Log directory: ~/.copaw/llm_sessions/
Each conversation session gets its own file: <session_id>.log

Usage:
    # At the start of each session (e.g. in query_handler):
    from copaw.utils.llm_logger import set_llm_log_session
    set_llm_log_session(session_id)

Each interaction is separated by a visual divider and includes:
  - Timestamp and direction (REQUEST / RESPONSE)
  - Model name
  - Full message list (for requests) or content blocks (for responses)
  - Token usage (for responses, when available)
"""
import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Directory & context variable
# ---------------------------------------------------------------------------
_LLM_SESSIONS_DIR = Path.home() / ".copaw" / "llm_sessions"
_LLM_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Holds the current session ID for the running async task.
# Falls back to "default" when set_llm_log_session() has not been called.
_current_session_id: ContextVar[str] = ContextVar(
    "llm_log_session_id", default="default"
)

# Cache of per-session loggers so we don't create a new FileHandler every call
_session_loggers: dict[str, logging.Logger] = {}

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def set_llm_log_session(session_id: str) -> None:
    """Bind *session_id* to the current async context.

    Call this once at the beginning of each ``query_handler`` invocation so
    that all subsequent LLM log writes for this request land in the correct
    per-session file.

    Args:
        session_id: Unique identifier for the conversation session.
    """
    _current_session_id.set(session_id)


def _get_session_logger() -> logging.Logger:
    """Return (and lazily create) the logger for the current session."""
    session_id = _current_session_id.get()
    if session_id not in _session_loggers:
        log_path = _LLM_SESSIONS_DIR / f"{session_id}.log"
        logger = logging.getLogger(f"copaw.llm_session.{session_id}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.FileHandler(
                str(log_path), mode="a", encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        _session_loggers[session_id] = logger
    return _session_loggers[session_id]


_DIVIDER = "=" * 80


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_json(obj: Any, max_len: int = 2000) -> str:
    """Serialize *obj* to a JSON string, truncating if necessary."""
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = repr(obj)
    if len(text) > max_len:
        text = text[:max_len] + f"\n... (truncated, total {len(text)} chars)"
    return text


def _format_content_block(block: Any) -> str:
    """Return a human-readable string for a single content block."""
    if isinstance(block, str):
        return block

    if not isinstance(block, dict):
        # Could be a dataclass / Pydantic model — try to convert
        try:
            block = block.__dict__ if hasattr(block, "__dict__") else dict(block)
        except Exception:
            return repr(block)

    btype = block.get("type", "unknown")

    if btype == "text":
        return block.get("text", "")
    if btype == "thinking":
        content = block.get("thinking", "")
        return f"[thinking]\n{content}"
    if btype == "tool_use":
        name = block.get("name", "?")
        call_id = block.get("id", "?")
        raw_input = block.get("input", {})
        input_str = _safe_json(raw_input, max_len=500)
        return f"[tool_use  name={name}  id={call_id}]\n{input_str}"
    if btype == "tool_result":
        call_id = block.get("tool_use_id", block.get("id", "?"))
        content = block.get("content", "")
        if isinstance(content, list):
            content = "\n".join(_format_content_block(b) for b in content)
        return f"[tool_result  id={call_id}]\n{content}"
    if btype == "image":
        src = block.get("source", {})
        return f"[image  media_type={src.get('media_type', '?')}]"

    # Fallback
    return _safe_json(block, max_len=500)


def _format_message(msg: dict) -> str:
    """Return a human-readable representation of a single OpenAI-style message."""
    role = msg.get("role", "unknown")
    content = msg.get("content", "")
    tool_call_id = msg.get("tool_call_id")
    tool_calls = msg.get("tool_calls")
    name = msg.get("name")

    header_parts = [f"[{role}]"]
    if name:
        header_parts.append(f"  name={name}")
    if tool_call_id:
        header_parts.append(f"  id={tool_call_id}")
    header = "".join(header_parts)

    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        body = "\n".join(_format_content_block(b) for b in content)
    else:
        body = repr(content)

    lines = [header]
    if body:
        lines.append(body)

    # Append any outgoing tool calls embedded in an assistant message
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            lines.append(
                f"[tool_call  name={fn.get('name', '?')}  "
                f"id={tc.get('id', '?')}]\n"
                f"{fn.get('arguments', '')}"
            )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_llm_request(
    messages: list[dict],
    model_name: str = "",
    tools: list | None = None,
    **kwargs: Any,
) -> None:
    """Log the messages being sent to the LLM.

    Args:
        messages: OpenAI-style message list.
        model_name: Name of the model being called.
        tools: Tool schemas passed to the model (only count is logged).
        **kwargs: Additional call kwargs (ignored, for forward-compat).
    """
    tools_info = f"  tools={len(tools)}" if tools else ""
    header = (
        f"\n{_DIVIDER}\n"
        f"▶ REQUEST  model={model_name}{tools_info}\n"
        f"{_DIVIDER}"
    )
    body_parts = []
    for msg in messages:
        body_parts.append(_format_message(msg))
    body = "\n\n".join(body_parts)
    _get_session_logger().debug("%s\n%s", header, body)


def log_llm_response(
    response: Any,
    model_name: str = "",
) -> None:
    """Log the response received from the LLM.

    *response* may be a ``ChatResponse`` dataclass, a dict, or ``None``.
    When ``None`` is passed (e.g. streaming was cancelled), nothing is written.

    Args:
        response: The final ChatResponse (or last accumulated chunk).
        model_name: Name of the model that produced the response.
    """
    if response is None:
        return

    # Extract content and usage from various response shapes
    content = getattr(response, "content", None)
    usage = getattr(response, "usage", None)

    usage_str = ""
    if usage is not None:
        in_tok = getattr(usage, "input_tokens", "?")
        out_tok = getattr(usage, "output_tokens", "?")
        elapsed = getattr(usage, "time", None)
        elapsed_str = f"  t={elapsed:.1f}s" if elapsed is not None else ""
        usage_str = f"  in={in_tok} out={out_tok}{elapsed_str}"

    header = (
        f"\n{_DIVIDER}\n"
        f"◀ RESPONSE  model={model_name}{usage_str}\n"
        f"{_DIVIDER}"
    )

    if content is None:
        body = repr(response)
    elif isinstance(content, list):
        body = "\n\n".join(_format_content_block(b) for b in content)
    else:
        body = str(content)

    _get_session_logger().debug("%s\n%s", header, body)
