# -*- coding: utf-8 -*-
"""Factory for creating chat models and formatters.

This module provides a unified factory for creating chat model instances
and their corresponding formatters based on configuration.

Example:
    >>> from copaw.agents.model_factory import create_model_and_formatter
    >>> model, formatter = create_model_and_formatter()
"""

import logging
import os
import re
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Optional, Sequence, Tuple, Type

from agentscope.formatter import FormatterBase, OpenAIChatFormatter
from agentscope.model import ChatModelBase, OpenAIChatModel

from .utils.tool_message_utils import _sanitize_tool_messages
from ..local_models import create_local_chat_model
from ..providers import (
    get_active_llm_config,
    get_chat_model_class,
    get_provider_chat_model,
    load_providers_json,
)
from ..utils.llm_logger import log_llm_request, log_llm_response

if TYPE_CHECKING:
    from ..providers import ResolvedModelConfig

logger = logging.getLogger(__name__)


# Mapping from chat model class to formatter class
_CHAT_MODEL_FORMATTER_MAP: dict[Type[ChatModelBase], Type[FormatterBase]] = {
    OpenAIChatModel: OpenAIChatFormatter,
}


def _get_formatter_for_chat_model(
    chat_model_class: Type[ChatModelBase],
) -> Type[FormatterBase]:
    """Get the appropriate formatter class for a chat model.

    Args:
        chat_model_class: The chat model class

    Returns:
        Corresponding formatter class, defaults to OpenAIChatFormatter
    """
    return _CHAT_MODEL_FORMATTER_MAP.get(
        chat_model_class,
        OpenAIChatFormatter,
    )


def _create_file_block_support_formatter(
    base_formatter_class: Type[FormatterBase],
) -> Type[FormatterBase]:
    """Create a formatter class with file block support.

    This factory function extends any Formatter class to support file blocks
    in tool results, which are not natively supported by AgentScope.

    Args:
        base_formatter_class: Base formatter class to extend

    Returns:
        Enhanced formatter class with file block support
    """

    class FileBlockSupportFormatter(base_formatter_class):
        """Formatter with file block support for tool results."""

        async def _format(self, msgs):
            """Override to sanitize tool messages before formatting.

            This prevents OpenAI API errors from improperly paired
            tool messages.
            """
            msgs = _sanitize_tool_messages(msgs)
            return await super()._format(msgs)

        @staticmethod
        def convert_tool_result_to_string(
            output: str | list[dict],
        ) -> tuple[str, Sequence[Tuple[str, dict]]]:
            """Extend parent class to support file blocks.

            Uses try-first strategy for compatibility with parent class.

            Args:
                output: Tool result output (string or list of blocks)

            Returns:
                Tuple of (text_representation, multimodal_data)
            """
            if isinstance(output, str):
                return output, []

            # Try parent class method first
            try:
                return base_formatter_class.convert_tool_result_to_string(
                    output,
                )
            except ValueError as e:
                if "Unsupported block type: file" not in str(e):
                    raise

                # Handle output containing file blocks
                textual_output = []
                multimodal_data = []

                for block in output:
                    if not isinstance(block, dict) or "type" not in block:
                        raise ValueError(
                            f"Invalid block: {block}, "
                            "expected a dict with 'type' key",
                        ) from e

                    if block["type"] == "file":
                        file_path = block.get("path", "") or block.get(
                            "url",
                            "",
                        )
                        file_name = block.get("name", file_path)

                        textual_output.append(
                            f"The returned file '{file_name}' "
                            f"can be found at: {file_path}",
                        )
                        multimodal_data.append((file_path, block))
                    else:
                        # Delegate other block types to parent class
                        (
                            text,
                            data,
                        ) = base_formatter_class.convert_tool_result_to_string(
                            [block],
                        )
                        textual_output.append(text)
                        multimodal_data.extend(data)

                if len(textual_output) == 0:
                    return "", multimodal_data
                elif len(textual_output) == 1:
                    return textual_output[0], multimodal_data
                else:
                    return (
                        "\n".join("- " + _ for _ in textual_output),
                        multimodal_data,
                    )

    FileBlockSupportFormatter.__name__ = (
        f"FileBlockSupport{base_formatter_class.__name__}"
    )
    return FileBlockSupportFormatter


def create_model_and_formatter(
    llm_cfg: Optional["ResolvedModelConfig"] = None,
) -> Tuple[ChatModelBase, FormatterBase]:
    """Factory method to create model and formatter instances.

    This method handles both local and remote models, selecting the
    appropriate chat model class and formatter based on configuration.

    Args:
        llm_cfg: Resolved model configuration. If None, will call
            get_active_llm_config() to fetch the active configuration.

    Returns:
        Tuple of (model_instance, formatter_instance)

    Example:
        >>> model, formatter = create_model_and_formatter()
        >>> # Use with custom config
        >>> from copaw.providers import get_active_llm_config
        >>> custom_cfg = get_active_llm_config()
        >>> model, formatter = create_model_and_formatter(custom_cfg)
    """
    # Fetch config if not provided
    if llm_cfg is None:
        llm_cfg = get_active_llm_config()

    # Create the model instance and determine chat model class
    model, chat_model_class = _create_model_instance(llm_cfg)

    # Create the formatter based on chat_model_class
    formatter = _create_formatter_instance(chat_model_class)

    # Wrap the model with logging proxy to record all LLM I/O
    model = LoggingChatModelProxy(model)

    return model, formatter


def _create_model_instance(
    llm_cfg: Optional["ResolvedModelConfig"],
) -> Tuple[ChatModelBase, Type[ChatModelBase]]:
    """Create a chat model instance and determine its class.

    Args:
        llm_cfg: Resolved model configuration

    Returns:
        Tuple of (model_instance, chat_model_class)
    """
    # Handle local models
    if llm_cfg and llm_cfg.is_local:
        model = create_local_chat_model(
            model_id=llm_cfg.model,
            stream=True,
            generate_kwargs={"max_tokens": None},
        )
        # Local models use OpenAIChatModel-compatible formatter
        return model, OpenAIChatModel

    # Handle remote models - determine chat_model_class from provider config
    chat_model_class = _get_chat_model_class_from_provider()

    # Create remote model instance with configuration
    model = _create_remote_model_instance(llm_cfg, chat_model_class)

    return model, chat_model_class


def _get_chat_model_class_from_provider() -> Type[ChatModelBase]:
    """Get the chat model class from provider configuration.

    Returns:
        Chat model class, defaults to OpenAIChatModel if not found
    """
    chat_model_class = OpenAIChatModel  # default
    try:
        providers_data = load_providers_json()
        provider_id = providers_data.active_llm.provider_id
        if provider_id:
            chat_model_name = get_provider_chat_model(
                provider_id,
                providers_data,
            )
            chat_model_class = get_chat_model_class(chat_model_name)
    except Exception as e:
        logger.debug(
            "Failed to determine chat model from provider: %s, "
            "using OpenAIChatModel",
            e,
        )
    return chat_model_class


def _normalize_base_url(base_url: str) -> str:
    """Ensure base_url ends with a versioned path for OpenAI-compatible APIs.

    The OpenAI Python client appends paths like /chat/completions directly
    to base_url, so base_url must already contain the version prefix (e.g.
    /v1). If the URL doesn't end with a version segment, /v1 is appended
    automatically so users don't need to add it manually.

    Args:
        base_url: The base URL to normalize

    Returns:
        Normalized base URL ending with a version segment
    """
    if not base_url:
        return base_url
    url = base_url.rstrip("/")
    # If the URL already ends with a version segment (e.g. /v1, /v2,
    # /compatible-mode/v1), leave it unchanged.
    if re.search(r"/v\d+$", url):
        return url
    logger.debug(
        "base_url '%s' has no version segment, appending /v1",
        url,
    )
    return url + "/v1"


def _create_remote_model_instance(
    llm_cfg: Optional["ResolvedModelConfig"],
    chat_model_class: Type[ChatModelBase],
) -> ChatModelBase:
    """Create a remote model instance with configuration.

    Args:
        llm_cfg: Resolved model configuration
        chat_model_class: Chat model class to instantiate

    Returns:
        Configured chat model instance
    """
    # Get configuration from llm_cfg or fall back to environment
    if llm_cfg and llm_cfg.api_key:
        model_name = llm_cfg.model or "qwen3-max"
        api_key = llm_cfg.api_key
        base_url = _normalize_base_url(llm_cfg.base_url)
    else:
        logger.warning(
            "No active LLM configured — "
            "falling back to DASHSCOPE_API_KEY env var",
        )
        model_name = "qwen3-max"
        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # Instantiate model
    model = chat_model_class(
        model_name,
        api_key=api_key,
        stream=True,
        client_kwargs={
            "base_url": base_url,
            "timeout": 300,  # 5-minute timeout to prevent indefinite hangs
        },
    )

    return model


def _create_formatter_instance(
    chat_model_class: Type[ChatModelBase],
) -> FormatterBase:
    """Create a formatter instance for the given chat model class.

    The formatter is enhanced with file block support for handling
    file outputs in tool results.

    Args:
        chat_model_class: The chat model class

    Returns:
        Formatter instance with file block support
    """
    base_formatter_class = _get_formatter_for_chat_model(chat_model_class)
    formatter_class = _create_file_block_support_formatter(
        base_formatter_class,
    )
    return formatter_class()


class LoggingChatModelProxy:
    """A transparent proxy that wraps any ChatModelBase instance to log
    all requests sent to the LLM and all responses received from it.

    The log is written to ~/.copaw/llm_messages.log via a RotatingFileHandler
    (see :mod:`copaw.utils.llm_logger`).

    Attribute access and mutations are forwarded to the wrapped model so the
    proxy is invisible to the rest of the codebase.
    """

    def __init__(self, model: ChatModelBase) -> None:
        # Use object.__setattr__ to avoid triggering our own __setattr__
        object.__setattr__(self, "_wrapped", model)

    # ------------------------------------------------------------------
    # Transparent attribute forwarding
    # ------------------------------------------------------------------

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_wrapped"), name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_wrapped":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_wrapped"), name, value)

    # ------------------------------------------------------------------
    # Logging __call__
    # ------------------------------------------------------------------

    async def __call__(self, messages, tools=None, **kwargs):
        """Intercept the model call to log request and response."""
        wrapped = object.__getattribute__(self, "_wrapped")
        model_name = getattr(wrapped, "model_name", "")

        # Log the outgoing request
        try:
            log_llm_request(messages, model_name=model_name, tools=tools)
        except Exception:
            pass  # Never let logging break the agent

        # Invoke the real model
        if tools is not None:
            result = await wrapped(messages, tools=tools, **kwargs)
        else:
            result = await wrapped(messages, **kwargs)

        # Distinguish streaming (AsyncGenerator) from non-streaming
        if isinstance(result, AsyncGenerator):
            return self._wrap_stream(result, model_name)
        else:
            try:
                log_llm_response(result, model_name=model_name)
            except Exception:
                pass
            return result

    async def _wrap_stream(
        self,
        stream: AsyncGenerator,
        model_name: str,
    ) -> AsyncGenerator:
        """Wrap an async generator to capture the last chunk for logging."""
        last_chunk = None
        try:
            async for chunk in stream:
                last_chunk = chunk
                yield chunk
        finally:
            # Log the final (complete) chunk which carries the full content
            try:
                log_llm_response(last_chunk, model_name=model_name)
            except Exception:
                pass


__all__ = [
    "create_model_and_formatter",
    "LoggingChatModelProxy",
]
