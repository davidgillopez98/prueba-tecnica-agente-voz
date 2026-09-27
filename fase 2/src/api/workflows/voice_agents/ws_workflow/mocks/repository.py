"""Persistencia SQLite mock con idempotencia por solicitud y proximidad."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


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
    def __init__(self, path: Path, dedup_window: timedelta = timedelta(minutes=10)):
        self.path = path
        self.dedup_window = dedup_window
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS claims (
                request_id TEXT PRIMARY KEY,
                claim_id TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS claim_fingerprints (
                request_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                FOREIGN KEY (request_id) REFERENCES claims(request_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ix_claim_fingerprint ON claim_fingerprints(fingerprint)")
            db.execute("""CREATE TABLE IF NOT EXISTS claim_request_aliases (
                request_id TEXT PRIMARY KEY,
                original_request_id TEXT NOT NULL,
                FOREIGN KEY (original_request_id) REFERENCES claims(request_id)
            )""")
            # Recalcula huellas guardadas por versiones anteriores que incluían texto libre.
            for request_id, serialized, previous in db.execute("""SELECT c.request_id, c.payload, f.fingerprint
                FROM claims c LEFT JOIN claim_fingerprints f ON f.request_id = c.request_id""").fetchall():
                current = claim_fingerprint(json.loads(serialized))
                if previous != current:
                    db.execute("""INSERT INTO claim_fingerprints (request_id, fingerprint) VALUES (?, ?)
                        ON CONFLICT(request_id) DO UPDATE SET fingerprint = excluded.fingerprint""", (request_id, current))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def create(self, request_id: str, claim_id: str, created_at: str, payload: dict, fingerprint: str) -> dict:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            same_request = db.execute(
                "SELECT claim_id, created_at, payload FROM claims WHERE request_id = ?", (request_id,)
            ).fetchone()
            if same_request:
                if same_request[2] != serialized:
                    raise ValueError("El identificador de solicitud ya tiene otro contenido")
                return self._public_claim(same_request)
            alias = db.execute("""SELECT c.claim_id, c.created_at, c.payload
                FROM claim_request_aliases a JOIN claims c ON c.request_id = a.original_request_id
                WHERE a.request_id = ?""", (request_id,)).fetchone()
            if alias:
                if alias[2] != serialized:
                    raise ValueError("El identificador de solicitud ya tiene otro contenido")
                return self._public_claim(alias)
            cutoff = (datetime.fromisoformat(created_at) - self.dedup_window).isoformat()
            duplicate = db.execute("""SELECT c.claim_id, c.created_at, c.payload, c.request_id
                FROM claims c JOIN claim_fingerprints f ON f.request_id = c.request_id
                WHERE f.fingerprint = ? AND c.created_at >= ?
                ORDER BY c.created_at DESC LIMIT 1""", (fingerprint, cutoff)).fetchone()
            if duplicate:
                db.execute("INSERT INTO claim_request_aliases VALUES (?, ?)", (request_id, duplicate[3]))
                return self._public_claim(duplicate)
            db.execute("INSERT INTO claims VALUES (?, ?, ?, ?)", (request_id, claim_id, created_at, serialized))
            db.execute("INSERT INTO claim_fingerprints VALUES (?, ?)", (request_id, fingerprint))
        return {"claim_id": claim_id, "created_at": created_at, "status": "abierto"}

    def get_by_request_id(self, request_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT claim_id, created_at, payload FROM claims WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                row = db.execute("""SELECT c.claim_id, c.created_at, c.payload
                    FROM claim_request_aliases a JOIN claims c ON c.request_id = a.original_request_id
                    WHERE a.request_id = ?""", (request_id,)).fetchone()
        return self._public_claim(row) if row else None

    def get(self, claim_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT claim_id, created_at, payload FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        return self._public_claim(row, include_payload=True) if row else None

    @staticmethod
    def _public_claim(row, include_payload: bool = False) -> dict:
        result = {"claim_id": row[0], "created_at": row[1], "status": "abierto"}
        if include_payload:
            result["incident"] = json.loads(row[2])
        return result
