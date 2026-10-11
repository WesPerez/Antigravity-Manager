"""Synthetic CI identities and interrupted single-container deployments."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
NAMES = ('common', 'ci_evidence', 'sources', 'adapters', 'updater', 'watch')
previous = {name: sys.modules.pop(name, None) for name in NAMES}
import common
import sources
import adapters
sys.path.pop(0)
for name, value in previous.items():
    sys.modules.pop(name, None)
    if value is not None:
        sys.modules[name] = value

A, B = 'a' * 40, 'b' * 40
OLD, NEW = 'a' * 64, 'b' * 64


class CandidateTests(unittest.TestCase):
    def source(self, rows):
        source = sources.Source(common.PROJECT, '/unused', Mock())
        source.sync = Mock(return_value=B)
        source.github.api.return_value = {'workflow_runs': rows}
        return source

    def row(self, sha=B, branch='MINE', event='push', status='completed', id=2):
        return {'id': id, 'head_sha': sha, 'head_branch': branch, 'event': event,
                'status': status, 'path': common.PROJECT['workflow']}

    def test_rebuilt_mine_history_accepts_only_exact_remote_tip(self):
        source = self.source([self.row(A, id=99), self.row()])
        self.assertEqual(source.candidate(A)['head_sha'], B)
        self.assertIn('branch=MINE&head_sha=' + B, source.github.api.call_args.args[0])

    def test_main_pull_requests_and_other_shas_are_not_candidates(self):
        for row in (self.row(branch='main'), self.row(event='pull_request'), self.row(A)):
            with self.subTest(row=row), self.assertRaises(common.Deferred):
                self.source([row]).candidate(A)

    def test_running_tip_ci_blocks_an_old_success(self):
        with self.assertRaisesRegex(common.Deferred, 'still running'):
            self.source([self.row(status='in_progress'), self.row(A)]).candidate(A)

    def test_current_tip_needs_no_new_ci_lookup(self):
        source = self.source([])
        self.assertIsNone(source.candidate(B))
        source.github.api.assert_not_called()

    def test_ci_verification_binds_mine_ref_and_required_job(self):
        source = self.source([])
        with patch.object(sources.evidence, 'verified_run', return_value={}) as verify:
            source.verified(self.row())
        self.assertEqual(verify.call_args.args[4:], ('refs/heads/MINE', B, ['build-and-push']))


class MetadataTests(unittest.TestCase):
    def test_wrong_revision_repository_image_or_digest_cannot_pull(self):
        app = {'repository': common.PROJECT['repository'], 'image': 'ghcr.io/wesperez/antigravity-manager'}
        correct = {**app, 'revision': B, 'digest': 'sha256:' + NEW}
        for key, value in (('revision', A), ('repository', 'other/project'), ('image', 'other/image'), ('digest', 'latest')):
            with self.subTest(key=key), patch.object(adapters.evidence, 'locate_artifact', return_value={
                    'download_path': 'repos/example', 'digest': 'sha256:' + NEW}), \
                    patch.object(adapters.evidence, 'checked_download', return_value=b'zip'), \
                    patch.object(adapters.evidence, 'unpack', return_value={**correct, key: value}), \
                    patch.object(adapters, 'run') as run:
                with self.assertRaises(common.Rejected):
                    adapters.prepare(app, Mock(), {'source': {'sha': B}}, '/unused')
                run.assert_not_called()


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.app = json.loads((HERE / 'config.example.json').read_text())['application']
        self.image = {'Config': {'Env': ['PORT=8045'], 'User': None}}
        self.obj = {'Id': OLD, 'Image': 'sha256:' + OLD,
                    'Mounts': [{'Type': 'bind', 'Source': self.app['root'],
                                'Destination': self.app['data_target'], 'RW': True}],
                    'Config': {'Env': ['PORT=8045', 'WEB_PASSWORD=synthetic'], 'User': ''},
                    'HostConfig': {'PortBindings': {'8045/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8045'}]},
                                   'RestartPolicy': {'Name': 'unless-stopped'},
                                   'LogConfig': {'Type': 'json-file', 'Config': self.app['logging']['options']},
                                   'ShmSize': 64 * 1024**2, 'IpcMode': 'private', 'CgroupnsMode': 'private',
                                   'Mounts': [{'Type': 'bind', 'Source': self.app['root'], 'Target': self.app['data_target']}]},
                    'NetworkSettings': {'Networks': {'bridge': {'Aliases': []}}}}

    def test_docker_empty_defaults_and_long_bind_syntax_are_accepted(self):
        with patch.object(adapters, 'inspect', return_value=self.image), \
             patch.object(adapters, 'environment', return_value={'WEB_PASSWORD': 'synthetic'}):
            adapters.contract(self.app, self.obj)

    def test_wrong_data_mount_cannot_pass_contract(self):
        self.obj['Mounts'][0]['Source'] = '/other/data'
        with self.assertRaises(common.Rejected):
            adapters.contract(self.app, self.obj)

    def test_environment_drift_is_rejected(self):
        with patch.object(adapters, 'inspect', return_value=self.image), \
             patch.object(adapters, 'environment', return_value={'WEB_PASSWORD': 'changed'}):
            with self.assertRaises(common.Rejected):
                adapters.contract(self.app, self.obj)


class ProbeTests(unittest.TestCase):
    def test_fresh_empty_instance_can_open_admin_ui_but_production_floor_stays_enforced(self):
        app = json.loads((HERE / 'config.example.json').read_text())['application']
        page = '<html><script src="./assets/app.js"></script></html>'
        for floor, count, running, accepted in ((0, 0, False, True), (1, 0, False, False), (0, 1, False, False)):
            with self.subTest(floor=floor, count=count):
                responses = [(200, 'application/json', '{"status":"ok"}'),
                             (200, 'application/json', json.dumps({'running': running, 'port': 8045, 'active_accounts': count})),
                             (200, 'application/json', '{"accounts":[]}'), (401, '', ''),
                             (200, 'text/html', page), (200, 'text/javascript', 'x' * 2048),
                             (200, 'text/html', page), (200, 'application/json', '{"accounts":[]}'), (401, '', '')]
                with patch.object(adapters, 'inspect', return_value={'Id': NEW, 'State': {'Running': True, 'Health': {'Status': 'healthy'}}}), \
                     patch.object(adapters, 'environment', return_value={'WEB_PASSWORD': 'synthetic'}), \
                     patch.object(adapters, 'run', return_value=page), \
                     patch.object(adapters, 'request', side_effect=responses):
                    if accepted:
                        self.assertEqual(adapters.probe_once({**app, 'min_accounts': floor}), 0)
                    else:
                        with self.assertRaises(common.Deferred):
                            adapters.probe_once({**app, 'min_accounts': floor})


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.app = {'container': 'test-app', 'locks': [str(self.directory / 'deploy.lock')],
                    'restart': 'unless-stopped', 'networks': {'bridge': [], 'private': ['test-app']}}
        self.old = {'Id': OLD, 'Name': '/test-app', 'Image': 'sha256:' + OLD,
                    'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}},
                    'NetworkSettings': {'Networks': {'bridge': {'Aliases': []}, 'private': {'Aliases': ['test-app']}}}}
        self.prepared = {'path': 'image@sha256:' + NEW, 'image_id': 'sha256:' + NEW}
        self.verified = {'source': {'sha': B}, 'builder': {}}
        self.record = {'state': 'switching', 'token': 'test-token', 'source_sha': B, 'previous_sha': A,
                       'old_id': OLD, 'old_image_id': 'sha256:' + OLD, 'new_image_id': 'sha256:' + NEW,
                       'old_name': 'test-app-previous', 'old_restart': 'unless-stopped',
                       'old_networks': self.app['networks'], 'expected_accounts': 1,
                       'recovery': str(self.directory / 'recovery-test')}

    def test_failed_acceptance_restores_previous_container(self):
        with patch.object(adapters, 'preflight', return_value=1), \
             patch.object(adapters, 'inspect', return_value=self.old), \
             patch.object(adapters, 'run'), patch.object(adapters, 'snapshot'), \
             patch.object(adapters, 'disconnect'), patch.object(adapters, 'connect'), \
             patch.object(adapters, 'create', return_value=NEW), \
             patch.object(adapters, 'probe', side_effect=common.Deferred('bad health')), \
             patch.object(adapters, 'rollback') as rollback:
            with self.assertRaisesRegex(common.Rejected, 'previous container restored'):
                adapters.deploy(self.app, self.prepared, self.verified, None, self.directory, A)
        rollback.assert_called_once()
        self.assertEqual(common.read(self.directory / 'transaction.json')['state'], 'rolled_back')

    def test_config_drift_never_stops_production(self):
        with patch.object(adapters, 'preflight', side_effect=common.Rejected('drift')), \
             patch.object(adapters, 'run') as run:
            with self.assertRaises(common.Rejected):
                adapters.deploy(self.app, self.prepared, self.verified, None, self.directory, A)
        run.assert_not_called()

    def test_interrupted_healthy_candidate_is_accepted_without_stop(self):
        common.write_json(self.directory / 'transaction.json', self.record)
        live = {'Id': NEW, 'Image': 'sha256:' + NEW, 'Config': {'Labels': {adapters.OWNER: 'test-token'}}}
        with patch.object(adapters, 'named_container', return_value=live), \
             patch.object(adapters, 'probe'), patch.object(adapters, 'contract'), \
             patch.object(adapters, 'inspect', return_value=live), \
             patch.object(adapters, 'current', return_value=B), \
             patch.object(adapters, 'finalize'), patch.object(adapters, 'rollback') as rollback, \
             patch.object(adapters, 'run') as run:
            self.assertEqual(adapters.recover(self.app, self.directory), 'success')
        rollback.assert_not_called()
        self.assertFalse(any('stop' in call.args[0] for call in run.call_args_list))

    def test_interrupted_unhealthy_candidate_is_rolled_back_and_quarantined(self):
        common.write_json(self.directory / 'transaction.json', self.record)
        live = {'Config': {'Labels': {adapters.OWNER: 'test-token'}}}
        with patch.object(adapters, 'named_container', return_value=live), \
             patch.object(adapters, 'probe', side_effect=common.Deferred('not ready')), \
             patch.object(adapters, 'rollback') as rollback:
            self.assertEqual(adapters.recover(self.app, self.directory), 'rolled_back')
        rollback.assert_called_once()
        self.assertEqual(common.read(self.directory / 'failed.json')['sha'], B)

    def test_unrelated_container_cannot_be_removed_during_rollback(self):
        other = {'Id': NEW, 'Config': {'Labels': {adapters.OWNER: 'someone-else'}}}
        with patch.object(adapters, 'inspect', return_value=self.old), \
             patch.object(adapters, 'named_container', return_value=other), \
             patch.object(adapters, 'run') as run:
            with self.assertRaises(common.Rejected):
                adapters.rollback(self.app, self.record)
        run.assert_not_called()

    def test_success_bookkeeping_never_restarts_accepted_container(self):
        common.write_json(self.directory / 'transaction.json', {**self.record, 'state': 'success'})
        with patch.object(adapters, 'finalize') as finalize, patch.object(adapters, 'run') as run:
            self.assertEqual(adapters.recover(self.app, self.directory), 'success')
        finalize.assert_called_once()
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
