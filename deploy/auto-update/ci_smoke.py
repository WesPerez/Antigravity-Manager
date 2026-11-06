#!/usr/bin/env python3
"""Exercise the real published image and rollback only on hosted CI."""
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import adapters
from common import Deferred, Rejected, read, run


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('This Docker smoke test runs only on GitHub-hosted CI')
    sha = os.environ['GITHUB_SHA']
    reference = os.environ['IMAGE_NAME'] + '@' + os.environ['IMAGE_DIGEST']
    obj = adapters.inspect(reference, image=True)
    app = json.loads(Path(__file__).with_name('config.example.json').read_text())['application']
    root = Path(tempfile.mkdtemp(prefix='antigravity-smoke-'))
    network = 'antigravity-smoke'
    proxy = 'antigravity-smoke-proxy'
    app.update(root=str(root / 'data'), env_file=str(root / 'runtime.env'),
               networks={'bridge': [], network: ['antigravity-manager']},
               locks=[str(root / 'deploy.lock')],
               web={'url': 'http://127.0.0.1:18045/antimanager/', 'base_path': '/antimanager/'})
    Path(app['root']).mkdir()
    Path(app['env_file']).write_text('API_KEY=synthetic-api-key\nWEB_PASSWORD=synthetic-web-password\nABV_MAX_BODY_SIZE=104857600\n')
    Path(app['env_file']).chmod(0o600)
    state = root / 'state'
    state.mkdir()
    prepared = {'path': reference, 'image_id': obj['Id'], 'sha256': os.environ['IMAGE_DIGEST']}
    verified = {'source': {'sha': sha}, 'builder': {'run_id': os.environ['GITHUB_RUN_ID']}}
    repository = Path(__file__).resolve().parents[2]
    snippet = (repository / 'docker/nginx-subpath.conf').read_text().replace('127.0.0.1:8045', 'antigravity-manager:8045')
    (root / 'nginx.conf').write_text('server { listen 80; server_name _;\n' + snippet + '\n}\n')
    try:
        run(['docker', 'network', 'create', network])
        old_id = adapters.create(app, prepared, {'token': 'initial-smoke'})
        adapters.connect(old_id, app['networks'])
        run(['docker', 'update', '--restart', app['restart'], old_id])
        run(['docker', 'start', old_id])
        run(['docker', 'run', '-d', '--name', proxy, '--network', network,
             '-p', '127.0.0.1:18045:80', '--mount', f'type=bind,src={root / "nginx.conf"},dst=/etc/nginx/conf.d/default.conf,readonly',
             'nginx:stable-alpine'], timeout=180)
        print('smoke: accept fresh account-free management UI', flush=True)
        adapters.probe(app, attempts=60)
        # Store a real sentinel alongside the app's SQLite stores. No account or
        # upstream API requests are necessary to prove data survival.
        import sqlite3
        with sqlite3.connect(Path(app['root']) / 'sentinel.db') as db:
            db.execute('CREATE TABLE sentinel (value TEXT)')
            db.execute('INSERT INTO sentinel VALUES (?)', ('preserved',))
        initial_probe = adapters.probe
        def after_start(*args, **kwargs):
            # nginx resolves an upstream at worker start; refresh its synthetic
            # upstream IP after each replacement, just as Docker DNS clients do.
            run(['docker', 'exec', proxy, 'nginx', '-s', 'reload'])
            return initial_probe(*args, **kwargs)
        print('smoke: replace container and verify networks/data', flush=True)
        with patch.object(adapters, 'probe', side_effect=after_start):
            accepted = adapters.deploy(app, prepared, verified, None, state, sha)
        assert accepted['state'] == 'success'
        new_id = adapters.inspect(app['container'])['Id']
        assert new_id != old_id
        assert adapters.inspect(old_id)['State']['Running'] is False
        assert not adapters.inspect(old_id)['NetworkSettings']['Networks']
        aliases = run(['docker', 'exec', proxy, 'getent', 'hosts', app['container']]).splitlines()
        ip = adapters.inspect(new_id)['NetworkSettings']['Networks'][network]['IPAddress']
        assert {line.split()[0] for line in aliases} == {ip}
        with sqlite3.connect(Path(accepted['recovery']) / 'sentinel.db') as db:
            assert db.execute('SELECT value FROM sentinel').fetchone() == ('preserved',)
        calls = 0
        def fail_candidate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:  # preflight passed, candidate acceptance fails
                raise Deferred('Synthetic candidate rejection')
            return after_start(*args, **kwargs)
        print('smoke: reject candidate and restore the exact predecessor', flush=True)
        try:
            with patch.object(adapters, 'probe', side_effect=fail_candidate):
                adapters.deploy(app, prepared, verified, None, state, sha)
        except Rejected as error:
            assert 'previous container restored' in str(error)
        else:
            raise AssertionError('Synthetic unhealthy candidate unexpectedly passed')
        assert adapters.inspect(app['container'])['Id'] == new_id
        assert read(state / 'transaction.json')['state'] == 'rolled_back'
        with sqlite3.connect(Path(app['root']) / 'sentinel.db') as db:
            assert db.execute('SELECT value FROM sentinel').fetchone() == ('preserved',)
        adapters.contract(app, adapters.inspect(new_id))
        print('Published-image health, auth, UI subpath/assets, two networks, SQLite snapshot and exact-container rollback passed')
    finally:
        ids = run(['docker', 'ps', '-aq', '--filter', 'name=antigravity-manager']).splitlines()
        if ids:
            run(['docker', 'rm', '-f', *ids])
        for command in (['docker', 'rm', '-f', proxy], ['docker', 'network', 'rm', network]):
            try:
                run(command)
            except Deferred:
                pass
        shutil.rmtree(root)


if __name__ == '__main__':
    main()
