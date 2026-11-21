#!/usr/bin/env python3
"""Update only this repository's application after exact hosted CI acceptance."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import adapters
from common import CONFIG, STATE, PROJECT, SHA, Deferred, Rejected, locks, now, read, write_json
from sources import Source
import updater


def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise Rejected('Configuration file is missing: ' + str(path))
    if path.is_symlink() or path.stat().st_mode & 0o022 or path.stat().st_uid != os.geteuid():
        raise Rejected('Configuration must be owned by the deployment user and not writable by others')
    config = read(path)
    if config.get('version') != 1 or not isinstance(config.get('application'), dict) or 'applications' in config:
        raise Rejected('Unsupported deployment configuration')
    app = config['application']
    if any(app.get(key) != PROJECT[key] for key in ('id', 'repository', 'kind', 'workflow', 'branch')):
        raise Rejected('This updater can manage only its own project')
    if (not Path(app.get('root', '')).is_absolute() or not app.get('locks')
            or not app.get('health') or not isinstance(app.get('enabled'), bool)):
        raise Rejected('Application contract is incomplete')
    if app.get('container') != PROJECT['name'] or app.get('image') != 'ghcr.io/wesperez/antigravity-manager':
        raise Rejected('Container identity differs from this project')
    if (app.get('host_ip') != '127.0.0.1' or app.get('container_port') != 8045
            or type(app.get('host_port')) is not int or not 1 <= app['host_port'] <= 65535
            or not isinstance(app.get('networks'), dict) or not app['networks']
            or not app.get('web', {}).get('url', '').endswith('/')
            or not app.get('web', {}).get('base_path', '').startswith('/')
            or not Path(app.get('env_file', '')).is_absolute()
            or any(not Path(lock).is_absolute() for lock in app['locks'])
            or app.get('restart') not in ('unless-stopped', 'always')
            or app.get('logging', {}).get('driver') != 'json-file'
            or not {'max-size', 'max-file'}.issubset(app['logging'].get('options', {}))):
        raise Rejected('Container runtime configuration is incomplete or unsafe')
    # Required jobs belong to versioned source, not stale host configuration.
    app['required_jobs'] = PROJECT.get('required_jobs', [])
    state = Path(config.get('state_root', str(STATE)))
    if not state.is_absolute() or state.resolve() != state:
        raise Rejected('State root must be an absolute path without symlinks')
    for filename in app.get('files', []):
        if Path(filename).is_absolute() or '..' in Path(filename).parts:
            raise Rejected('Runtime file escapes the application root')
    return config


def status(directory, app, state, **detail):
    previous = read(directory / 'status.json', {})
    value = {'application': app['id'], 'state': state, **detail}
    changed = any(previous.get(k) != v for k, v in value.items())
    write_json(directory / 'status.json', {**value, 'checked_at': now()})
    if changed:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    return value


def coordinated_policy(source, sha, expected_revision):
    path = 'deploy/auto-deploy.json'
    # Distinguish a missing policy from a Git read error.
    if source.git('ls-tree', '--name-only', sha, '--', path) != path:
        if expected_revision:
            raise Deferred('This release has no coordinated deployment policy')
        return False
    policy = json.loads(source.git('show', sha + ':' + path))
    if policy == {'version': 1, 'automatic': True}:
        if expected_revision:
            raise Deferred('This release does not require coordinated deployment')
        return False
    if (expected_revision == sha and policy ==
            {'version': 1, 'automatic': False, 'coordination': 'pause_quota_refresh'}):
        return True
    raise Deferred('This release declares a coordinated deployment requirement')


def poll_app(app, state_root, *, apply, expected_revision=None):
    if expected_revision is not None and (not apply or not SHA.fullmatch(expected_revision)):
        raise ValueError('Coordinated deployment requires apply and a complete expected revision')
    directory = Path(state_root) / app['id']
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    sha = None
    try:
        with locks([directory / 'poll.lock']):
            if not app['enabled']:
                return status(directory, app, 'disabled')
            transaction = read(directory / 'transaction.json', {})
            if (expected_revision and transaction.get('state') in ('stopping', 'switching')
                    and transaction.get('source_sha') != expected_revision):
                return status(directory, app, 'attention', reason='Another revision has an unfinished deployment',
                              candidate_sha=transaction.get('source_sha'))
            if transaction.get('state') in ('stopping', 'switching', 'success') and apply:
                adapters.recover(app, directory)
                transaction = read(directory / 'transaction.json', {})
            if transaction.get('state') in ('stopping', 'switching', 'rollback_failed'):
                return status(directory, app, 'attention', reason='Unfinished deployment requires recovery',
                              candidate_sha=transaction.get('source_sha'))
            current = adapters.current(app)
            source = Source(app, directory)
            candidate = source.candidate(current)
            if candidate is None:
                if expected_revision and expected_revision != current:
                    raise Deferred('MINE no longer matches the expected revision')
                if expected_revision:
                    if transaction.get('state') != 'success' or transaction.get('source_sha') != expected_revision:
                        raise Deferred('The expected revision has no accepted coordinated transaction')
                    adapters.check_quota_paused(app, transaction)
                update = {}
                if apply:
                    try:
                        updater.refresh(source, current, directory)
                    except Exception as error:
                        update = {'updater': 'pending', 'updater_reason': type(error).__name__}
                return status(directory, app, 'current', deployed_sha=current, **update)
            sha = candidate['head_sha']
            if expected_revision and expected_revision != sha:
                raise Deferred('MINE no longer matches the expected revision')
            failed = read(directory / 'failed.json', {})
            if failed.get('sha') == sha:
                return status(directory, app, 'quarantined', deployed_sha=current, candidate_sha=sha,
                              reason=failed['reason'])
            verified = source.verified(candidate)
            prepared_policy = coordinated_policy(source, sha, expected_revision)
            if not apply:
                return status(directory, app, 'ready', deployed_sha=current, candidate_sha=sha,
                              run_id=candidate['id'], route=verified['route'])
            adapters.preflight(app, current)
            with tempfile.TemporaryDirectory(prefix='download-', dir=directory) as work:
                # Fetch and validate both deliveries before stopping the app.
                # This runs from an installed immutable snapshot, never a live
                # checkout that application activation may replace.
                update = {}
                try:
                    engine = updater.prepare(source, verified, Path(work) / 'updater')
                except Exception as error:
                    if prepared_policy:
                        raise Deferred('Coordinated release updater artifact is unavailable') from None
                    engine = None
                    update = {'updater': 'pending', 'updater_reason': type(error).__name__}
                prepared = adapters.prepare(app, source, verified, work)
                # Re-resolve after downloads: newer commits do not let an old
                # run race a newer MINE deployment into production.
                latest = source.candidate(current)
                if latest is None or latest['head_sha'] != sha:
                    raise Deferred('MINE advanced during download; reconsider next poll')
                adapters.deploy(app, prepared, verified, source, directory, current,
                                pause_quota_refresh=prepared_policy)
                try:
                    updater.activate(engine, verified)
                except Exception as error:
                    update = {'updater': 'pending', 'updater_reason': type(error).__name__}
            (directory / 'failed.json').unlink(missing_ok=True)
            return status(directory, app, 'current', deployed_sha=sha, run_id=candidate['id'], **update)
    except Deferred as error:
        return status(directory, app, 'waiting', reason=str(error), candidate_sha=sha)
    except Rejected as error:
        if sha:
            write_json(directory / 'failed.json', {'sha': sha, 'reason': str(error), 'at': now()})
        return status(directory, app, 'attention', reason=str(error), candidate_sha=sha)
    except Exception as error:
        # Do not expose GitHub/Compose response bodies, tokens or database data.
        return status(directory, app, 'attention', reason='Updater error: ' + type(error).__name__, candidate_sha=sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('poll', 'status', 'retry', 'check'))
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-revision', help='Apply a coordinated release at this exact MINE SHA')
    args = parser.parse_args()
    if args.expected_revision is not None and (
            args.command != 'poll' or not args.apply or not SHA.fullmatch(args.expected_revision)):
        parser.error('--expected-revision requires poll --apply and a complete Git SHA')
    config = load_config(args.config)
    app = config['application']
    state = Path(config.get('state_root', str(STATE)))
    if args.command == 'check':
        print(json.dumps({'project': PROJECT['name'], 'configuration': 'valid', 'state_root': str(state)}))
        return
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    if args.command == 'status':
        print(json.dumps(read(state / app['id'] / 'status.json',
                             {'application': app['id'], 'state': 'not_checked'}), ensure_ascii=False, indent=2))
        return
    if args.command == 'retry':
        with locks([state / app['id'] / 'poll.lock']):
            adapters.recover(app, state / app['id'], retry=True)
            (state / app['id'] / 'failed.json').unlink(missing_ok=True)
        return
    result = poll_app(app, state, apply=args.apply, expected_revision=args.expected_revision)
    if (result['state'] in ('attention', 'quarantined')
            or (args.expected_revision and result.get('deployed_sha') != args.expected_revision)):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
