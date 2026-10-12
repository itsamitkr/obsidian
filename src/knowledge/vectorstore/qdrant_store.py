"""Qdrant-backed vector store (default recommended backend).

Embedded on disk (``qdrant_local``) or a Qdrant server. Requires the ``vector``
extra (qdrant-client). Point ids are UUID5-derived from the chunk id so
Qdrant's id constraints are satisfied while remaining deterministic.
"""

from __future__ import annotations

import atexit
import uuid

from ..config import VectorConfig
from .base import VectorMatch, VectorPoint, VectorStore

_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _point_uuid(chunk_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, chunk_id))


class QdrantVectorStore(VectorStore):
    name = "qdrant"

    def __init__(self, cfg: VectorConfig, dim: int, local_path: str | None = None):
        try:
            from qdrant_client import QdrantClient  # type: ignore
            from qdrant_client.http import models as qm  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "qdrant-client not installed. Install with "
                "`pip install 'knowledge-system[vector]'`."
            ) from exc

        self._qm = qm
        # Two deployment modes:
        #   * server mode  -> cfg.url (a Qdrant server you run)
        #   * local mode   -> on-disk persistence, no server required
        # Local mode keeps the same Qdrant API (filters, payload indexes) and
        # persists under the vault's derived-state directory.
        if local_path:
            self._client = QdrantClient(path=local_path)
        else:
            self._client = QdrantClient(url=cfg.url, api_key=cfg.api_key or None)
        self._collection = cfg.collection
        self._dim = dim
        self._distance = {
            "cosine": qm.Distance.COSINE,
            "dot": qm.Distance.DOT,
            "euclid": qm.Distance.EUCLID,
        }[cfg.distance]
        self._local = bool(local_path)
        self._ensure_collection()
        # Close before interpreter teardown so qdrant-client's own __del__
        # doesn't fire after imports are gone and print a spurious traceback.
        atexit.register(self.close)

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=self._qm.VectorParams(
                    size=self._dim, distance=self._distance
                ),
            )
            # Index on document_id for efficient delete-by-document. Local
            # (embedded) Qdrant ignores payload indexes, so skip it there to
            # avoid a noisy no-op warning.
            if not self._local:
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name="document_id",
                    field_schema=self._qm.PayloadSchemaType.KEYWORD,
                )

    def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        self._client.upsert(
            collection_name=self._collection,
            points=[
                self._qm.PointStruct(
                    id=_point_uuid(p.id),
                    vector=p.vector,
                    payload={**p.payload, "chunk_id": p.id},
                )
                for p in points
            ],
        )

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        self._client.delete(
            collection_name=self._collection,
            points_selector=self._qm.PointIdsList(
                points=[_point_uuid(i) for i in ids]
            ),
        )

    def delete_by_document(self, document_id: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            points_selector=self._qm.FilterSelector(
                filter=self._qm.Filter(
                    must=[
                        self._qm.FieldCondition(
                            key="document_id",
                            match=self._qm.MatchValue(value=document_id),
                        )
                    ]
                )
            ),
        )

    def _build_filter(self, filters: dict | None):
        if not filters:
            return None
        return self._qm.Filter(
            must=[
                self._qm.FieldCondition(
                    key=k, match=self._qm.MatchValue(value=v)
                )
                for k, v in filters.items()
            ]
        )

    def search(
        self,
        vector: list[float],
        limit: int = 10,
        filters: dict | None = None,
    ) -> list[VectorMatch]:
        query_filter = self._build_filter(filters)
        # qdrant-client >= 1.10 replaced the deprecated `search()` with
        # `query_points()` (and removed it in later versions). Support both.
        if hasattr(self._client, "query_points"):
            res = self._client.query_points(
                collection_name=self._collection,
                query=vector,
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            ).points
        else:  # pragma: no cover - older clients
            res = self._client.search(
                collection_name=self._collection,
                query_vector=vector,
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )
        return [
            VectorMatch(
                id=(r.payload or {}).get("chunk_id", str(r.id)),
                score=float(r.score),
                payload=r.payload or {},
            )
            for r in res
        ]

    def get(self, id: str) -> VectorPoint | None:
        res = self._client.retrieve(
            collection_name=self._collection,
            ids=[_point_uuid(id)],
            with_payload=True,
            with_vectors=True,
        )
        if not res:
            return None
        r = res[0]
        return VectorPoint(id=id, vector=r.vector or [], payload=r.payload or {})

    def rebuild(self) -> None:
        """Empty the collection so it can be repopulated from Markdown.

        We delete every point rather than dropping the collection: in embedded
        mode a dropped collection can be reloaded from its on-disk storage,
        leaving stale vectors behind. Deleting points goes through the normal
        write path and persists correctly in both embedded and server modes.

        The result is verified, because a silently-failed rebuild would break
        the guarantee that derived indexes are reconstructible from Markdown.
        """
        self._ensure_collection()
        self._client.delete(
            collection_name=self._collection,
            # An empty filter matches every point.
            points_selector=self._qm.FilterSelector(filter=self._qm.Filter()),
        )
        remaining = self._client.count(
            collection_name=self._collection, exact=True
        ).count
        if remaining:
            raise RuntimeError(
                f"Qdrant collection '{self._collection}' still holds "
                f"{remaining} point(s) after rebuild. If another process holds "
                "the embedded store, stop it and retry."
            )

    def health_check(self) -> bool:
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Release the local storage lock (embedded mode)."""
        client, self._client = getattr(self, "_client", None), None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            pass

    def __del__(self):
        # qdrant-client's own __del__ can raise during interpreter shutdown
        # ("sys.meta_path is None"). Closing explicitly (and idempotently)
        # keeps CLI output clean without changing behaviour.
        self.close()
