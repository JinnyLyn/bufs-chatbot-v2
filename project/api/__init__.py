"""FastAPI bridge exposing the agentic-RAG LangGraph pipeline to the CamChat frontend.

The existing `project/` core (LangGraph agent, Qdrant hybrid retrieval, parent/child
chunking) is reused unchanged. This package only adds the HTTP/SSE surface that the
CamChat Next.js chat UI expects:

  POST /api/session            -> create a session (session_id == LangGraph thread_id)
  GET  /api/session/{id}       -> session info
  GET  /api/chat/stream        -> SSE: token / clear / status / done / error events
"""
