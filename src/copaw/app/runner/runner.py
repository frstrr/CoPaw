# -*- coding: utf-8 -*-
# pylint: disable=unused-argument too-many-branches too-many-statements
import asyncio
import json
import logging
import logging.handlers
from pathlib import Path

from agentscope.pipeline import stream_printing_messages
from agentscope_runtime.engine.runner import Runner
from agentscope_runtime.engine.schemas.agent_schemas import AgentRequest
from dotenv import load_dotenv

from .query_error_dump import write_query_error_dump
from .session import SafeJSONSession
from .utils import build_env_context
from ..channels.schema import DEFAULT_CHANNEL
from ...agents.memory import MemoryManager
from ...agents.react_agent import CoPawAgent
from ...config import load_config
from ...constant import WORKING_DIR
from ...utils.llm_logger import set_llm_log_session

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dedicated debug logger for model I/O — writes to ~/.copaw/debug_model.log
# ---------------------------------------------------------------------------
_MODEL_DEBUG_LOG = Path.home() / ".copaw" / "debug_model.log"
_MODEL_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)

_model_debug_logger = logging.getLogger("copaw.model_debug")
_model_debug_logger.setLevel(logging.DEBUG)
_model_debug_logger.propagate = False  # Don't leak into root logger
if not _model_debug_logger.handlers:
    _mh = logging.handlers.RotatingFileHandler(
        str(_MODEL_DEBUG_LOG),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=2,
        encoding="utf-8",
    )
    _mh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    _model_debug_logger.addHandler(_mh)


def _log_msg_blocks(label: str, msg) -> None:
    """Log content blocks of a Msg for debugging encoding issues."""
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        _model_debug_logger.debug(
            "%s | role=%s content_type=str len=%s fffd=%s preview=%r",
            label,
            getattr(msg, "role", "?"),
            len(str(content)),
            str(content).count("\ufffd"),
            str(content)[:120],
        )
        return
    for i, blk in enumerate(content):
        if not isinstance(blk, dict):
            _model_debug_logger.debug(
                "%s | block[%d] not-dict type=%s", label, i, type(blk)
            )
            continue
        btype = blk.get("type", "?")
        if btype == "thinking":
            txt = blk.get("thinking", "")
            _model_debug_logger.debug(
                "%s | block[%d] type=thinking len=%d fffd=%d preview=%r",
                label, i, len(txt), txt.count("\ufffd"), txt[:120],
            )
        elif btype == "text":
            txt = blk.get("text", "")
            _model_debug_logger.debug(
                "%s | block[%d] type=text len=%d fffd=%d preview=%r",
                label, i, len(txt), txt.count("\ufffd"), txt[:120],
            )
        else:
            _model_debug_logger.debug(
                "%s | block[%d] type=%s", label, i, btype
            )


def _log_agent_memory(label: str, agent) -> None:
    """Log assistant messages in agent memory to check for encoding issues."""
    try:
        memory_content = agent.memory.content
    except Exception as exc:
        _model_debug_logger.debug("%s | cannot read memory: %s", label, exc)
        return
    for i, item in enumerate(memory_content):
        msg = item[0] if isinstance(item, (list, tuple)) else item
        role = getattr(msg, "role", "?")
        if role != "assistant":
            continue
        _log_msg_blocks(f"{label}/memory[{i}]", msg)


class AgentRunner(Runner):
    def __init__(self) -> None:
        super().__init__()
        self.framework_type = "agentscope"
        self._chat_manager = None  # Store chat_manager reference
        self._mcp_manager = None  # MCP client manager for hot-reload

        self.memory_manager: MemoryManager | None = None

    def set_chat_manager(self, chat_manager):
        """Set chat manager for auto-registration.

        Args:
            chat_manager: ChatManager instance
        """
        self._chat_manager = chat_manager

    def set_mcp_manager(self, mcp_manager):
        """Set MCP client manager for hot-reload support.

        Args:
            mcp_manager: MCPClientManager instance
        """
        self._mcp_manager = mcp_manager

    async def query_handler(
        self,
        msgs,
        request: AgentRequest = None,
        **kwargs,
    ):
        """
        Handle agent query.
        """

        agent = None
        chat = None

        try:
            session_id = request.session_id
            user_id = request.user_id
            channel = getattr(request, "channel", DEFAULT_CHANNEL)

            logger.info(
                "Handle agent query:\n%s",
                json.dumps(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "channel": channel,
                        "msgs_len": len(msgs) if msgs else 0,
                        "msgs_str": str(msgs)[:300] + "...",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )

            # Bind this session to the per-session LLM log file
            set_llm_log_session(session_id)

            env_context = build_env_context(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                working_dir=str(WORKING_DIR),
            )

            # Get MCP clients from manager (hot-reloadable)
            mcp_clients = []
            if self._mcp_manager is not None:
                mcp_clients = await self._mcp_manager.get_clients()

            config = load_config()
            max_iters = config.agents.running.max_iters
            max_input_length = config.agents.running.max_input_length

            agent = CoPawAgent(
                env_context=env_context,
                mcp_clients=mcp_clients,
                memory_manager=self.memory_manager,
                max_iters=max_iters,
                max_input_length=max_input_length,
            )
            await agent.register_mcp_clients()
            agent.set_console_output_enabled(enabled=False)

            logger.debug(
                f"Agent Query msgs {msgs}",
            )

            name = "New Chat"
            if len(msgs) > 0:
                content = msgs[0].get_text_content()
                if content:
                    name = msgs[0].get_text_content()[:10]
                else:
                    name = "Media Message"

            if self._chat_manager is not None:
                chat = await self._chat_manager.get_or_create_chat(
                    session_id,
                    user_id,
                    channel,
                    name=name,
                )

            await self.session.load_session_state(
                session_id=session_id,
                user_id=user_id,
                agent=agent,
            )

            # Rebuild system prompt so it always reflects the latest
            # AGENTS.md / SOUL.md / PROFILE.md, not the stale one saved
            # in the session state.
            agent.rebuild_sys_prompt()

            _model_debug_logger.debug(
                "=== QUERY START session=%s ===", session_id
            )
            async for msg, last in stream_printing_messages(
                agents=[agent],
                coroutine_task=agent(msgs),
            ):
                # CHECKPOINT-1: log only the final chunk of each message
                if last:
                    _log_msg_blocks(
                        f"CP1:stream_final session={session_id}", msg
                    )
                yield msg, last

        except asyncio.CancelledError:
            if agent is not None:
                await agent.interrupt()
            raise
        except Exception as e:
            debug_dump_path = write_query_error_dump(
                request=request,
                exc=e,
                locals_=locals(),
            )
            path_hint = (
                f"\n(Details:  {debug_dump_path})" if debug_dump_path else ""
            )
            logger.exception(f"Error in query handler: {e}{path_hint}")
            if debug_dump_path:
                setattr(e, "debug_dump_path", debug_dump_path)
                if hasattr(e, "add_note"):
                    e.add_note(
                        f"(Details:  {debug_dump_path})",
                    )
                suffix = f"\n(Details:  {debug_dump_path})"
                e.args = (
                    (f"{e.args[0]}{suffix}" if e.args else suffix.strip()),
                ) + e.args[1:]
            raise
        finally:
            if agent is not None:
                # CHECKPOINT-2: inspect memory before session save
                _log_agent_memory(
                    f"CP2:pre_save session={session_id}", agent
                )
                await self.session.save_session_state(
                    session_id=session_id,
                    user_id=user_id,
                    agent=agent,
                )

            if self._chat_manager is not None and chat is not None:
                await self._chat_manager.update_chat(chat)

    async def init_handler(self, *args, **kwargs):
        """
        Init handler.
        """
        # Load environment variables from .env file
        env_path = Path(__file__).resolve().parents[4] / ".env"
        if env_path.exists():
            load_dotenv(env_path)
            logger.debug(f"Loaded environment variables from {env_path}")
        else:
            logger.debug(
                f".env file not found at {env_path}, "
                "using existing environment variables",
            )

        session_dir = str(WORKING_DIR / "sessions")
        self.session = SafeJSONSession(save_dir=session_dir)

        try:
            if self.memory_manager is None:
                self.memory_manager = MemoryManager(
                    working_dir=str(WORKING_DIR),
                )
            await self.memory_manager.start()
        except Exception as e:
            logger.exception(f"MemoryManager start failed: {e}")

    async def shutdown_handler(self, *args, **kwargs):
        """
        Shutdown handler.
        """
        try:
            await self.memory_manager.close()
        except Exception as e:
            logger.warning(f"MemoryManager stop failed: {e}")
