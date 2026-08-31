import logging
from contextlib import contextmanager, nullcontext

import config

logger = logging.getLogger(__name__)


class Observability:

    def __init__(self):
        self._enabled = config.LANGFUSE_ENABLED
        self._handler = None
        self._client = None

        if not self._enabled:
            return

        if not config.LANGFUSE_PUBLIC_KEY or not config.LANGFUSE_SECRET_KEY:
            logger.warning("Langfuse enabled but API keys are missing — skipping")
            self._enabled = False
            return

        try:
            from langfuse import get_client
            from langfuse.langchain import CallbackHandler

            self._client = get_client()

            if self._client.auth_check():
                print("Langfuse client is authenticated and ready!")
            else:
                print("Authentication failed. Please check your credentials and host.")
                self._enabled = False
                return

            self._handler = CallbackHandler()
        except Exception as exc:
            logger.warning("Could not initialize Langfuse: %s", exc)
            self._enabled = False

    def get_handler(self):
        return self._handler

    def langchain_callbacks(self):
        """`callbacks` list for a LangChain/LangGraph config, or None when disabled."""
        return [self._handler] if self._handler else None

    def chat_turn(self, session_id: str, question: str, trace_id: str):
        """Context manager: root span for one chat turn (yields None when disabled).

        The root span's input/output become the trace-level input/output in
        Langfuse (question in, final answer out) instead of raw LangGraph state
        dicts; LangChain callback runs started inside the block nest beneath it
        (same thread, active OTel context). `propagate_attributes` stamps the
        session and the app's log trace_id onto the trace and every span, so
        `debug/pipeline.py` can keep resolving traces via metadata.trace_id.
        """
        if not self._enabled or self._client is None:
            return nullcontext(None)
        return self._chat_turn(session_id, question, trace_id)

    @contextmanager
    def _chat_turn(self, session_id: str, question: str, trace_id: str):
        from langfuse import propagate_attributes

        with propagate_attributes(
            session_id=session_id,
            metadata={"trace_id": trace_id},
        ), self._client.start_as_current_observation(
            as_type="span", name="chat-turn", input={"question": question}
        ) as root:
            yield root

    def flush(self):
        if self._client is not None:
            try:
                self._client.flush()
            except Exception:
                pass