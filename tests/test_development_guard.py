"""Real commands, changed source bytes, and checkpoint conflicts at the adapter boundary."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from olympus import projects as p, workspaces as w
from olympus.preservation import Store, PreservationError
from test_workspaces import project, repo

SCRIPT = Path(__file__).resolve().parents[1] / '.agents/skills/olympus-development/scripts/checkpoint_guard.py'
spec = importlib.util.spec_from_file_location('development_guard', SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class DevelopmentGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='olympus-development-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = repo(self.root / 'app')
        self.store = Store(self.root / 'state')
        p.register(self.store, project(self.app))
        self.work = w.start(self.store, 'task', 'example', goal='Change behavior',
                            selection={'app': ['app.py']}, intent='edit')
        self.update = {'done': ['Behavior checked'], 'pending': [], 'next_step': 'Complete',
                       'artifacts': [str(self.app / 'app.py')]}

    def check(self, code='assert 1 + 1 == 2', name='check.json'):
        return guard.check(self.store, 'task', cwd=self.app,
                           argv=[sys.executable, '-B', '-c', code], output=self.root / name)

    def assertFailure(self, error, receipt, update=None):
        with self.assertRaisesRegex(PreservationError, '^' + error + '$'):
            guard.finish(self.store, 'task', update=update or self.update, receipts=[receipt])
        self.assertEqual(p.read_record(self.store, 'workspace', 'task')['record']['status'], 'in-progress')

    def test_real_check_and_finish_from_new_process(self):
        (self.app / 'owned.txt').write_text('Owner unfinished work\n')
        owner = (self.app / 'owned.txt').read_bytes()
        self.check('from app import value; assert value == 1')
        update = self.root / 'update.json'; update.write_text(json.dumps(self.update))
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.store.root),
                                 'finish', 'task', '--update', str(update),
                                 '--receipt', str(self.root / 'check.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['record']['status'], 'complete')
        self.assertEqual((self.app / 'owned.txt').read_bytes(), owner)

    def test_same_git_status_but_different_source_rejects_old_success(self):
        (self.app / 'app.py').write_text('value = 2\n')
        receipt = self.check('from app import value; assert value == 2')
        (self.app / 'app.py').write_text('value = 3\n')
        self.assertFailure('development_checks_stale', receipt)

    def test_changed_instruction_rejects_old_success(self):
        receipt = self.check()
        (self.app / 'AGENTS.md').write_text('Changed project contract\n')
        self.assertFailure('development_checks_stale', receipt)

    def test_concurrent_checkpoint_rejects_old_receipt(self):
        receipt = self.check()
        w.checkpoint(self.store, 'task', {'next_step': 'Another writer'}, expected=self.work['version_id'])
        self.assertFailure('workspace_version_conflict', receipt)

    def test_failed_command_does_not_finish(self):
        receipt = self.check('raise SystemExit(7)')
        self.assertEqual(receipt['check']['exit_code'], 7)
        self.assertFailure('development_check_failed', receipt)

    def test_command_that_changes_source_cannot_certify_itself(self):
        receipt = self.check("from pathlib import Path; Path('app.py').write_text('value = 5\\n')")
        self.assertFalse(receipt['stable'])
        self.assertFailure('development_checks_stale', receipt)

    def test_wrong_cwd_and_receipt_inside_resource_never_run_command(self):
        for cwd, output, error in [(self.root, self.root / 'check.json', 'development_check_cwd_outside_code'),
                                   (self.app, self.app / 'proof.json', 'development_receipt_inside_resource')]:
            with self.subTest(error=error), self.assertRaisesRegex(PreservationError, error):
                guard.check(self.store, 'task', cwd=cwd, argv=[sys.executable, '-c', 'raise SystemExit(99)'], output=output)
        self.assertFalse((self.app / 'proof.json').exists())

    def test_receipt_is_create_only(self):
        self.check()
        original = (self.root / 'check.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.check("from pathlib import Path; Path('app.py').write_text('unwanted')")
        self.assertEqual((self.root / 'check.json').read_bytes(), original)
        self.assertEqual((self.app / 'app.py').read_text(), 'value = 1\n')

    def test_failed_post_command_snapshot_keeps_a_readable_failure_receipt(self):
        receipt = self.check("from pathlib import Path; Path('app.py').write_bytes(b'x' * (8 * 1024 * 1024 + 1))")
        persisted = json.loads((self.root / 'check.json').read_text())
        self.assertEqual(persisted, receipt)
        self.assertEqual(receipt['check']['exit_code'], 0)
        self.assertFalse(receipt['stable'])
        self.assertEqual(receipt['error'], 'development_context_unavailable_after_check')

    def test_timeout_and_launch_error_cannot_be_successful(self):
        cases = [([sys.executable, '-c', 'import time; time.sleep(3)'], 'development_check_timeout', 'timeout.json'),
                 ([str(self.root / 'missing-command')], 'development_check_launch_failed', 'launch.json')]
        for argv, error, name in cases:
            with self.subTest(error=error):
                receipt = guard.check(self.store, 'task', cwd=self.app, argv=argv,
                                      output=self.root / name, timeout=1)
                self.assertIsNone(receipt['check']['exit_code'])
                self.assertEqual(receipt['error'], error)
                self.assertFailure('development_check_unstable', receipt)

    def test_incomplete_work_keeps_existing_finish_gate(self):
        receipt = self.check()
        self.assertFailure('workspace_completion_evidence_required', receipt,
                           {**self.update, 'pending': ['Still unfinished']})

    def test_finish_cannot_change_the_scope_after_verification(self):
        receipt = self.check()
        self.assertFailure('development_scope_update_requires_checkpoint', receipt,
                           {**self.update, 'selection': {'app': ['owned.txt']}})

    def test_check_cli_parses_command_arguments(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.store.root), 'check', 'task',
                                 '--cwd', str(self.app), '--output', str(self.root / 'cli.json'),
                                 '--', sys.executable, '-B', '-c', 'assert True'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['check']['exit_code'], 0)


if __name__ == '__main__':
    unittest.main()
