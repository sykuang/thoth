"""Offline only: synthetic ARM transport and fake az subprocess responses."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/release_worker.py'


class ReleaseTests(unittest.TestCase):
    def load(self):
        self.assertTrue(SCRIPT.is_file(), 'durable worker release entrypoint is missing')
        spec = importlib.util.spec_from_file_location('worker_release', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_arm_redirects_are_refused_without_forwarding_credentials(self):
        m = self.load()
        # Exercise redirect handling without opening a socket.
        self.assertTrue(hasattr(m, 'NoRedirect'), 'ARM transport needs explicit no-redirect handling')
        with self.assertRaises(m.ReleaseError):
            m.NoRedirect().redirect_request(None, None, 302, 'private-sentinel', {}, 'https://evil.invalid/')

    def test_arm_transport_json_and_redacted_failure(self):
        m = self.load()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"value": []}'
        with patch.object(m.urllib.request.OpenerDirector, 'open', return_value=response) as opened:
            self.assertEqual(m.request(m.HOST + '/test', 'credential-sentinel', timeout=7), {'value': []})
            self.assertEqual(opened.call_args.kwargs['timeout'], 7)
        with patch.object(m.urllib.request.OpenerDirector, 'open', side_effect=OSError('private-sentinel')), \
             self.assertRaisesRegex(m.ReleaseError, '^arm_request_failed$'):
            m.request(m.HOST + '/test', 'credential-sentinel')
        with patch.object(m.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('must not send')), \
             self.assertRaisesRegex(m.ReleaseError, '^unexpected_arm_host$'):
            m.request('https://evil.invalid/test', 'credential-sentinel')

    def test_shell_worker_dispatch_precedes_bootstrap_validation(self):
        import os
        env = dict(os.environ, BOOTSTRAP_NETWORK_ONLY='invalid', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run(['bash', str(SCRIPT.parent / 'deploy.sh'), 'worker', '--help'],
                                capture_output=True, text=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--exclusive-approved', result.stdout)
        self.assertNotIn('First run', result.stdout)

    def test_dry_run_reads_exact_cohort_without_writes(self):
        m = self.load()
        image = m.REGISTRY + '/' + m.REPOSITORY + ':0.3.122-ce92d05'
        digest = 'sha256:' + 'a' * 64
        root = '/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/thoth-rg/providers/Microsoft.App/'
        resources = {}
        for name in (m.APP, *m.JOBS):
            resources[name] = {'id': root + ('containerApps/' if name == m.APP else 'jobs/') + name,
                               'name': name, 'location': 'test', 'properties': {
                                   'provisioningState': 'Succeeded',
                                   'configuration': {'secret': 'private-sentinel'},
                                   'template': {'containers': [{'name': 'worker', 'image': image}]}}}
        calls = []
        def request(url, token, method='GET', payload=None, timeout=60):
            calls.append((method, url))
            self.assertEqual(method, 'GET')
            if '/executions?' in url:
                return {'value': []}
            return copy.deepcopy(resources[url.split('?', 1)[0].rsplit('/', 1)[-1]])
        def cli(cmd, **kwargs):
            self.assertEqual(cmd[0], 'az')
            self.assertIn('timeout', kwargs)
            self.assertTrue(kwargs['capture_output'])
            self.assertNotIn('update', cmd)
            data = {'accessToken': 'credential-sentinel', 'subscription': root.split('/')[2]} if cmd[1] == 'account' else {'digest': digest}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')
        output = io.StringIO()
        with patch.object(m, 'request', side_effect=request), patch.object(m.subprocess, 'run', side_effect=cli), \
             patch.object(m.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('network forbidden')), contextlib.redirect_stdout(output):
            self.assertEqual(m.main(['--image', image]), 0)
        self.assertEqual({url.split('?', 1)[0].rsplit('/', 1)[-1] for _, url in calls if '/executions?' not in url}, {m.APP, *m.JOBS})
        self.assertNotIn('sentinel', output.getvalue())
        self.assertIn('dry_run', output.getvalue())


class ApplyTests(unittest.TestCase):
    load = ReleaseTests.load

    def setUp(self):
        import tempfile
        self.m = self.load()
        self.tmp = tempfile.TemporaryDirectory(prefix='worker-release-test-')
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        m = self.m
        self.image = m.REGISTRY + '/' + m.REPOSITORY + ':0.3.122-ce92d05'
        self.old = m.REGISTRY + '/' + m.REPOSITORY + ':0.3.121-abcdef0'
        self.manifest = 'sha256:' + 'a' * 64
        self.prior_digest = 'sha256:' + 'b' * 64
        self.sub = '00000000-0000-0000-0000-000000000001'
        self.root = f'/subscriptions/{self.sub}/resourceGroups/thoth-rg/providers/Microsoft.App/'
        self.resources = {}
        for name in (m.APP, *m.JOBS):
            self.resources[name] = {'name': name, 'id': self.root + ('containerApps/' if name == m.APP else 'jobs/') + name,
                'location': 'test', 'tags': {'test': 'private-sentinel'}, 'properties': {
                'provisioningState': 'Succeeded', 'environmentId': 'test-environment',
                'configuration': {'triggerType': 'Schedule', 'scheduleTriggerConfig': {'cronExpression': '*/5 * * * *'},
                                  'secrets': [{'name': 'secret', 'value': 'private-sentinel'}]},
                'template': {'containers': [{'name': 'worker', 'image': self.old if name != m.APP else 'api:unchanged',
                            'command': ['original'], 'args': ['private-sentinel'],
                            'env': [{'name': 'VALUE', 'value': 'private-sentinel'}],
                            'resources': {'cpu': 0.5, 'memory': '1Gi'}}], 'volumes': [{'name': 'original'}]}}}
        self.before = copy.deepcopy(self.resources)
        self.calls = []
        self.cli_calls = []
        self.locks = set()
        self.hook = lambda name, method: None
        self.cli_hook = lambda cmd: None
        self.states = {}
        self.uncertain = set()
        self.unapplied = set()
        self.output = ''

    def cli(self, cmd, **kwargs):
        self.cli_calls.append(cmd)
        self.assertEqual(cmd[0], 'az')
        self.assertTrue(kwargs['capture_output'])
        self.assertGreater(kwargs['timeout'], 0)
        custom = self.cli_hook(cmd)
        if custom is not None:
            return custom
        if cmd[1] == 'account':
            data = {'accessToken': 'credential-sentinel', 'subscription': self.sub}
        else:
            self.assertEqual(cmd[1:3], ['acr', 'repository'])
            self.assertIn(cmd[3], ('show', 'update'))
            selector = cmd[cmd.index('--image') + 1]
            selected_digest = self.manifest if selector in (self.image.split('/', 1)[1], 'thoth-backend@' + self.manifest) else self.prior_digest
            if cmd[3] == 'update':
                self.assertEqual(cmd[cmd.index('--delete-enabled') + 1], 'false')
                self.assertEqual(cmd[cmd.index('--write-enabled') + 1], 'false')
                self.locks.add(selector)
            data = {'digest': selected_digest, 'changeableAttributes': {
                'deleteEnabled': selector not in self.locks, 'writeEnabled': selector not in self.locks, 'readEnabled': True}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(data), '')

    def request(self, url, token, method='GET', payload=None, timeout=60):
        self.calls.append((method, url))
        if '/executions?' in url:
            self.assertEqual(method, 'GET')
            return {'value': [{'id': url.split('?', 1)[0].removeprefix(self.m.HOST) + '/execution-one'}]}
        name = url.split('?', 1)[0].rsplit('/', 1)[-1]
        self.hook(name, method)
        if method == 'PATCH':
            self.assertIn(name, self.m.JOBS)
            self.assertTrue((self.state / (name + '.receipt.json')).is_file(), 'receipt must precede submission')
            self.assertIn('thoth-backend@' + self.manifest, self.locks)
            self.assertIn(self.image.split('/', 1)[1], self.locks)
            self.assertEqual(self.calls[-2], ('GET', url), 'fresh target must be final ARM read')
            if name not in self.unapplied:
                self.resources[name]['properties']['template'] = copy.deepcopy(payload['properties']['template'])
            if name in self.uncertain or name in self.unapplied:
                raise OSError('private-sentinel lost acknowledgement')
        else:
            self.assertEqual(method, 'GET')
            states = self.states.get(name, [])
            if states and any(method == 'PATCH' and address.split('?', 1)[0].endswith('/' + name) for method, address in self.calls):
                self.resources[name]['properties']['provisioningState'] = states.pop(0) if len(states) > 1 else states[0]
        return copy.deepcopy(self.resources[name])

    def release(self, extra=None, apply=True):
        args = ['--image', self.image, '--state-dir', str(self.state), '--poll-attempts', '3', '--poll-interval', '1']
        if apply:
            args += ['--apply', '--exclusive-approved']
        args += extra or []
        output = io.StringIO()
        with patch.object(self.m, 'request', side_effect=self.request), patch.object(self.m.subprocess, 'run', side_effect=self.cli), \
             patch.object(self.m.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('network forbidden')), \
             patch('time.sleep'), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                code = self.m.main(args)
            except SystemExit as exc:
                code = exc.code
        self.output = output.getvalue()
        self.assertNotIn('private-sentinel', self.output)
        self.assertNotIn('credential-sentinel', self.output)
        return code

    def writes(self):
        return [url.split('?', 1)[0].rsplit('/', 1)[-1] for method, url in self.calls if method != 'GET']

    def test_missing_replaced_prior_is_reported_but_repair_proceeds(self):
        retired = self.m.REGISTRY + '/' + self.m.REPOSITORY + ':0.3.122-7c2d0ec'
        for name in self.m.JOBS[1:]:
            self.resources[name]['properties']['template']['containers'][0]['image'] = retired
        def cli_hook(cmd):
            if '--image' in cmd and cmd[cmd.index('--image') + 1] == retired.split('/', 1)[1]:
                return subprocess.CompletedProcess(cmd, 1, '', 'ERROR: (MANIFEST_UNKNOWN) private-sentinel')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))
        self.assertIn('previous_reference_missing', self.output)
        self.assertNotIn(retired.split('/', 1)[1], self.locks)
        self.assertIn('thoth-backend@' + self.prior_digest, self.locks)

    def test_missing_prior_cli_tag_not_exist_is_tolerated(self):
        def cli_hook(cmd):
            if '--image' in cmd and cmd[cmd.index('--image') + 1] == self.old.split('/', 1)[1]:
                return subprocess.CompletedProcess(cmd, 3, '', 'ERROR: The specified tag does not exist.\n')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 0, self.output)
        self.assertIn('previous_reference_missing', self.output)

    def test_disabled_image_reads_block_patches(self):
        def cli_hook(cmd):
            if cmd[1:4] == ['acr', 'repository', 'show'] and self.locks and cmd[cmd.index('--image') + 1] in (
                    self.image.split('/', 1)[1], 'thoth-backend@' + self.manifest):
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'digest': self.manifest,
                    'changeableAttributes': {'deleteEnabled': False, 'writeEnabled': False, 'readEnabled': False}}), '')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertIn('image_lock_unverified', self.output)

    def test_missing_candidate_is_never_tolerated(self):
        def cli_hook(cmd):
            if cmd[1:4] == ['acr', 'repository', 'show']:
                return subprocess.CompletedProcess(cmd, 1, '', 'ERROR: (MANIFEST_UNKNOWN) private-sentinel')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.locks, set())

    def test_prior_auth_failure_is_not_missing(self):
        def cli_hook(cmd):
            if '--image' in cmd and cmd[cmd.index('--image') + 1] == self.old.split('/', 1)[1]:
                return subprocess.CompletedProcess(cmd, 1, '', 'authorization failed: private-sentinel')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertNotIn('previous_reference_missing', self.output)

    def test_missing_prior_reported_in_readonly_dry_run(self):
        self.cli_hook = lambda cmd: subprocess.CompletedProcess(cmd, 1, '', '(MANIFEST_UNKNOWN) private-sentinel') \
            if '--image' in cmd and cmd[cmd.index('--image') + 1] == self.old.split('/', 1)[1] else None
        self.assertEqual(self.release(apply=False), 0, self.output)
        self.assertIn('previous_reference_missing', self.output)
        self.assertFalse(any(self.state.iterdir()))
        self.assertEqual(self.locks, set())

    def test_invalid_candidate_and_cli_errors_are_redacted(self):
        for suffix in (':latest', ':stable', ':0.3.122', ':0.3.122-xyz', '@sha256:bad', ':0.3.122-abcdef0/private-sentinel'):
            self.assertEqual(self.release(['--image', self.m.REGISTRY + '/' + self.m.REPOSITORY + suffix]), 1)
        self.assertEqual(self.release(['--image', 'evil.invalid/thoth-backend:0.3.122-abcdef0']), 1)
        self.assertEqual(self.release(['--private-sentinel']), 1)
        self.assertEqual(self.release(['--timeout', 'private-sentinel']), 1)
        self.assertEqual(self.cli_calls, [])
        self.assertEqual(self.calls, [])

    def test_digest_mismatch_and_lock_failures_precede_patches(self):
        self.assertEqual(self.release(['--expected-digest', self.prior_digest]), 1)
        self.assertEqual(self.release(['--image', self.m.REGISTRY + '/' + self.m.REPOSITORY + '@' + 'sha256:' + 'c' * 64]), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.locks, set())
        self.cli_hook = lambda cmd: subprocess.CompletedProcess(cmd, 1, '', 'private-sentinel') if 'update' in cmd else None
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])

    def test_lock_readback_false_or_moved_tag_is_fatal(self):
        def cli_hook(cmd):
            if cmd[1:4] == ['acr', 'repository', 'show'] and self.locks:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'digest': self.manifest,
                    'changeableAttributes': {'deleteEnabled': True, 'writeEnabled': False}}), '')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 1)
        self.assertIn('image_lock_unverified', self.output)
        self.assertEqual(self.writes(), [])

    def test_exclusive_approval_required_before_credentials(self):
        output = io.StringIO()
        with patch.object(self.m.subprocess, 'run', side_effect=AssertionError('must not authenticate')), contextlib.redirect_stdout(output):
            self.assertEqual(self.m.main(['--image', self.image, '--apply', '--state-dir', str(self.state)]), 1)
        self.assertIn('exclusive_approval_required', output.getvalue())

    def test_pending_then_success(self):
        self.states[self.m.JOBS[0]] = ['Updating', 'Creating', 'Succeeded']
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))

    def test_failed_target_retains_receipt_without_success(self):
        self.states[self.m.JOBS[0]] = ['Failed']
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [self.m.JOBS[0]])
        self.assertIn('target_failed_reconcile_only', self.output)
        self.assertNotIn('"status": "succeeded"', self.output)
        self.assertTrue((self.state / (self.m.JOBS[0] + '.receipt.json')).exists())

    def test_pending_partial_resume_continues_without_duplicate(self):
        first, second, _ = self.m.JOBS
        self.states[second] = ['Updating']
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [first, second])
        self.assertNotIn('"status": "succeeded"', self.output)
        self.states[second] = ['Succeeded']
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))

    def test_uncertain_ack_applied_is_confirmed_not_resent(self):
        self.uncertain.add(self.m.JOBS[0])
        self.assertEqual(self.release(), 0, self.output)
        self.assertIn('uncertain', self.output)
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))

    def test_uncertain_unapplied_never_resent_on_resume(self):
        first = self.m.JOBS[0]
        self.unapplied.add(first)
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [first])
        self.assertIn('update_unconfirmed_reconcile_only', self.output)

    def test_resume_rejects_unsubmitted_and_applied_drift(self):
        first, second, _ = self.m.JOBS
        self.states[first] = ['Updating']
        self.assertEqual(self.release(), 1)
        self.states[first] = ['Succeeded']
        self.resources[second]['properties']['template']['containers'][0]['image'] = self.image
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [first])
        self.assertIn('job_baseline_drift', self.output)
        self.resources[second] = copy.deepcopy(self.before[second])
        self.resources[first]['properties']['template']['containers'][0]['command'] = ['drift']
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [first])
        self.assertIn('nonimage_spec_changed', self.output)

    def test_fresh_target_configuration_drift_blocks(self):
        target = self.m.JOBS[0]
        reads = []
        def hook(name, method):
            if method == 'GET' and name == target:
                reads.append(name)
                if len(reads) == 4:  # baseline, preflight, cohort check, final target read
                    self.resources[name]['properties']['configuration']['scheduleTriggerConfig']['cronExpression'] = 'drift'
        self.hook = hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertIn('nonimage_spec_changed', self.output)

    def test_nonimage_and_api_postflight_drift_fail(self):
        def hook(name, method):
            if method == 'GET' and name == self.m.APP and self.writes():
                self.resources[name]['properties']['template']['containers'][0]['image'] = 'drift'
        self.hook = hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [self.m.JOBS[0]])
        self.assertIn('api_baseline_drift', self.output)
        self.assertNotIn('"status": "succeeded"', self.output)

    def test_patch_side_effect_nonimage_drift_is_not_success(self):
        def hook(name, method):
            if method == 'PATCH':
                self.resources[name]['properties']['configuration']['triggerType'] = 'Manual'
        self.hook = hook
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [self.m.JOBS[0]])
        self.assertIn('nonimage_spec_changed', self.output)

    def test_receipt_tampering_and_state_binding_drift_rejected(self):
        self.assertEqual(self.release(), 0, self.output)
        receipt = self.state / (self.m.JOBS[0] + '.receipt.json')
        data = json.loads(receipt.read_text())
        data['data']['job'] = self.m.JOBS[1]
        data['checksum'] = self.m.digest(data['data'])
        receipt.chmod(0o600)
        receipt.write_text(json.dumps(data))
        receipt.chmod(0o400)
        self.assertEqual(self.release(), 1)
        self.assertIn('receipt_binding_mismatch', self.output)
        self.assertEqual(self.release(['--image', self.old]), 1)
        self.assertIn('state_binding_mismatch', self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))

    def test_same_image_skips_arm_write(self):
        for name in self.m.JOBS:
            self.resources[name]['properties']['template']['containers'][0]['image'] = self.image
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), [])
        self.assertFalse(list(self.state.glob('*.receipt.json')))

    def test_execution_pagination_reports_identity_changes_not_counts(self):
        original = self.request
        def request(url, token, method='GET', payload=None, timeout=60):
            if '/executions?' not in url:
                return original(url, token, method, payload, timeout)
            self.calls.append((method, url))
            base = url.split('?', 1)[0]
            identity = 'unchanged' if 'page=2' in url else ('after' if self.writes() else 'before')
            page = {'value': [{'id': base.removeprefix(self.m.HOST) + '/' + identity}]}
            if 'page=2' not in url:
                page['nextLink'] = base + '?api-version=' + self.m.API_VERSION + '&page=2'
            return page
        self.request = request
        self.assertEqual(self.release(), 0, self.output)
        result = json.loads(self.output.splitlines()[-1])
        for name in self.m.JOBS:
            prefix = self.root + 'jobs/' + name + '/executions/'
            changes = result['execution_changes_observed'][name]
            self.assertEqual(changes['added_identity_hashes'], [self.m.digest((prefix + 'after').lower())])
            self.assertEqual(changes['removed_identity_hashes'], [self.m.digest((prefix + 'before').lower())])

    def test_pagination_foreign_nextlink_refused(self):
        original = self.request
        def request(url, token, method='GET', payload=None, timeout=60):
            if '/executions?' in url:
                return {'value': [], 'nextLink': 'https://evil.invalid/private-sentinel'}
            return original(url, token, method, payload, timeout)
        self.request = request
        self.assertEqual(self.release(), 1)
        self.assertIn('invalid_execution_pagination', self.output)
        self.assertEqual(self.writes(), [])

    def test_local_lock_and_unsafe_state_refuse_before_auth(self):
        import fcntl
        import os
        fd = os.open(self.state, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.release(), 1)
            self.assertIn('local_deployment_locked', self.output)
        finally:
            os.close(fd)
        self.state.chmod(0o755)
        self.assertEqual(self.release(), 1)
        self.assertIn('private_state_dir_required', self.output)
        self.assertEqual(self.cli_calls, [])

    def test_digest_candidate_success_locks_manifest_without_tag(self):
        self.image = self.m.REGISTRY + '/' + self.m.REPOSITORY + '@' + self.manifest
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))
        self.assertIn('thoth-backend@' + self.manifest, self.locks)
        self.assertNotIn('thoth-backend:0.3.122-ce92d05', self.locks)

    def test_receipt_fsync_failure_never_submits(self):
        original = self.m.os.fsync
        def fsync(fd):
            if any(self.state.glob('*.receipt.json')):
                raise OSError('private-sentinel fsync failure')
            return original(fd)
        with patch.object(self.m.os, 'fsync', side_effect=fsync):
            self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.release(), 1)
        self.assertEqual(self.writes(), [])
        self.assertIn('update_unconfirmed_reconcile_only', self.output)

    def test_state_checksum_failure_and_deleted_receipt_do_not_resubmit(self):
        self.assertEqual(self.release(), 0, self.output)
        receipt = self.state / (self.m.JOBS[0] + '.receipt.json')
        receipt.unlink()
        self.assertEqual(self.release(), 1)
        self.assertIn('job_baseline_drift', self.output)
        baseline = self.state / 'baseline.json'
        data = json.loads(baseline.read_text())
        data['checksum'] = '0' * 64
        baseline.chmod(0o600)
        baseline.write_text(json.dumps(data))
        baseline.chmod(0o400)
        self.assertEqual(self.release(), 1)
        self.assertIn('state_checksum_mismatch', self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))

    def test_api_nonimage_drift_blocks_later_targets(self):
        def hook(name, method):
            if method == 'GET' and name == self.m.APP and self.writes():
                self.resources[name]['properties']['template']['containers'][0]['env'][0]['value'] = 'drift'
        self.hook = hook
        self.assertEqual(self.release(), 1)
        self.assertIn('api_baseline_drift', self.output)
        self.assertEqual(self.writes(), [self.m.JOBS[0]])

    def test_candidate_tag_moves_between_resolution_and_lock_verification(self):
        def cli_hook(cmd):
            if cmd[1:4] == ['acr', 'repository', 'show'] and '--image' in cmd and self.locks \
                    and cmd[cmd.index('--image') + 1] == self.image.split('/', 1)[1]:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'digest': self.prior_digest,
                    'changeableAttributes': {'deleteEnabled': False, 'writeEnabled': False, 'readEnabled': True}}), '')
        self.cli_hook = cli_hook
        self.assertEqual(self.release(), 1)
        self.assertIn('image_lock_unverified', self.output)
        self.assertEqual(self.writes(), [])

    def test_apply_three_jobs_preserves_nonimage_and_api(self):
        self.assertEqual(self.release(), 0, self.output)
        self.assertEqual(self.writes(), list(self.m.JOBS))
        self.assertEqual(self.resources[self.m.APP], self.before[self.m.APP])
        for name in self.m.JOBS:
            self.assertEqual(self.m.stable(self.resources[name], True), self.m.stable(self.before[name], True))
            self.assertEqual(self.m.images(self.resources[name]), [self.image])
        self.assertIn('"status": "succeeded"', self.output)
        for path in self.state.glob('*.json'):
            self.assertNotIn('private-sentinel', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
