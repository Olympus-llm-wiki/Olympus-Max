"""Real age encryption with throwaway keys; no owner identity or PostgreSQL calls."""

from contextlib import contextmanager
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from olympus.backup import (CheckpointError, COPY_SIGNATURE, NATIVE_TABLES, create_checkpoint,
                            inspect_checkpoint, restore_local_checkpoint, validate_native_backup)
from olympus.preservation import PreservationError, Store, canonical


@contextmanager
def identity_pipe(identity):
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, identity)
    finally:
        os.close(write_fd)
    try:
        yield read_fd
    finally:
        os.close(read_fd)


def native_fixture(path, *, mutate=None, bad_stream=None, extra=None):
    stream = COPY_SIGNATURE + struct.pack("!IIh", 0, 0, -1)
    payloads = {table + ".bin": stream for table in NATIVE_TABLES}
    manifest = {"version": "2", "created_at": "2026-09-05T12:00:00+00:00", "schema": "public",
                "tables": {table: {"rows": 0, "size_bytes": len(stream),
                                    "columns": [{"name": "id", "type_name": "text"}]}
                           for table in NATIVE_TABLES}}
    if bad_stream:
        payloads[bad_stream + ".bin"] = b"not PostgreSQL"
        manifest["tables"][bad_stream]["size_bytes"] = len(payloads[bad_stream + ".bin"])
    if mutate:
        mutate(manifest, payloads)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in payloads.items():
            archive.writestr(name, data)
        if extra:
            archive.writestr(*extra)
    return path


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age/age-keygen required")
class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Generated in memory, never written to disk or printed in a test result.
        cls.identity = subprocess.run(["age-keygen"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      check=True).stdout
        cls.recipient = subprocess.run(["age-keygen", "-y"], input=cls.identity, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, check=True).stdout.decode().strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="olympus-backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "store")
        self.native = native_fixture(self.root / "native.zip")
        self.runtime = {"writers_stopped": True, "workers_stopped": True,
                        "hindsight_version": "0.9.2", "bank_id": "olympus-v1"}

    def capture(self, key="source", text="Exact source text."):
        return self.store.capture(source_key=key, scope="pilot", title="Example", original=text.encode(), text=text)

    def checkpoint(self):
        return create_checkpoint(self.store, self.native, self.recipient, self.root / "backups",
                                 runtime_receipt=self.runtime)

    def inspect(self, path):
        with identity_pipe(self.identity) as fd:
            return inspect_checkpoint(path, identity_fd=fd)

    def decrypt_bytes(self, path):
        with identity_pipe(self.identity) as fd:
            return subprocess.run(["age", "-d", "-i", f"/dev/fd/{fd}", str(path)], pass_fds=(fd,),
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout

    def encrypt_bytes(self, data, name="malicious.age"):
        path = self.root / name
        with path.open("wb") as output:
            subprocess.run(["age", "-r", self.recipient], input=data, stdout=output,
                           stderr=subprocess.DEVNULL, check=True)
        return path

    def rewrite(self, receipt, transform):
        with tarfile.open(fileobj=io.BytesIO(self.decrypt_bytes(receipt["path"])), mode="r:") as archive:
            members = [(copy.copy(item), archive.extractfile(item).read()) for item in archive]
        members = transform(members)
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for item, body in members:
                item.size = len(body) if item.isreg() else 0
                archive.addfile(item, io.BytesIO(body) if item.isreg() else None)
        return self.encrypt_bytes(data.getvalue())

    def error(self, code, function):
        with self.assertRaises(CheckpointError) as raised:
            function()
        self.assertEqual(raised.exception.code, code)

    def test_flattened_secret_comments_restore(self):
        version = self.capture()
        receipt = self.checkpoint()
        flattened = b" ".join(self.identity.splitlines())
        with identity_pipe(flattened) as fd:
            inspect_checkpoint(receipt["path"], identity_fd=fd)
        with identity_pipe(flattened) as fd:
            restored = restore_local_checkpoint(receipt["path"], self.root / "restored", identity_fd=fd)
        self.assertEqual(self.store.read_version(version.version_id),
                         Store(restored["store_root"]).read_version(version.version_id))

    def test_ambiguous_and_oversized_identities_rejected(self):
        receipt = self.checkpoint()
        for identity in (self.identity + self.identity, b"x" * 8193):
            with identity_pipe(identity) as fd:
                self.error("invalid_identity_format",
                                 lambda: inspect_checkpoint(receipt["path"], identity_fd=fd))

    def test_exact_queue_sources_events_and_tombstones_roundtrip_with_recovery_barrier(self):
        old = self.capture("versioned", "Old claim")
        current = self.capture("versioned", "Correct claim")
        forgotten = self.capture("forgotten", "A detail to forget")
        self.store.supersede(old.version_id, current.version_id, "new evidence")
        self.store.forget(forgotten.source_id, "owner request")
        # Hold WAL open: a plain registry.sqlite3 file copy would miss this event.
        writer = sqlite3.connect(self.store.db_path)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO registrations(thread_id,transcript_path,started_at,codex_version,scope) VALUES(?,?,?,?,?)",
                       ("thread-current", "/not-opened/transcript.jsonl", "2026-09-05", "0.153.1", "pilot"))
        writer.execute("INSERT INTO captured_events(thread_id,event_id,observed_at,message_json) VALUES(?,?,?,?)",
                       ("thread-current", "event-1", "2026-09-05", '{"role":"user","text":"keep this"}'))
        writer.execute("UPDATE delivery SET state='submitted',attempts=2,lease_id='prior-lease',lease_until=42 WHERE version_id=?",
                       (current.version_id,))
        writer.commit()
        with self.store.connect() as db:
            expected_delivery = [tuple(r) for r in db.execute("SELECT * FROM delivery ORDER BY version_id")]
            expected_changes = [tuple(r) for r in db.execute("SELECT * FROM changes ORDER BY id")]
            expected_events = [tuple(r) for r in db.execute("SELECT * FROM captured_events")]
        receipt = self.checkpoint()
        self.assertEqual(receipt["remote_state"], "unconfirmed")
        self.assertFalse(receipt["restore_verified"])
        self.assertEqual(self.inspect(receipt["path"])["manifest"]["local"]["tables"]["captured_events"], 1)
        with identity_pipe(self.identity) as fd:
            restored = restore_local_checkpoint(receipt["path"], self.root / "restored", identity_fd=fd)
        recovered = Store(restored["store_root"])
        with recovered.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM delivery ORDER BY version_id")], expected_delivery)
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM changes ORDER BY id")], expected_changes)
            self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM captured_events")], expected_events)
        for version in (old, current, forgotten):
            self.assertEqual(self.store.read_version(version.version_id), recovered.read_version(version.version_id))
        self.assertEqual(recovered.setting("recovery_state"), "blocked")
        with self.assertRaises(PreservationError):
            recovered.active_documents("pilot")
        self.assertIsNone(recovered.claim())
        with self.assertRaises(PreservationError):
            recovered.capture(source_key="forgotten", scope="pilot", title="Example", original=b"new", text="new")
        self.assertTrue(restored["native_restore_required"])
        self.assertTrue(restored["latest_revocations_required"])
        self.assertEqual(restored["remote_state"], "unconfirmed")

    def test_only_registry_referenced_versions_enter_archive_not_auth_cache_or_orphans(self):
        item = self.capture()
        (self.store.root / "auth.json").write_text("FAKE-CREDENTIAL-FILE")
        (self.store.root / ".env").write_text("FAKE-ENVIRONMENT-FILE")
        (self.store.root / "private.key").write_text("FAKE-KEY-FILE")
        (self.store.versions / ("olv-" + "a" * 64)).mkdir()
        receipt = self.checkpoint()
        names = set(receipt["manifest"]["files"])
        self.assertEqual(names, {"local/registry.sqlite3", "native/hindsight.zip", *[
            f"local/versions/{item.version_id}/{name}" for name in ("original", "text.txt", "manifest.json", "manifest.sha256")]})
        ciphertext = Path(receipt["path"]).read_bytes()
        self.assertNotIn(b"Exact source text.", ciphertext)
        self.assertEqual(Path(receipt["path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(hashlib.sha256(ciphertext).hexdigest(), receipt["sha256"])

    def test_native_inventory_and_binary_copy_shape_are_verified(self):
        valid = validate_native_backup(self.native)
        self.assertEqual(valid["table_count"], 21)
        self.assertFalse(valid["restore_verified"])
        native_fixture(self.native, bad_stream="documents")
        self.error("invalid_native_copy_stream", self.checkpoint)
        native_fixture(self.native, mutate=lambda m, p: p.pop("documents.bin"))
        self.error("native_backup_inventory_mismatch", self.checkpoint)
        native_fixture(self.native, mutate=lambda m, p: m.update(version="3"))
        self.error("unsupported_native_backup_schema", self.checkpoint)
        native_fixture(self.native, mutate=lambda m, p: m["tables"]["banks"].update(rows=1))
        self.error("native_copy_shape_mismatch", self.checkpoint)

    def test_native_zip_traversal_symlink_and_arbitrary_zip_are_rejected(self):
        native_fixture(self.native, extra=("../escape", b"bad"))
        self.error("native_backup_inventory_mismatch", self.checkpoint)
        with zipfile.ZipFile(self.native, "w") as archive:
            archive.writestr("notes.txt", "not a database backup")
        self.error("native_backup_inventory_mismatch", self.checkpoint)
        native_fixture(self.native)
        with zipfile.ZipFile(self.native) as archive:
            members = {i.filename: archive.read(i.filename) for i in archive.infolist()}
        with zipfile.ZipFile(self.native, "w") as archive:
            for name, body in members.items():
                item = zipfile.ZipInfo(name)
                item.external_attr = (0o120777 if name == "banks.bin" else 0o100600) << 16
                archive.writestr(item, body)
        self.error("invalid_native_backup_member", self.checkpoint)

    def test_ciphertext_tampering_and_wrong_identity_never_publish_restore(self):
        self.capture()
        receipt = self.checkpoint()
        path = Path(receipt["path"])
        data = bytearray(path.read_bytes())
        data[-12] ^= 1
        corrupt = self.root / "corrupt.age"
        corrupt.write_bytes(data)
        self.error("checkpoint_decryption_failed", lambda: self.inspect(corrupt))
        other = subprocess.run(["age-keygen"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        with identity_pipe(other) as fd:
            self.error("checkpoint_decryption_failed", lambda: restore_local_checkpoint(path, self.root / "failed", identity_fd=fd))
        self.assertFalse((self.root / "failed").exists())

    def test_tar_path_traversal_and_links_never_escape_target(self):
        receipt = self.checkpoint()
        for kind in ("traversal", "symlink", "hardlink"):
            with self.subTest(kind=kind):
                def transform(members):
                    item = tarfile.TarInfo("../escaped" if kind == "traversal" else "local/registry.sqlite3")
                    if kind != "traversal":
                        item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                        item.linkname = "../../escaped"
                    return [(item, b"attack")] + members
                bad = self.rewrite(receipt, transform)
                with identity_pipe(self.identity) as fd:
                    self.error("unsafe_checkpoint_member", lambda: restore_local_checkpoint(bad, self.root / "bad-target", identity_fd=fd))
                self.assertFalse((self.root / "escaped").exists())
                self.assertFalse((self.root / "bad-target").exists())

    def test_unknown_checkpoint_schema_and_hash_mismatch_are_rejected(self):
        self.capture()
        receipt = self.checkpoint()
        def schema(members):
            result = []
            for item, body in members:
                if item.name == "checkpoint.json":
                    m = json.loads(body); m["schema"] = 99; body = canonical(m)
                result.append((item, body))
            return result
        self.error("unsupported_checkpoint_schema", lambda: self.inspect(self.rewrite(receipt, schema)))
        def content(members):
            return [(item, body + b"edited" if item.name.endswith("/text.txt") else body) for item, body in members]
        self.error("invalid_checkpoint_manifest", lambda: self.inspect(self.rewrite(receipt, content)))
        def same_size(members):
            return [(item, b"X" + body[1:] if item.name.endswith("/text.txt") else body) for item, body in members]
        self.error("checkpoint_member_hash_mismatch", lambda: self.inspect(self.rewrite(receipt, same_size)))

    def test_corrupt_sqlite_with_updated_outer_hash_still_fails_integrity(self):
        receipt = self.checkpoint()
        def broken_database(members):
            mutated = b"not SQLite"
            result = []
            for item, body in members:
                if item.name == "local/registry.sqlite3":
                    body = mutated
                elif item.name == "checkpoint.json":
                    manifest = json.loads(body)
                    manifest["files"]["local/registry.sqlite3"] = {"size": len(mutated), "sha256": hashlib.sha256(mutated).hexdigest()}
                    body = canonical(manifest)
                result.append((item, body))
            return result
        self.error("local_database_integrity_failed", lambda: self.inspect(self.rewrite(receipt, broken_database)))

    def test_source_anchor_drift_and_source_symlink_are_rejected(self):
        receipt = self.capture()
        folder = self.store.versions / receipt.version_id
        anchor = folder / "manifest.sha256"
        original = anchor.read_text()
        anchor.write_text("0" * 64)
        self.error("source_manifest_anchor_mismatch", self.checkpoint)
        anchor.write_text(original)
        text = folder / "text.txt"
        saved = text.read_bytes(); text.unlink()
        alternate = self.root / "outside.txt"; alternate.write_bytes(saved); text.symlink_to(alternate)
        self.error("source_not_regular", self.checkpoint)

    def test_unknown_local_schema_and_secret_metadata_fail_closed(self):
        with self.store.connect(write=True) as db:
            db.execute("PRAGMA user_version=99")
        self.error("unsupported_local_schema", self.checkpoint)
        with self.store.connect(write=True) as db:
            db.execute("PRAGMA user_version=1")
        self.runtime["api_key"] = "FAKE-NONREAL-KEY"
        self.error("runtime_receipt_contains_credentials", self.checkpoint)
        self.runtime.pop("api_key")
        self.runtime["workers_stopped"] = False
        self.error("runtime_freeze_receipt_required", self.checkpoint)

    def test_restore_refuses_nonempty_target_and_symlink_without_touching_existing_data(self):
        receipt = self.checkpoint()
        target = self.root / "existing"; target.mkdir(); marker = target / "owner.txt"; marker.write_text("preserve")
        with identity_pipe(self.identity) as fd:
            self.error("restore_target_not_empty", lambda: restore_local_checkpoint(receipt["path"], target, identity_fd=fd))
        linked = self.root / "linked"; linked.symlink_to(target, target_is_directory=True)
        with identity_pipe(self.identity) as fd:
            self.error("restore_target_not_empty", lambda: restore_local_checkpoint(receipt["path"], linked, identity_fd=fd))
        self.assertEqual(marker.read_text(), "preserve")

    def test_invalid_public_recipient_and_descriptor_are_rejected(self):
        self.error("invalid_age_recipient", lambda: create_checkpoint(self.store, self.native, "ssh-ed25519 not-supported",
                    self.root / "output", runtime_receipt=self.runtime))
        self.error("invalid_age_recipient", lambda: create_checkpoint(self.store, self.native, "age1" + "q" * 58,
                    self.root / "output", runtime_receipt=self.runtime))
        self.error("invalid_identity_descriptor", lambda: inspect_checkpoint(self.native, identity_fd=-1))


if __name__ == "__main__":
    unittest.main()
