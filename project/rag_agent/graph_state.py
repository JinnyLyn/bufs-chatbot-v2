from typing import List, Annotated, Set
from langgraph.graph import MessagesState
import operator

def accumulate_or_reset(existing: List[dict], new: List[dict]) -> List[dict]:
    if new and any(item.get('__reset__') for item in new):
        return []
    return existing + new

def set_union(a: Set[str], b: Set[str]) -> Set[str]:
    return a | b

class State(MessagesState):
    """State for main agent graph"""
    questionIsClear: bool = False
    conversation_summary: str = ""
    originalQuery: str = ""
    rewrittenQuestions: List[str] = []
    agent_answers: Annotated[List[dict], accumulate_or_reset] = []
    # Issue #145: user conditions extracted from the question (UserSlots.model_dump();
    # {} when SLOT_EXTRACTION_ENABLED is off or nothing was stated).
    userSlots: dict = {}

class AgentState(MessagesState):
    """State for individual agent subgraph"""
    question: str = ""
    question_index: int = 0
    context_summary: str = ""
    retrieval_keys: Annotated[Set[str], set_union] = set()
    # #177 P2: "Parent ID:" lines live only inside ToolMessages, which compress_context
    # deletes — harvested there so synthesis-time parent expansion still sees chunks
    # from compressed-away iterations. First-seen order; compress_context is the only writer.
    observed_parent_ids: List[str] = []
    # #177 P1: set by clean_synthesis so aggregate_answers can tell that the answer is
    # already a final single-shot synthesis and skip the degrading LLM re-pass.
    clean_synthesized: bool = False
    final_answer: str = ""
    agent_answers: List[dict] = []
    tool_call_count: Annotated[int, operator.add] = 0
    iteration_count: Annotated[int, operator.add] = 0
    # #89: monotonic timestamp of the subgraph's first orchestrator turn — the reference
    # point for the TOOL_CALL_SOFT_TIMEOUT_S elapsed check. 0.0 = not armed (lever off).
    loop_started_at: float = 0.0
