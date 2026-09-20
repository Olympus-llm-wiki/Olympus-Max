"""Bounded native representations of immutable canonical source versions.

Planning and enabling are explicit. The original document/operation binding is
never overwritten, and unfinished generations never replace a searchable one.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from .native_proof import CompletionProofs, profile_fingerprint
from .preservation import Store, PreservationError, canonical, digest, timestamp, guard_no_secrets


def _exists(store: Store) -> bool:
    with store.connect() as db:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_representations'").fetchone() is not None


def native_targets_for_versions(store: Store, version_ids) -> list[dict]:
    """All generations, including incomplete ones, for withdrawal/reconciliation."""
    ids = set(version_ids)
    with store.connect() as db:
        rows = [dict(r) for r in db.execute("SELECT version_id,version_id AS document_id,operation_id FROM delivery")
                if r["version_id"] in ids]
        if _exists(store):
            plans = list(db.execute("SELECT version_id,manifest_json,manifest_sha256 FROM native_representations"))
            for plan in plans:
                if plan["version_id"] not in ids:
                    continue
                if digest(plan["manifest_json"].encode()) != plan["manifest_sha256"]:
                    raise PreservationError("representation_manifest_mismatch")
                manifest = json.loads(plan["manifest_json"])
                if manifest["version_id"] != plan["version_id"]:
                    raise PreservationError("representation_manifest_mismatch")
                # Withdrawal uses the immutable manifest's identities, not a
                # mutable working row whose document binding could be damaged.
                rows.extend({"version_id": plan["version_id"], "document_id": part["document_id"],
                             "operation_id": part["operation_id"]}
                            for part in manifest["parts"] if part["requires_native"])
    return rows


def native_document_map(store: Store, version_ids) -> dict[str, dict]:
    """Only published complete generations; callers recheck canonical withdrawals."""
    ids = set(version_ids)
    result = {vid: {"version_id": vid, "document_id": vid, "profile": "legacy"} for vid in ids}
    if not _exists(store):
        return _with_projection_mappings(store, result)
    with store.connect() as db:
        rows = list(db.execute("""SELECT r.version_id,r.kind,r.id,r.profile_fingerprint,p.document_id,
            p.char_start,p.char_end,p.byte_start,p.byte_end FROM native_representation_parts p
            JOIN native_representations r ON r.id=p.representation_id
            JOIN versions v ON v.id=r.version_id JOIN sources s ON s.id=v.source_id
            WHERE r.published=1 AND r.state='complete' AND p.state='complete' AND p.requires_native=1
              AND v.active=1 AND s.forgotten_at IS NULL"""))
    for row in rows:
        if row["version_id"] not in ids:
            continue
        if row["kind"] == "native_index":
            result.pop(row["version_id"], None)
        result[row["document_id"]] = {"version_id": row["version_id"], "document_id": row["document_id"],
            "representation_id": row["id"], "profile": row["profile_fingerprint"],
            "char_start": row["char_start"], "char_end": row["char_end"],
            "byte_start": row["byte_start"], "byte_end": row["byte_end"]}
    return _with_projection_mappings(store, result)


def _with_projection_mappings(store: Store, result: dict) -> dict:
    """Native offsets refer to projected text; expose its exact deletion map."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='native_completion_receipts'").fetchone():
            return result
        rows, document_ids = [], list(result)
        for start in range(0, len(document_ids), 500):
            batch = document_ids[start:start+500]
            rows.extend(db.execute('SELECT * FROM native_completion_receipts WHERE document_id IN ('
                                   + ','.join('?' for _ in batch) + ')', batch))
    for row in rows:
        binding = result.get(row['document_id'])
        if binding is None or row['version_id'] != binding['version_id']:
            continue
        proof = CompletionProofs.verify_row(row, version_id=row['version_id'], document_id=row['document_id'],
            operation_id=row['operation_id'], text_sha256=row['text_sha256'], profile=row['profile_fingerprint'])
        projection = proof['readback'].get('native_text_projection') if proof else None
        if projection and projection['removed_chars']:
            binding['native_text_projection'] = projection
            binding['projection_coordinates'] = 'document_local; add canonical part offsets when present'
    return result


def inflight_parts(store: Store) -> int:
    if not _exists(store):
        return 0
    with store.connect() as db:
        return db.execute("""SELECT count(*) FROM native_representation_parts p
            JOIN native_representations r ON r.id=p.representation_id
            JOIN versions v ON v.id=r.version_id JOIN sources s ON s.id=v.source_id
            WHERE r.enabled=1 AND v.active=1 AND s.forgotten_at IS NULL
              AND (p.state='submitted' OR (p.state='pending' AND p.attempts>0))""").fetchone()[0]


def representation_status(store: Store, version_id: str) -> dict:
    """Read-only public identities; declared parts are not claimed to exist remotely."""
    with store.connect() as db:
        delivery = db.execute("SELECT operation_id,state,last_error FROM delivery WHERE version_id=?", (version_id,)).fetchone()
    if delivery is None:
        raise PreservationError("unknown_version")
    from .native_proof import completion_readiness
    manifest = store.read_manifest(version_id)
    legacy_readiness = completion_readiness(store, version_id=version_id, document_id=version_id,
        operation_id=delivery["operation_id"], text_sha256=manifest["text_sha256"],
        profile=profile_fingerprint(manifest["metadata"].get("retain_strategy")))
    result = {"version_id": version_id, "canonical_readiness": delivery["state"],
              "waiting_reason": delivery["last_error"],
              "legacy_binding": {"document_id": version_id, "operation_id": delivery["operation_id"],
                                 "used_by_current_primary": True, "completion": legacy_readiness}, "representations": [],
              "identity_notice": "Canonical version IDs remain stable; actual native documents may be representation parts."}
    review = store.setting("delivery_format:" + version_id)
    if review is not None:
        result["format_review"] = json.loads(review)
    if not _exists(store):
        result["mode"] = "legacy"
        return result
    # No constructor/DDL in this inspection path.
    reader = object.__new__(Representations)
    reader.store = store
    with store.connect() as db:
        plans = [r[0] for r in db.execute("SELECT id FROM native_representations WHERE version_id=? ORDER BY created_at,id", (version_id,))]
    for identifier in plans:
        plan = reader.get(identifier)
        counts = {}
        for part in plan["parts"]:
            counts[part["state"]] = counts.get(part["state"], 0) + 1
        native = plan["manifest"]["profile"]["native"]
        result["representations"].append({"representation_id": identifier, "kind": plan["kind"],
            "generation": plan["generation"], "state": plan["state"], "enabled": bool(plan["enabled"]),
            "published": bool(plan["published"]), "profile_fingerprint": plan["profile_fingerprint"],
            "mode": native["mode"], "executor": native.get("executor"),
            "execution_profile_known": native.get("execution_profile_known", False), "part_counts": counts,
            "parts": [{key: part[key] for key in ("part_index", "document_id", "operation_id", "state", "attempts",
                       "requires_native", "char_start", "char_end", "byte_start", "byte_end", "text_sha256", "last_error")}
                      for part in plan["parts"]]})
        if plan["kind"] == "native_index" and (plan["enabled"] or plan["published"]):
            result["legacy_binding"]["used_by_current_primary"] = False
    result["mode"] = "representations" if result["representations"] else "legacy"
    return result


class Representations:
    """Additive versioned schema, created only at an explicit bootstrap boundary."""

    def __init__(self, store: Store):
        self.store = store
        with store.exclusive(), store.connect(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS native_representations (
                id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                version_id TEXT NOT NULL REFERENCES versions(id), kind TEXT NOT NULL,
                generation TEXT NOT NULL DEFAULT 'initial',
                profile_json TEXT NOT NULL, profile_fingerprint TEXT NOT NULL,
                manifest_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'planned', enabled INTEGER NOT NULL DEFAULT 0,
                published INTEGER NOT NULL DEFAULT 0, last_served REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, UNIQUE(version_id,kind,profile_fingerprint,generation)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS native_representation_parts (
                representation_id TEXT NOT NULL REFERENCES native_representations(id),
                part_index INTEGER NOT NULL, document_id TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL UNIQUE, text_sha256 TEXT NOT NULL,
                char_start INTEGER NOT NULL,char_end INTEGER NOT NULL,
                byte_start INTEGER NOT NULL,byte_end INTEGER NOT NULL,
                requires_native INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                lease_id TEXT, proof_json TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(representation_id,part_index)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS native_parts_pending ON native_representation_parts(state,next_attempt,lease_until)")
        self.proofs = CompletionProofs(store)

    def prepare(self, version_id: str, *, profile: dict, kind: str = "native_index",
                max_part_chars: int = 12000, max_part_bytes: int = 48000, generation: str = "initial") -> dict:
        if kind not in {"native_index", "enrichment"}:
            raise PreservationError("invalid_representation_kind")
        if not isinstance(generation, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", generation):
            raise PreservationError("invalid_representation_generation")
        if (type(max_part_chars) is not int or not 1 <= max_part_chars <= 50000
                or type(max_part_bytes) is not int or not 4 <= max_part_bytes <= 200000):
            raise PreservationError("invalid_representation_bounds")
        if not isinstance(profile, dict) or profile.get("mode") not in {"concise", "verbose", "custom", "verbatim", "chunks"}:
            raise PreservationError("invalid_representation_profile")
        if profile.get("execution_profile_known") is not True:
            raise PreservationError("representation_executor_profile_unknown")
        guard_no_secrets(canonical(profile))
        if profile["mode"] == "chunks" and profile.get("bank_auto_consolidation") is not False and profile.get("bank_observations") is not False:
            raise PreservationError("chunks_consolidation_not_disabled")
        version = self.store.read_text_version(version_id)
        from .materials import material_profile
        if not material_profile(version)["indexable"]:
            raise PreservationError("representation_source_not_indexable")
        policy = {"schema": 1, "kind": kind, "native": profile,
                  "max_part_chars": max_part_chars, "max_part_bytes": max_part_bytes,
                  "splitter": "utf8-contiguous-v1"}
        fingerprint = digest(canonical(policy))
        identifier = "olrep-" + digest(canonical([version_id, fingerprint, generation]))
        text = version["text"]
        parts = []
        start = byte_start = 0
        # Four bytes is the maximum UTF-8 width; this bound cannot overshoot.
        step = min(max_part_chars, max_part_bytes // 4)
        while start < len(text):
            end = min(len(text), start + step)
            if end < len(text):
                boundary = text.rfind("\n", start + step // 2, end)
                if boundary >= start:
                    end = boundary + 1
            content = text[start:end]
            raw = content.encode()
            part_index = len(parts)
            part_id = "olr-" + digest(canonical([identifier, part_index, digest(raw)]))
            parts.append({"part_index": part_index, "document_id": part_id,
                "operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "olympus:" + part_id)),
                "text_sha256": digest(raw), "char_start": start, "char_end": end,
                "byte_start": byte_start, "byte_end": byte_start + len(raw),
                "requires_native": int(bool(content.strip()))})
            start, byte_start = end, byte_start + len(raw)
        manifest = {"schema": 1, "representation_id": identifier, "version_id": version_id,
            "generation": generation,
            "source_text_sha256": version["text_sha256"], "original_sha256": version["original_sha256"],
            "text_chars": len(text), "text_bytes": byte_start, "profile": policy,
            "profile_fingerprint": fingerprint, "parts": parts}
        encoded = canonical(manifest)
        with self.store.exclusive(), self.store.connect(write=True) as db:
            active = db.execute("SELECT active FROM versions WHERE id=?", (version_id,)).fetchone()
            if active is None or not active[0]:
                raise PreservationError("version_not_active")
            db.execute("""INSERT INTO native_representations
                (id,schema_version,version_id,kind,generation,profile_json,profile_fingerprint,manifest_json,manifest_sha256,created_at)
                VALUES(?,1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
                (identifier, version_id, kind, generation, canonical(policy).decode(), fingerprint,
                 encoded.decode(), digest(encoded), timestamp()))
            for part in parts:
                db.execute("""INSERT INTO native_representation_parts
                    (representation_id,part_index,document_id,operation_id,text_sha256,char_start,char_end,
                     byte_start,byte_end,requires_native,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(representation_id,part_index) DO NOTHING""",
                    (identifier, *(part[key] for key in ("part_index", "document_id", "operation_id", "text_sha256",
                     "char_start", "char_end", "byte_start", "byte_end", "requires_native")), timestamp()))
        return self.get(identifier)

    def get(self, representation_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM native_representations WHERE id=?", (representation_id,)).fetchone()
            if row is None:
                raise PreservationError("unknown_representation")
            parts = [dict(r) for r in db.execute("SELECT * FROM native_representation_parts WHERE representation_id=? ORDER BY part_index",
                                               (representation_id,))]
        if row["schema_version"] != 1 or digest(row["manifest_json"].encode()) != row["manifest_sha256"]:
            raise PreservationError("representation_manifest_mismatch")
        manifest = json.loads(row["manifest_json"])
        if (manifest.get("representation_id") != row["id"] or manifest.get("version_id") != row["version_id"]
                or manifest.get("profile_fingerprint") != row["profile_fingerprint"] or manifest.get("generation") != row["generation"]
                or canonical(manifest.get("profile")).decode() != row["profile_json"]):
            raise PreservationError("representation_manifest_mismatch")
        if len(parts) != len(manifest["parts"]):
            raise PreservationError("representation_manifest_mismatch")
        cursor = byte_cursor = 0
        for part, expected in zip(parts, manifest["parts"]):
            if any(part[key] != value for key, value in expected.items()):
                raise PreservationError("representation_manifest_mismatch")
            if part["char_start"] != cursor or part["byte_start"] != byte_cursor:
                raise PreservationError("representation_coverage_gap")
            cursor, byte_cursor = part["char_end"], part["byte_end"]
        if cursor != manifest["text_chars"] or byte_cursor != manifest["text_bytes"]:
            raise PreservationError("representation_coverage_gap")
        return {**dict(row), "manifest": manifest, "parts": parts}

    def enable(self, representation_id: str) -> dict:
        plan = self.get(representation_id)
        if plan["state"] == "retired":
            raise PreservationError("representation_retired")
        if plan["manifest"]["profile"]["native"].get("execution_profile_known") is not True:
            raise PreservationError("representation_executor_profile_unknown")
        with self.store.exclusive(), self.store.connect(write=True) as db:
            active = db.execute("SELECT active FROM versions WHERE id=?", (plan["version_id"],)).fetchone()
            if active is None or not active[0]:
                raise PreservationError("version_not_active")
            # Only one unfinished generation of a kind runs; old published data
            # stays readable until a complete replacement is atomically promoted.
            others = db.execute("""SELECT 1 FROM native_representations WHERE version_id=? AND kind=?
                AND enabled=1 AND state!='complete' AND id!=?""", (plan["version_id"], plan["kind"], representation_id)).fetchone()
            if others:
                raise PreservationError("representation_already_running")
            db.execute("UPDATE native_representations SET enabled=1,state=CASE WHEN state='planned' THEN 'pending' ELSE state END WHERE id=?",
                       (representation_id,))
            if plan["kind"] == "native_index":
                db.execute("""UPDATE delivery SET state='pending',next_attempt=0,lease_until=0,lease_id=NULL,last_error=NULL
                    WHERE version_id=? AND state IN ('failed','partial','blocked')""", (plan["version_id"],))
        return self.get(representation_id)

    def disable(self, representation_id: str) -> dict:
        """Stop future parts, never pretend a running native operation stopped."""
        self.get(representation_id)
        with self.store.exclusive(), self.store.connect(write=True) as db:
            if db.execute("""SELECT 1 FROM native_representation_parts WHERE representation_id=?
                AND (state='submitted' OR (state='pending' AND attempts>0) OR lease_until>?)""",
                (representation_id, time.time())).fetchone():
                raise PreservationError("representation_inflight")
            db.execute("UPDATE native_representations SET enabled=0 WHERE id=?", (representation_id,))
        return self.get(representation_id)

    def for_version(self, version_id: str, *, kind: str = "native_index") -> dict | None:
        with self.store.connect() as db:
            row = db.execute("""SELECT id FROM native_representations WHERE version_id=? AND kind=? AND enabled=1
                ORDER BY CASE WHEN state!='complete' THEN 0 ELSE 1 END,created_at DESC LIMIT 1""", (version_id, kind)).fetchone()
        return self.get(row[0]) if row else None

    def claim(self, representation_id: str, *, lease_seconds: float = 90) -> dict | None:
        now = time.time()
        with self.store.connect(write=True) as db:
            eligible = db.execute("""SELECT 1 FROM native_representations r JOIN versions v ON v.id=r.version_id
                JOIN sources s ON s.id=v.source_id WHERE r.id=? AND r.enabled=1 AND v.active=1 AND s.forgotten_at IS NULL""",
                (representation_id,)).fetchone()
            if not eligible:
                return None
            if db.execute("""SELECT 1 FROM native_representation_parts WHERE representation_id=?
                AND state IN ('pending','submitted') AND lease_until>?""", (representation_id, now)).fetchone():
                return None
            row = db.execute("""SELECT * FROM native_representation_parts WHERE representation_id=?
                AND state IN ('pending','submitted') AND next_attempt<=? AND lease_until<=?
                AND ((state='submitted' OR attempts>0) OR NOT EXISTS
                    (SELECT 1 FROM native_representation_parts busy WHERE busy.representation_id=?
                        AND (busy.state='submitted' OR (busy.state='pending' AND busy.attempts>0))))
                ORDER BY CASE WHEN state='submitted' OR attempts>0 THEN 0 ELSE 1 END,part_index LIMIT 1""",
                (representation_id, now, now, representation_id)).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE native_representation_parts SET lease_id=?,lease_until=? WHERE representation_id=? AND part_index=?",
                       (token, now + lease_seconds, representation_id, row["part_index"]))
            return {**dict(row), "lease_id": token, "lease_until": now + lease_seconds}

    def read_part(self, part: dict) -> str:
        plan = self.get(part["representation_id"])
        expected = plan["manifest"]["parts"][part["part_index"]]
        if any(part[key] != value for key, value in expected.items()):
            raise PreservationError("representation_manifest_mismatch")
        path = self.store.versions / plan["version_id"] / "text.txt"
        try:
            with path.open("rb") as stream:
                stream.seek(part["byte_start"])
                raw = stream.read(part["byte_end"] - part["byte_start"])
        except OSError:
            raise PreservationError("representation_part_unavailable") from None
        if digest(raw) != part["text_sha256"]:
            raise PreservationError("representation_part_hash_mismatch")
        return raw.decode("utf-8")

    def reserve(self, part: dict) -> bool:
        with self.store.connect(write=True) as db:
            return db.execute("""UPDATE native_representation_parts SET attempts=attempts+1,updated_at=?
                WHERE representation_id=? AND part_index=? AND lease_id=? AND state IN ('pending','submitted')
                AND EXISTS (SELECT 1 FROM native_representations r JOIN versions v ON v.id=r.version_id
                    WHERE r.id=representation_id AND r.enabled=1 AND v.active=1)""",
                (timestamp(), part["representation_id"], part["part_index"], part["lease_id"])).rowcount == 1

    def finish(self, part: dict, state: str, *, error: str | None = None, delay: float = 0, proof: dict | None = None) -> bool:
        if state not in {"pending", "submitted", "complete", "partial", "failed", "blocked"}:
            raise PreservationError("invalid_representation_state")
        if state == "complete" and proof is None:
            raise PreservationError("representation_completion_proof_required")
        if error and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", error):
            raise PreservationError("unsafe_error_code")
        with self.store.connect(write=True) as db:
            changed = db.execute("""UPDATE native_representation_parts SET state=?,last_error=?,next_attempt=?,
                lease_id=NULL,lease_until=0,proof_json=?,updated_at=?
                WHERE representation_id=? AND part_index=? AND lease_id=? AND state IN ('pending','submitted')""",
                (state, error, time.time() + delay, canonical(proof).decode() if proof else None,
                 timestamp(), part["representation_id"], part["part_index"], part["lease_id"])).rowcount == 1
            if changed and state in {"complete", "partial", "failed", "blocked"}:
                db.execute("UPDATE native_representations SET last_served=? WHERE id=?", (time.time(), part["representation_id"]))
        return changed

    def request_retry(self, representation_id: str) -> dict:
        """Retry only failed native work; completed partial extraction needs a new generation.

        Previously completed parts are never reset. An unknown/lost submission
        already resumes automatically with its recorded UUID.
        """
        plan = self.get(representation_id)
        if any(p["state"] in {"partial", "blocked"} for p in plan["parts"]):
            raise PreservationError("partial_representation_requires_reconciliation")
        with self.store.exclusive(), self.store.connect(write=True) as db:
            active = db.execute("SELECT active FROM versions WHERE id=?", (plan["version_id"],)).fetchone()
            if active is None or not active[0]:
                raise PreservationError("version_not_active")
            db.execute("""UPDATE native_representation_parts SET state='pending',last_error='retry_requested',
                next_attempt=0,lease_until=0,lease_id=NULL WHERE representation_id=? AND state='failed'""", (representation_id,))
            if plan["kind"] == "native_index":
                db.execute("""UPDATE delivery SET state='pending',next_attempt=0,lease_until=0,lease_id=NULL,last_error=NULL
                    WHERE version_id=? AND state IN ('failed','partial','blocked')""", (plan["version_id"],))
        return self.get(representation_id)

    def complete(self, representation_id: str) -> dict:
        plan = self.get(representation_id)
        if plan["state"] == "retired":
            raise PreservationError("representation_retired")
        manifest = plan["manifest"]
        units = 0
        for part in plan["parts"]:
            if part["state"] != "complete":
                raise PreservationError("representation_incomplete")
            if part["requires_native"]:
                proof = self.proofs.verified(version_id=plan["version_id"], document_id=part["document_id"],
                    operation_id=part["operation_id"], text_sha256=part["text_sha256"], profile=plan["profile_fingerprint"])
                if proof is None:
                    raise PreservationError("representation_completion_proof_required")
                units += proof["readback"]["memory_unit_count"]
            elif self.read_part(part).strip():
                raise PreservationError("representation_whitespace_mismatch")
        with self.store.exclusive(), self.store.connect(write=True) as db:
            active = db.execute("SELECT active FROM versions WHERE id=?", (plan["version_id"],)).fetchone()
            if active is None or not active[0]:
                raise PreservationError("version_not_active")
            current = db.execute("SELECT state FROM native_representations WHERE id=?", (representation_id,)).fetchone()
            # A delayed duplicate completion of an older generation cannot
            # roll back a newer published generation.
            if current[0] != "complete":
                db.execute("UPDATE native_representations SET published=0 WHERE version_id=? AND kind=?",
                           (plan["version_id"], plan["kind"]))
                db.execute("UPDATE native_representations SET state='complete',published=1 WHERE id=?", (representation_id,))
        return {"representation_id": representation_id, "version_id": plan["version_id"], "state": "complete",
                "profile_fingerprint": plan["profile_fingerprint"], "parts_complete": len(plan["parts"]),
                "parts_total": len(plan["parts"]), "text_sha256": manifest["source_text_sha256"], "memory_unit_count": units}
