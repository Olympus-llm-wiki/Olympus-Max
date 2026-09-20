import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from olympus.jobs import PipelineJobs
from olympus.preservation import Store


class JobsTests(unittest.TestCase):
    def test_liveness_renewal_does_not_invent_progress(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = PipelineJobs(Store(root))
            with patch('olympus.jobs.time.time', return_value=100):
                lease = jobs.begin('backup.snapshot', lease_seconds=10)
                jobs.progress(lease, {'bytes': 12})
            with patch('olympus.jobs.time.time', return_value=105):
                self.assertTrue(jobs.renew(lease))
            row = jobs.snapshot()[0]
            self.assertEqual(row['last_progress'], 100)
            self.assertEqual(row['detail'], {'bytes': 12})
            self.assertEqual(row['lease_until'], 115)

    def test_dead_worker_reclaim_rejects_stale_completion(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = PipelineJobs(Store(root))
            with patch('olympus.jobs.time.time', return_value=100):
                first = jobs.begin('library.export', lease_seconds=2)
                self.assertIsNone(jobs.begin('library.export'))
                jobs.progress(first, {'exported': 4})
            with patch('olympus.jobs.time.time', return_value=103):
                second = jobs.begin('library.export')
                self.assertFalse(jobs.succeed(first, {'exported': 8}))
                self.assertTrue(jobs.fail(second, 'file_unavailable', 'stage', detail={'errno': 60}))
            row = jobs.snapshot()[0]
            self.assertEqual(row['attempts'], 2)
            self.assertEqual(row['last_progress'], 100)
            self.assertEqual(row['error_code'], 'file_unavailable')
            self.assertIsNone(row['last_success'])

    def test_failed_stage_does_not_block_control_and_schema_is_additive(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            receipt = store.capture(source_key='one', scope='test', title='one', original=b'one', text='one')
            before = store.receipt(receipt.version_id)
            jobs = PipelineJobs(store)
            jobs.fail(jobs.begin('library.export'), 'io_failure', 'stage', retry_after=10)
            self.assertIsNone(jobs.begin('library.export'))
            self.assertTrue(jobs.succeed(jobs.begin('library.control'), {'published': True}))
            # Old version-1 reader can reopen unchanged source/delivery records.
            self.assertEqual(Store(root).receipt(receipt.version_id), before)
            with sqlite3.connect(store.db_path) as db:
                self.assertEqual(db.execute('pragma user_version').fetchone()[0], 1)
