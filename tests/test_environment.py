import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('portable_environment', ROOT / 'environment.py')
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.temp = tempfile.TemporaryDirectory(prefix='olympus portable ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'Olympus-Lite'
        self.root.mkdir()
        (self.root / 'TEMPLATE.json').write_text(json.dumps({'edition':'Olympus-Lite','distribution_version':'0.3.0'}))
        self.home = Path(self.temp.name) / 'new-home'
        self.home.mkdir()

    def test_plan_is_portable_and_lite_needs_no_docker(self):
        plan = self.m.install_plan(self.root, system='Darwin', machine='arm64')
        self.assertNotIn('docker-desktop', str(plan))
        self.assertIn('uv', str(plan))
        self.assertNotIn('/Users/', json.dumps(plan))
        self.assertEqual(plan['edition'], 'Olympus-Lite')

    def test_wrong_platform_fails_before_any_install(self):
        with self.assertRaisesRegex(self.m.EnvironmentError, 'macos_apple_silicon_required'):
            self.m.install_plan(self.root, system='Linux', machine='x86_64')

    def test_max_plan_adds_docker(self):
        (self.root / 'TEMPLATE.json').write_text(json.dumps({'edition':'Olympus-Max'}))
        self.assertIn('docker-desktop', str(self.m.install_plan(self.root, system='Darwin', machine='arm64')))

    def test_configuration_is_repeatable_and_preserves_other_files(self):
        first = self.m.configure(self.root, home=self.home, python='/usr/bin/python3')
        second = self.m.configure(self.root, home=self.home, python='/usr/bin/python3')
        self.assertEqual(first, second)
        self.assertTrue(Path(first['state']).is_relative_to(self.home.resolve()))
        self.assertIn(str(self.root), (self.root / '.codex/config.toml').read_text())
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.root / 'starter.local.json').exists())

    def test_conflicting_config_is_not_overwritten(self):
        (self.root / '.codex').mkdir()
        path = self.root / '.codex/config.toml'; path.write_text('user config')
        with self.assertRaisesRegex(self.m.EnvironmentError, 'existing_configuration_differs'):
            self.m.configure(self.root, home=self.home, python='/usr/bin/python3')
        self.assertEqual(path.read_text(), 'user config')

    def test_compose_has_private_port_separate_volumes_and_no_keys(self):
        cfg={'id':'olympus-max-123456789abc','state':str(self.home/'state'),'port':19888,'uid':501,'gid':20}
        c=self.m.compose_config(cfg, self.root)
        self.assertEqual(c['services']['hindsight']['ports'], ['127.0.0.1:19888:8888'])
        self.assertNotIn('OLYMPUS_HINDSIGHT_API_KEY', json.dumps(c))
        self.assertNotIn('/Users/', json.dumps(c))
        self.assertNotIn('ports', c['services']['db'])
        self.assertIn('@sha256:', c['services']['hindsight']['image'])
        self.assertEqual(c['name'], cfg['id'])
        self.assertEqual(c['services']['worker']['environment']['HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS'],'0')
        self.assertEqual(c['services']['worker']['environment']['HINDSIGHT_API_LLM_PROVIDER'],'none')

    def test_config_refuses_state_inside_code(self):
        with self.assertRaisesRegex(self.m.EnvironmentError,'state_must_be_outside'):
            self.m.configure(self.root, home=self.root, python='/usr/bin/python3')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        import hashlib
        self.temp=tempfile.TemporaryDirectory(prefix='olympus archive ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'Olympus-Lite';self.root.mkdir()
        (self.root/'scripts').mkdir();(self.root/'.agents/skills/demo').mkdir(parents=True)
        (self.root/'.claude').mkdir();(self.root/'.claude/skills').symlink_to('../.agents/skills')
        (self.root/'code.py').write_text('print("test")\n')
        (self.root/'.agents/skills/demo/SKILL.md').write_text('synthetic')
        # Load the actual verifier and builder. The copied test also works in the release.
        verifier=ROOT.parents[1]/'scripts/verify-starter-distribution.py' if (ROOT/'common').exists() else ROOT/'scripts/verify-distribution.py'
        import shutil
        shutil.copyfile(verifier,self.root/'scripts/verify-distribution.py')
        builder=ROOT/'common/package-release.py' if (ROOT/'common').exists() else ROOT/'scripts/package-release.py'
        spec=importlib.util.spec_from_file_location('release_builder',builder);self.builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.builder)
        files={n:hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in ('code.py','.agents/skills/demo/SKILL.md','scripts/verify-distribution.py')}
        (self.root/'DISTRIBUTION.json').write_text(json.dumps({'schema':1,'files':files}))
        self.output=Path(self.temp.name)/'release.tar.gz'

    def test_archive_omits_untracked_secrets_and_preserves_skill_link(self):
        import tarfile
        (self.root/'.env').write_text('synthetic private data')
        (self.root/'starter.local.json').write_text('synthetic state path')
        receipt=self.builder.pack(self.root,self.output)
        with tarfile.open(self.output) as archive:
            names=archive.getnames()
            self.assertFalse(any('.env' in n or 'starter.local' in n for n in names))
            self.assertEqual(archive.getmember('Olympus-Lite/.claude/skills').linkname,'../.agents/skills')
        self.assertGreater(receipt['bytes'],0)

    def test_changed_file_fails_without_publishing_archive(self):
        (self.root/'code.py').write_text('changed')
        with self.assertRaisesRegex(ValueError,'distribution_not_verified'):self.builder.pack(self.root,self.output)
        self.assertFalse(self.output.exists())

    def test_private_file_in_manifest_is_rejected(self):
        import hashlib
        (self.root/'.env').write_text('synthetic')
        path=self.root/'DISTRIBUTION.json';d=json.loads(path.read_text());d['files']['.env']=hashlib.sha256(b'synthetic').hexdigest();path.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'private_file_in_distribution'):self.builder.pack(self.root,self.output)
        self.assertFalse(self.output.exists())

if __name__=='__main__': unittest.main()
