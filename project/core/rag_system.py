import logging
import uuid
from langchain_ollama import ChatOllama
import config
from db.vector_db_manager import VectorDbManager
from db.parent_store_manager import ParentStoreManager
from document_chunker import DocumentChuncker
from rag_agent.tools import ToolFactory
from rag_agent.graph import create_agent_graph
from core.observability import Observability

logger = logging.getLogger(__name__)


class RAGSystem:

    def __init__(self, collection_name=config.CHILD_COLLECTION):
        self.collection_name = collection_name
        self.vector_db = VectorDbManager()
        self.parent_store = ParentStoreManager()
        self.chunker = DocumentChuncker()
        self.observability = Observability()
        self.agent_graph = None
        self.llm = None  # exposed for post-hoc tasks (e.g. answer translation)
        self.thread_id = str(uuid.uuid4())
        self.recursion_limit = config.GRAPH_RECURSION_LIMIT

    def initialize(self):
        self.vector_db.create_collection(self.collection_name)
        collection = self.vector_db.get_collection(self.collection_name)

        # Sampling/runtime options MUST be passed to the constructor: langchain-ollama 1.1.0
        # only reads them from self.* into the request `options` dict (.with_config/.bind do not
        # apply). num_predict/repeat_penalty cap runaway generation; keep_alive avoids cold reloads.
        _llm_kwargs = dict(
            model=config.LLM_MODEL,
            temperature=config.LLM_TEMPERATURE,
            reasoning=config.LLM_REASONING,
            num_ctx=config.LLM_NUM_CTX,
            num_predict=config.LLM_NUM_PREDICT,
            repeat_penalty=config.LLM_REPEAT_PENALTY,
            keep_alive=config.LLM_KEEP_ALIVE,
        )
        if config.OLLAMA_BASE_URL:
            _llm_kwargs["base_url"] = config.OLLAMA_BASE_URL  # local Ollama (RTX 4070)
        llm = ChatOllama(**_llm_kwargs)
        self.llm = llm
        # Warm the model into VRAM at startup so the first user request doesn't pay a cold reload
        # (cold-start was the 11.5 s rewrite_query / 43 s trace outlier). Best-effort; never fatal.
        # IMPORTANT: warm up with the SAME runtime options as real requests (no options= override).
        # Passing options={...} would replace the constructor's self.* options (dropping num_ctx),
        # so Ollama would load the model at a different num_ctx and then RELOAD it on the first real
        # request — defeating the warmup. num_predict=2048 is just a ceiling; "warmup" stops at EOS.
        if config.LLM_WARMUP:
            try:
                llm.invoke("warmup")
            except Exception as exc:  # noqa: BLE001 — startup must not fail on a warmup ping
                logger.warning("LLM warmup ping failed (continuing): %s", exc)
        tools = ToolFactory(collection).create_tools()
        self.agent_graph = create_agent_graph(llm, tools, collection=collection)

    def get_config(self):
        cfg = {"configurable": {"thread_id": self.thread_id}, "recursion_limit": self.recursion_limit}
        callbacks = self.observability.langchain_callbacks()
        if callbacks:
            cfg["callbacks"] = callbacks
        return cfg

    def reset_thread(self):
        try:
            self.agent_graph.checkpointer.delete_thread(self.thread_id)
        except Exception as e:
            print(f"Warning: Could not delete thread {self.thread_id}: {e}")
        self.thread_id = str(uuid.uuid4())