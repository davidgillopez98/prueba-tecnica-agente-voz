"""Persistencia SQLite mock en una tabla, con huella única sin límite temporal."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def claim_fingerprint(payload: dict) -> str:
    # Descripción y daños son texto libre. La demo deja la ubicación como texto libre;
    # en un agente real se normalizarían los nombres de localidad antes del hash.
    dedup_key = {
        "user_id": payload["user_id"],
        "policy_id": payload["policy_id"],
        "incident_date": payload["incident_date"],
        "location": payload["location"],
        "incident_type": payload["incident_type"],
    }
    return hashlib.sha256(json.dumps(dedup_key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ClaimRepository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(claims)")}
            if columns and "fingerprint" not in columns:
                raise ValueError(
                    "Esquema SQLite antiguo. Use otra ruta de base de datos o elimine el archivo para recrear la tabla."
                )
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                fingerprint TEXT PRIMARY KEY NOT NULL,
                claim_id TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def create(self, payload: dict) -> dict:
        """Devuelve el expediente y si se creó; un duplicado conserva todos sus datos."""
        fingerprint = claim_fingerprint(payload)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with closing(self._connect()) as db, db:
            # Serializa las escrituras para que búsqueda e inserción sean atómicas.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT claim_id, created_at, payload FROM claims WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing:
                return {"claim": self._public_claim(existing), "created": False}
            claim_id = "CLM-" + uuid4().hex[:12].upper()
            created_at = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO claims VALUES (?, ?, ?, ?)", (fingerprint, claim_id, created_at, serialized))
        return {
            "claim": {"claim_id": claim_id, "created_at": created_at, "status": "abierto"},
            "created": True,
        }

    def get_by_fingerprint(self, fingerprint: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT claim_id, created_at, payload FROM claims WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return self._public_claim(row) if row else None

    def get(self, claim_id: str) -> dict | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT claim_id, created_at, payload FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        return self._public_claim(row, include_payload=True) if row else None

    @staticmethod
    def _public_claim(row, include_payload: bool = False) -> dict:
        result = {"claim_id": row[0], "created_at": row[1], "status": "abierto"}
        if include_payload:
            result["incident"] = json.loads(row[2])
        return result
