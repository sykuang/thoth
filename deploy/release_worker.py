#!/usr/bin/env python3
"""Local operator release of an already built/tested worker image; dry run by default."""
import argparse
import copy
import fcntl
import hashlib
from pathlib import Path
import stat
import time
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = '2024-03-01'
HOST = 'https://management.azure.com'
APP = 'thoth-backend-public'
JOBS = ('thoth-sync-queued', 'thoth-sync-scheduled', 'thoth-payment-reminders')
REGISTRY = 'thothacr21df07.azurecr.io'
REPOSITORY = 'thoth-backend'
DIGEST = r'sha256:[0-9a-f]{64}'


class ReleaseError(Exception):
    """Only fixed, locally defined safe codes may reach operator output."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def image_selector(image):
    prefix = REGISTRY + '/' + REPOSITORY
    if not re.fullmatch(re.escape(prefix) + r'(?::[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7,40}|@' + DIGEST + ')', image):
        raise ReleaseError('invalid_image_ref')
    return image[len(REGISTRY) + 1:]


def az(args, timeout):
    env = dict(os.environ, AZURE_CORE_COLLECT_TELEMETRY='no', AZURE_CORE_CHECK_VERSION='false')
    try:
        result = subprocess.run(['az', *args, '--only-show-errors', '-o', 'json'],
                                capture_output=True, text=True, timeout=timeout, env=env)
        if result.returncode:
            missing = re.search(r'\bMANIFEST_UNKNOWN\b', result.stderr, re.IGNORECASE) or (
                result.returncode == 3 and re.search(r'^ERROR: The specified tag does not exist\.', result.stderr, re.MULTILINE))
            if args[:3] == ['acr', 'repository', 'show'] and missing:
                raise ReleaseError('manifest_missing')
            raise ReleaseError('az_failed')
        return json.loads(result.stdout)
    except ReleaseError:
        raise
    except Exception:
        raise ReleaseError('az_failed') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReleaseError('arm_redirect_refused')


def request(url, token, method='GET', payload=None, timeout=60):
    if urllib.parse.urlsplit(url).netloc != 'management.azure.com' or not url.startswith(HOST + '/'):
        raise ReleaseError('unexpected_arm_host')
    req = urllib.request.Request(url, method=method,
                                 data=None if payload is None else json.dumps(payload).encode(),
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            body = response.read()
            return json.loads(body) if body.strip() else {}
    except Exception:
        raise ReleaseError('arm_request_failed') from None


def stable(resource, omit_image=False):
    value = copy.deepcopy(resource)
    for key in ('id', 'name', 'type', 'systemData', 'etag'):
        value.pop(key, None)
    p = value['properties']
    # Read-only operational fields, never configuration/template fields.
    for key in ('provisioningState', 'runningStatus', 'eventStreamEndpoint'):
        p.pop(key, None)
    if omit_image:
        for container in p['template']['containers']:
            container.pop('image', None)
    return digest(value)


def images(resource):
    return [c['image'] for c in resource['properties']['template']['containers']]


def save(path, data):
    """Exclusive immutable creation; flush file AND parent before external writes."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'data': data, 'checksum': digest(data)}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o377:
            raise ReleaseError('unsafe_state_file')
        envelope = json.load(stream)
    if set(envelope) != {'data', 'checksum'} or digest(envelope['data']) != envelope['checksum']:
        raise ReleaseError('state_checksum_mismatch')
    return envelope['data']


def rollout(args, state=None):
    auth = az(['account', 'get-access-token', '--resource', HOST + '/'], args.timeout)
    sub, token = auth['subscription'], auth['accessToken']
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', sub):
        raise ReleaseError('invalid_subscription')
    root = HOST + f'/subscriptions/{sub}/resourceGroups/thoth-rg/providers/Microsoft.App/'
    acr = ['acr', 'repository']
    registry_args = ['--name', REGISTRY.split('.')[0], '--subscription', sub]

    def resolve(image):
        selector = image_selector(image)
        data = az([*acr, 'show', *registry_args, '--image', selector], args.timeout)
        resolved = data['digest']
        if not re.fullmatch(DIGEST, resolved):
            raise ReleaseError('invalid_manifest_digest')
        if '@' in selector and selector.split('@')[1] != resolved:
            raise ReleaseError('manifest_digest_mismatch')
        return resolved

    candidate_digest = resolve(args.image)
    if args.expected_digest and args.expected_digest != candidate_digest:
        raise ReleaseError('manifest_digest_mismatch')
    binding = {'schema': 1, 'subscription': sub, 'root': root, 'image': args.image, 'digest': candidate_digest}

    def url(name):
        return root + ('containerApps/' if name == APP else 'jobs/') + name + '?api-version=' + API_VERSION

    def read(name):
        current = request(url(name), token, timeout=args.timeout)
        if current['name'] != name or current['id'].lower() != url(name).split('?')[0][len(HOST):].lower():
            raise ReleaseError('resource_identity_mismatch')
        if name in JOBS and len(current['properties']['template']['containers']) != 1:
            raise ReleaseError('unexpected_container_count')
        return current

    def executions(name):
        base = url(name).split('?')[0] + '/executions'
        next_url = base + '?api-version=' + API_VERSION
        seen, identities = set(), set()
        for _ in range(args.max_pages):
            if not next_url:
                return sorted(identities)
            if next_url in seen or next_url.split('?')[0] != base:
                raise ReleaseError('invalid_execution_pagination')
            seen.add(next_url)
            page = request(next_url, token, timeout=args.timeout)
            for row in page['value']:
                identity = row['id']
                if not isinstance(identity, str) or not identity.lower().startswith(base[len(HOST):].lower() + '/'):
                    raise ReleaseError('invalid_execution_identity')
                identities.add(digest(identity.lower()))
            next_url = page.get('nextLink')
        if next_url:
            raise ReleaseError('execution_page_limit')
        return sorted(identities)

    baseline_path = state / 'baseline.json' if state else None
    if baseline_path and baseline_path.exists():
        before = load(baseline_path)
        if before['binding'] != binding or set(before['jobs']) != set(JOBS):
            raise ReleaseError('state_binding_mismatch')
    else:
        if state and any(state.glob('*.receipt.json')):
            raise ReleaseError('orphan_receipt')
        app = read(APP)
        before = {'binding': binding, 'app': {'id': app['id'], 'spec': stable(app)}, 'jobs': {}, 'executions': {}}
        for name in JOBS:
            job = read(name)
            image_selector(images(job)[0])
            if job['properties'].get('provisioningState') != 'Succeeded':
                raise ReleaseError('target_not_ready')
            before['jobs'][name] = {'id': job['id'], 'spec': stable(job), 'nonimage': stable(job, True), 'image': images(job)[0]}
            before['executions'][name] = executions(name)
        if state and args.apply:
            save(baseline_path, before)

    def receipt(name):
        return {'baseline': digest(before), 'binding': binding, 'job': name, 'original': before['jobs'][name]}

    applied = set()
    if state:
        expected = {name + '.receipt.json' for name in JOBS}
        if any(path.name not in expected for path in state.glob('*.receipt.json')):
            raise ReleaseError('unexpected_receipt')
        for name in JOBS:
            path = state / (name + '.receipt.json')
            if path.exists():
                if load(path) != receipt(name):
                    raise ReleaseError('receipt_binding_mismatch')
                applied.add(name)

    def check_job(current, name, updated=False):
        baseline = before['jobs'][name]
        if current['id'].lower() != baseline['id'].lower():
            raise ReleaseError('job_identity_drift')
        if stable(current, True) != baseline['nonimage']:
            raise ReleaseError('nonimage_spec_changed')
        if not updated and stable(current) != baseline['spec']:
            raise ReleaseError('job_baseline_drift')

    def confirm(name):
        for attempt in range(args.poll_attempts):
            current = read(name)
            check_job(current, name, updated=True)
            status = current['properties'].get('provisioningState')
            if status in ('Failed', 'Canceled', 'Cancelled'):
                raise ReleaseError('target_failed_reconcile_only')
            if status == 'Succeeded' and images(current) == [args.image]:
                return
            if attempt + 1 < args.poll_attempts:
                time.sleep(args.poll_interval)
        raise ReleaseError('update_unconfirmed_reconcile_only')

    def check_all():
        app = read(APP)
        if app['id'].lower() != before['app']['id'].lower() or stable(app) != before['app']['spec']:
            raise ReleaseError('api_baseline_drift')
        for name in JOBS:
            current = read(name)
            check_job(current, name, name in applied)
            expected_image = args.image if name in applied else before['jobs'][name]['image']
            if current['properties'].get('provisioningState') != 'Succeeded' or images(current) != [expected_image]:
                raise ReleaseError('cohort_readback_unconfirmed')

    # Existing receipts are reconciliation obligations, never permission to resend.
    for name in JOBS:
        if name in applied:
            confirm(name)
    check_all()
    previous_digests = {}
    for previous in sorted({j['image'] for j in before['jobs'].values()} - {args.image}):
        try:
            previous_digests[previous] = resolve(previous)
        except ReleaseError as exc:
            if str(exc) != 'manifest_missing':
                raise
            # Only a prior main-container reference being replaced may be absent.
            print(json.dumps({'warning': 'previous_reference_missing', 'reference_hash': digest(previous),
                              'jobs': [n for n in JOBS if before['jobs'][n]['image'] == previous]}), flush=True)
    if not args.apply:
        print(json.dumps({'status': 'dry_run', 'jobs': list(JOBS), 'image': args.image, 'digest': candidate_digest}))
        return

    def protect(image, resolved):
        selectors = [REPOSITORY + '@' + resolved]
        if '@' not in image:
            selectors.append(image_selector(image))
        for selector in selectors:
            az([*acr, 'update', *registry_args, '--image', selector,
                '--delete-enabled', 'false', '--write-enabled', 'false'], args.timeout)
            verified = az([*acr, 'show', *registry_args, '--image', selector], args.timeout)
            attributes = verified.get('changeableAttributes', {})
            if (verified.get('digest') != resolved or attributes.get('deleteEnabled') is not False
                    or attributes.get('writeEnabled') is not False or attributes.get('readEnabled') is not True):
                raise ReleaseError('image_lock_unverified')

    protect(args.image, candidate_digest)
    for previous, resolved in previous_digests.items():
        protect(previous, resolved)

    for name in JOBS:
        if name in applied:
            continue
        check_all()
        current = read(name)  # Final ARM read before this target's receipt + PATCH.
        check_job(current, name)
        if current['properties'].get('provisioningState') != 'Succeeded':
            raise ReleaseError('target_not_ready')
        if images(current) != [args.image]:
            template = copy.deepcopy(current['properties']['template'])
            template['containers'][0]['image'] = args.image
            save(state / (name + '.receipt.json'), receipt(name))
            try:
                request(url(name), token, method='PATCH',
                        payload={'properties': {'template': template}}, timeout=args.timeout)
            except Exception:
                print(json.dumps({'job': name, 'patch_ack': 'uncertain'}), flush=True)
            confirm(name)
        applied.add(name)
    check_all()
    changes = {}
    for name in JOBS:
        old, new = set(before['executions'][name]), set(executions(name))
        changes[name] = {'added_identity_hashes': sorted(new - old), 'removed_identity_hashes': sorted(old - new)}
    check_all()
    print(json.dumps({'status': 'succeeded', 'jobs': list(JOBS), 'image': args.image,
                      'digest': candidate_digest, 'execution_changes_observed': changes}))


def run(args):
    image_selector(args.image)
    if args.expected_digest and not re.fullmatch(DIGEST, args.expected_digest):
        raise ReleaseError('invalid_expected_digest')
    if args.apply and not args.exclusive_approved:
        raise ReleaseError('exclusive_approval_required')
    if args.apply and not args.state_dir:
        raise ReleaseError('private_state_dir_required')
    if not args.state_dir:
        return rollout(args)
    state = Path(args.state_dir)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReleaseError('private_state_dir_required')
        # ponytail: flock covers this local state directory, not other hosts/CAS.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError('local_deployment_locked') from None
        return rollout(args, state)
    finally:
        os.close(fd)


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise ReleaseError('invalid_arguments')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--exclusive-approved', action='store_true')
    parser.add_argument('--state-dir')
    parser.add_argument('--expected-digest')
    parser.add_argument('--timeout', type=int, default=60, help='per CLI/HTTP call seconds, 1..300')
    parser.add_argument('--poll-attempts', type=int, default=24)
    parser.add_argument('--poll-interval', type=int, default=5)
    parser.add_argument('--max-pages', type=int, default=100)
    try:
        args = parser.parse_args(argv)
        if not (1 <= args.timeout <= 300 and 1 <= args.poll_attempts <= 120
                and 1 <= args.poll_interval <= 60 and 1 <= args.max_pages <= 1000):
            raise ReleaseError('invalid_timeout')
        run(args)
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'safe_code': str(exc) if type(exc) is ReleaseError else 'operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
