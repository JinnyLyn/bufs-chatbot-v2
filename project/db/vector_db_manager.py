import config
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_qdrant import QdrantVectorStore, FastEmbedSparse, RetrievalMode
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

# Korean morphological strategies handled by our custom BM25 sparse embedding;
# any other SPARSE_MODEL value is treated as a FastEmbed model id.
_KOREAN_SPARSE_STRATEGIES = {"kiwi", "okt", "whitespace"}


def _build_sparse_embeddings():
    if config.SPARSE_MODEL in _KOREAN_SPARSE_STRATEGIES:
        from db.korean_sparse import KoreanBM25Sparse
        return KoreanBM25Sparse(config.SPARSE_MODEL)
    if config.SPARSE_MODEL == "bm42_kiwi":
        from db.korean_sparse import Bm42KiwiSparse
        return Bm42KiwiSparse()
    return FastEmbedSparse(model_name=config.SPARSE_MODEL)


class VectorDbManager:
    __client: QdrantClient
    __dense_embeddings: HuggingFaceEmbeddings
    def __init__(self):
        self.__client = QdrantClient(path=config.QDRANT_DB_PATH)
        self.__dense_embeddings = HuggingFaceEmbeddings(
            model_name=config.DENSE_MODEL,
            model_kwargs={"device": config.EMBEDDING_DEVICE},
        )
        self.__sparse_embeddings = _build_sparse_embeddings()

    def create_collection(self, collection_name):
        if not self.__client.collection_exists(collection_name):
            print(f"Creating collection: {collection_name}...")
            # BM25 sparse vectors are TF-only; modifier="idf" makes Qdrant apply the
            # IDF part server-side for a proper BM25 score. Enabled via SPARSE_IDF.
            sparse_modifier = qmodels.Modifier.IDF if config.SPARSE_IDF else None
            self.__client.create_collection(
                collection_name=collection_name,
                vectors_config=qmodels.VectorParams(size=len(self.__dense_embeddings.embed_query("test")), distance=qmodels.Distance.COSINE),
                sparse_vectors_config={config.SPARSE_VECTOR_NAME: qmodels.SparseVectorParams(modifier=sparse_modifier)},
            )
            print(f"✓ Collection created: {collection_name}")
        else:
            print(f"✓ Collection already exists: {collection_name}")

    def delete_collection(self, collection_name):
        try:
            if self.__client.collection_exists(collection_name):
                print(f"Removing existing Qdrant collection: {collection_name}")
                self.__client.delete_collection(collection_name)
        except Exception as e:
            print(f"Warning: could not delete collection {collection_name}: {e}")

    def get_collection(self, collection_name) -> QdrantVectorStore:
        try:
            return QdrantVectorStore(
                    client=self.__client,
                    collection_name=collection_name,
                    embedding=self.__dense_embeddings,
                    sparse_embedding=self.__sparse_embeddings,
                    retrieval_mode=RetrievalMode.HYBRID,
                    sparse_vector_name=config.SPARSE_VECTOR_NAME
                )
        except Exception as e:
            print(f"Unable to get collection {collection_name}: {e}")