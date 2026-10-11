"""A single writer, exact image digests, and durable container rollback."""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
from urllib.parse import urljoin, urlsplit

from common import Deferred, Rejected, SHA, DIGEST, digest, locks, now, read, run, write_json
from sources import evidence

WATCHTOWER = 'com.centurylinklabs.watchtower.enable'
OWNER = 'io.antigravity.auto-update.transaction'


def inspect(name, *, image=False):
    return json.loads(run(['docker', *(['image'] if image else []), 'inspect', name]))[0]


def environment(app):
    path = Path(app['env_file'])
    if (not path.is_file() or path.is_symlink() or path.resolve() != path
            or path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077):
        raise Rejected('Runtime environment must be a deployment-owned 0600 file')
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch('[A-Z_][A-Z_0-9]*', key) or key in result or '\x00' in value:
            raise Rejected('Runtime environment has an invalid or duplicate key')
        result[key] = value
    if not (result.get('ABV_WEB_PASSWORD') or result.get('WEB_PASSWORD')):
        raise Rejected('A nonempty Web password is required for authenticated acceptance')
    return result


def current(app):
    obj = inspect(app['container'])
    labels = inspect(obj['Image'], image=True)['Config'].get('Labels') or {}
    sha = labels.get('org.opencontainers.image.revision', '')
    if not SHA.fullmatch(sha):
        raise Deferred('Running image has no complete source revision')
    return sha


def contract(app, obj):
    """Reject drift instead of silently dropping runtime settings on recreation."""
    host, config = obj['HostConfig'], obj['Config']
    mounts = sorted((m['Type'], m['Source'], m['Destination'], m['RW']) for m in obj['Mounts'])
    expected_mounts = [('bind', app['root'], app['data_target'], True)]
    expected_ports = {str(app['container_port']) + '/tcp':
                      [{'HostIp': app['host_ip'], 'HostPort': str(app['host_port'])}]}
    networks = obj['NetworkSettings']['Networks']
    if (mounts != expected_mounts or host['PortBindings'] != expected_ports
            or set(networks) != set(app['networks']) or host['RestartPolicy']['Name'] != app['restart']
            or host['LogConfig'] != {'Type': app['logging']['driver'], 'Config': app['logging']['options']}):
        raise Rejected('Container mounts, ports, networks, restart or logging drifted from host config')
    for name, aliases in app['networks'].items():
        net = networks[name]
        # Docker may also add the current container name and short ID.
        actual = set(net.get('Aliases') or []) - {obj['Id'][:12], app['container']}
        expected = set(aliases) - {app['container']}
        if (actual != expected or not set(aliases).issubset(set(net.get('Aliases') or []) | {app['container']})
                or net.get('IPAMConfig') or net.get('Links') or net.get('DriverOpts')):
            raise Rejected('Container network aliases or addressing drifted')
    unsupported = ('Privileged', 'ReadonlyRootfs', 'CapAdd', 'CapDrop', 'SecurityOpt', 'Devices',
                   'DeviceRequests', 'Ulimits', 'Dns', 'DnsOptions', 'DnsSearch', 'ExtraHosts', 'Tmpfs',
                   'Memory', 'MemoryReservation', 'MemorySwap', 'NanoCpus', 'CpuQuota', 'CpuPeriod',
                   'CpuShares', 'CpusetCpus', 'CpusetMems', 'PidsLimit', 'PidMode', 'UsernsMode',
                   'Init', 'AutoRemove', 'Links', 'VolumesFrom', 'GroupAdd', 'Sysctls')
    if (any(host.get(key) for key in unsupported) or host.get('ShmSize') != 64 * 1024**2
            or host.get('IpcMode') != 'private' or host.get('CgroupnsMode') != 'private'
            or host.get('Runtime') not in (None, '', 'runc') or config.get('Tty') or config.get('OpenStdin')):
        raise Rejected('Container has runtime overrides this simple updater does not support')
    image_config = inspect(obj['Image'], image=True)['Config']
    for key in ('Entrypoint', 'Cmd', 'WorkingDir', 'User', 'Healthcheck', 'StopSignal'):
        if (config.get(key) or None) != (image_config.get(key) or None):
            raise Rejected('Container overrides an image default; declare support before deployment')
    live = dict(item.split('=', 1) for item in config.get('Env', []))
    defaults = dict(item.split('=', 1) for item in image_config.get('Env', []))
    if live != {**defaults, **environment(app)}:
        raise Rejected('Runtime environment differs from the private environment file')


def request(url, *, headers=(), resolve=None, limit=16 * 1024**2):
    args = ['curl', '--silent', '--show-error', '--connect-timeout', '3', '--max-time', '10',
            '--max-filesize', str(limit), '--noproxy', '*', '--write-out', '\n%{http_code}\t%{content_type}']
    if resolve:
        args += ['--resolve', resolve]
    if headers:
        args += ['--header', '@-']
    # Secrets go through stdin, never process arguments or exception output.
    output = run([*args, url], input='\n'.join(headers) + '\n' if headers else None, timeout=15)
    body, _, status = output.rpartition('\n')
    code, _, content_type = status.partition('\t')
    return int(code), content_type, body


def probe_once(app, *, expected_accounts=0):
    obj = inspect(app['container'])
    if not obj['State']['Running'] or obj['State'].get('Health', {}).get('Status') != 'healthy':
        raise Deferred('Container health is not ready')
    base = app['health']['url'].removesuffix('/health')
    code, mime, body = request(app['health']['url'])
    if code != 200 or 'json' not in mime or json.loads(body).get('status') != 'ok':
        raise Deferred('Backend health failed')
    env = environment(app)
    password = env.get('ABV_WEB_PASSWORD') or env['WEB_PASSWORD']
    headers = ['Authorization: Bearer ' + password, 'x-api-key: ' + password]
    code, mime, body = request(base + '/api/proxy/status', headers=headers)
    value = json.loads(body) if code == 200 and 'json' in mime else {}
    count = value.get('active_accounts')
    if (value.get('running') is not True or value.get('port') != app['container_port']
            or type(count) is not int or count < max(app.get('min_accounts', 0), expected_accounts)):
        raise Deferred('Authenticated account loading failed')
    code, mime, body = request(base + '/api/accounts', headers=headers)
    if code != 200 or 'json' not in mime or not isinstance(json.loads(body).get('accounts'), list):
        raise Deferred('Web login/account read failed')
    code, _, _ = request(base + '/api/proxy/status')
    if code != 401:
        raise Deferred('Management authentication is not enforced')
    web = app['web']
    code, mime, page = request(web['url'], resolve=web.get('resolve'))
    if code != 200 or 'text/html' not in mime:
        raise Deferred('Web entry page failed')
    if web['base_path'] != '/' and '<base href="' + web['base_path'] + '">' not in page:
        raise Deferred('Reverse-proxy base path is missing')
    assets = re.findall(r'<script\b[^>]*\bsrc="([^"]+\.js)"', page)
    if not assets:
        raise Deferred('Web entry has no JavaScript bundle')
    local = run(['docker', 'exec', obj['Id'], 'cat', '/app/dist/index.html'])
    for asset in assets:
        if asset not in local:
            raise Deferred('Proxy serves a different frontend revision')
        target = urljoin(web['url'], asset)
        if urlsplit(target).netloc != urlsplit(web['url']).netloc:
            raise Deferred('Frontend asset escaped the configured web origin')
        code, mime, body = request(target, resolve=web.get('resolve'))
        if code != 200 or 'javascript' not in mime or len(body) < 1024:
            raise Deferred('Frontend asset returned a fallback or invalid response')
    code, mime, _ = request(urljoin(web['url'], 'accounts'), resolve=web.get('resolve'))
    if code != 200 or 'text/html' not in mime:
        raise Deferred('Web deep-link refresh failed')
    code, mime, body = request(urljoin(web['url'], 'api/accounts'), headers=headers, resolve=web.get('resolve'))
    if code != 200 or 'json' not in mime or not isinstance(json.loads(body).get('accounts'), list):
        raise Deferred('Web subpath login/account read failed')
    code, _, _ = request(urljoin(web['url'], 'api/proxy/status'), resolve=web.get('resolve'))
    if code != 401:
        raise Deferred('Web subpath management authentication is not enforced')
    return count


def probe(app, *, attempts=1, expected_accounts=0):
    for attempt in range(attempts):
        try:
            return probe_once(app, expected_accounts=expected_accounts)
        except (Deferred, ValueError, KeyError):
            if attempt + 1 == attempts:
                raise Deferred('Application acceptance failed') from None
            time.sleep(2)


def preflight(app, expected):
    if current(app) != expected:
        raise Deferred('Production changed before deployment')
    obj = inspect(app['container'])
    if ((obj['Config'].get('Labels') or {}).get(WATCHTOWER) == 'true'
            and run(['docker', 'ps', '-q', '--filter', 'label=com.centurylinklabs.watchtower=true']).strip()):
        raise Deferred('Stop the legacy Watchtower before handing over this container')
    contract(app, obj)
    return probe(app)


def prepare(app, source, verified, work):
    try:
        item = evidence.locate_artifact(verified, 'image-metadata')
        data = evidence.checked_download(source.github, item['download_path'], item['digest'],
                                         item.get('size', item.get('size_in_bytes')))
        metadata = evidence.unpack(data, 'image-metadata.json')
    except evidence.EvidenceError as error:
        raise Rejected('Image metadata verification failed: ' + str(error)) from None
    sha = verified['source']['sha']
    if (metadata.get('repository') != app['repository'] or metadata.get('revision') != sha
            or metadata.get('image') != app['image'] or not DIGEST.fullmatch(metadata.get('digest', ''))):
        raise Rejected('Image metadata differs from verified CI')
    reference = app['image'] + '@' + metadata['digest']
    run(['docker', 'pull', reference], timeout=300)
    obj = inspect(reference, image=True)
    labels = obj['Config'].get('Labels') or {}
    if (reference not in obj.get('RepoDigests', []) or labels.get('org.opencontainers.image.revision') != sha
            or labels.get('org.opencontainers.image.source') != 'https://github.com/' + app['repository']):
        raise Rejected('Pulled image identity differs from CI')
    return {'path': reference, 'sha256': metadata['digest'], 'image_id': obj['Id']}


def snapshot(app, destination):
    """Old writer is stopped; SQLite backup API includes any remaining WAL."""
    destination.mkdir(mode=0o700)
    write_json(destination / 'owner.json', {'project': 'antigravity-manager'})
    root = Path(app['root'])
    paths = sorted(root.glob('*.db')) + sorted(root.glob('*.json')) + sorted((root / 'accounts').glob('*.json'))
    if shutil.disk_usage(destination).free < sum(p.stat().st_size for p in paths) * 2 + 512 * 1024**2:
        raise Deferred('Not enough space for a consistent recovery copy')
    inventory = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise Rejected('Recovery input is not a regular managed file')
        target = destination / path.relative_to(root)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.suffix == '.db':
            deadline = time.monotonic() + 90
            def progress(*_):
                if time.monotonic() > deadline:
                    raise Deferred('SQLite recovery copy exceeded its time budget')
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=15) as original:
                with sqlite3.connect(target) as copy:
                    original.backup(copy, pages=256, sleep=0.05, progress=progress)
                    copy.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                    if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise Rejected('SQLite recovery copy failed integrity validation')
        else:
            shutil.copy2(path, target)
        target.chmod(0o600)
        inventory.append({'file': str(path.relative_to(root)), 'sha256': digest(target)})
    shutil.copy2(app['env_file'], destination / 'runtime.env')
    (destination / 'runtime.env').chmod(0o600)
    write_json(destination / 'inventory.json', inventory)


def disconnect(obj):
    for network in obj['NetworkSettings']['Networks']:
        run(['docker', 'network', 'disconnect', network, obj['Id']])


def connect(container_id, networks):
    attached = inspect(container_id)['NetworkSettings']['Networks']
    for network, aliases in networks.items():
        if network in attached:
            continue
        args = ['docker', 'network', 'connect']
        for alias in aliases:
            args += ['--alias', alias]
        run([*args, network, container_id])


def create(app, prepared, record):
    primary = next(iter(app['networks']))
    args = ['docker', 'create', '--name', app['container'], '--pull', 'never',
            '--restart', 'no', '--env-file', app['env_file'],
            '--mount', f'type=bind,src={app["root"]},dst={app["data_target"]}',
            '--publish', f'{app["host_ip"]}:{app["host_port"]}:{app["container_port"]}',
            '--network', primary, '--label', WATCHTOWER + '=false', '--label', OWNER + '=' + record['token'],
            '--log-driver', app['logging']['driver']]
    for key, value in app['logging']['options'].items():
        args += ['--log-opt', key + '=' + value]
    for alias in app['networks'][primary]:
        args += ['--network-alias', alias]
    new_id = run([*args, prepared['path']]).strip()
    if not re.fullmatch('[a-f0-9]{64}', new_id):
        raise Rejected('Docker did not return a complete container ID')
    return new_id


def named_container(name):
    ids = run(['docker', 'ps', '-aq', '--no-trunc', '--filter', 'name=^/' + name + '$']).splitlines()
    if len(ids) > 1:
        raise Rejected('Ambiguous container identity')
    return inspect(ids[0]) if ids else None


def rollback(app, record):
    old = inspect(record['old_id'])
    if old['Image'] != record['old_image_id']:
        raise Rejected('Previous container image changed')
    live = named_container(app['container'])
    if live and live['Id'] != old['Id']:
        if (live['Config'].get('Labels') or {}).get(OWNER) != record['token']:
            raise Rejected('Another deployment owns the current container')
        if live['State']['Running']:
            run(['docker', 'stop', '--time', '60', live['Id']], timeout=90)
        run(['docker', 'rm', live['Id']])
    if old['Name'] != '/' + app['container']:
        run(['docker', 'rename', old['Id'], app['container']])
    connect(old['Id'], record['old_networks'])
    run(['docker', 'update', '--restart', record['old_restart'], old['Id']])
    run(['docker', 'start', old['Id']])
    probe(app, attempts=60, expected_accounts=record['expected_accounts'])
    if current(app) != record['previous_sha']:
        raise Rejected('Rollback did not restore the prior image revision')


def finalize(app, directory, record):
    write_json(directory / 'deployed.json', record)
    # Retain exactly one stopped predecessor. Its image and credentials remain
    # protected even if a separate image-retention job runs immediately after us.
    try:
        previous = record.get('retire', {})
        if previous and previous.get('old_id') != record['old_id']:
            older = named_container(previous['old_name'])
            if older:
                if older['Id'] != previous['old_id'] or older['State']['Running']:
                    raise Deferred('Prior recovery container is still referenced')
                run(['docker', 'rm', older['Id']])
        keep = Path(record['recovery'])
        for path in directory.glob('recovery-*'):
            if (path != keep and path.is_dir() and not path.is_symlink()
                    and read(path / 'owner.json', {}).get('project') == 'antigravity-manager'):
                shutil.rmtree(path)
        (directory / 'retention-pending.json').unlink(missing_ok=True)
    except (OSError, Deferred, KeyError) as error:
        write_json(directory / 'retention-pending.json', {'reason': type(error).__name__})


def deploy(app, prepared, verified, source, directory, expected):
    with locks(app['locks']):
        accounts = preflight(app, expected)
        if source is not None and source.sync() != verified['source']['sha']:
            raise Deferred('MINE advanced before the container switch')
        old = inspect(app['container'])
        previous = read(directory / 'deployed.json', {})
        token = uuid.uuid4().hex
        record = {'state': 'stopping', 'token': token, 'source_sha': verified['source']['sha'],
                  'previous_sha': expected, 'source': verified['source'], 'builder': verified['builder'],
                  'old_id': old['Id'], 'old_image_id': old['Image'], 'new_image_id': prepared['image_id'],
                  'reference': prepared['path'], 'old_restart': old['HostConfig']['RestartPolicy']['Name'],
                  'old_networks': {n: net.get('Aliases') or [] for n, net in old['NetworkSettings']['Networks'].items()},
                  'old_name': app['container'] + '-previous-' + token[:12], 'expected_accounts': min(accounts, 1),
                  'recovery': str(directory / ('recovery-' + token)), 'started_at': now()}
        if previous:
            record['retire'] = {key: previous[key] for key in ('old_id', 'old_name')}
        write_json(directory / 'transaction.json', record)
        try:
            # Neither container may restart during a crash until recovery has
            # decided which one owns the shared databases and production port.
            run(['docker', 'update', '--restart', 'no', old['Id']])
            run(['docker', 'stop', '--time', '60', old['Id']], timeout=90)
            snapshot(app, Path(record['recovery']))
            record['state'] = 'switching'
            write_json(directory / 'transaction.json', record)
            disconnect(inspect(old['Id']))
            run(['docker', 'rename', old['Id'], record['old_name']])
            new_id = create(app, prepared, record)
            record['new_id'] = new_id
            write_json(directory / 'transaction.json', record)
            connect(new_id, app['networks'])
            run(['docker', 'start', new_id])
            probe(app, attempts=60, expected_accounts=record['expected_accounts'])
            run(['docker', 'update', '--restart', app['restart'], new_id])
            contract(app, inspect(new_id))
            if current(app) != record['source_sha'] or inspect(new_id)['Image'] != prepared['image_id']:
                raise Rejected('Accepted container differs from verified image')
            for _ in range(2):
                time.sleep(2)
                probe(app, expected_accounts=record['expected_accounts'])
            record.update(state='success', completed_at=now())
            write_json(directory / 'transaction.json', record)
        except BaseException:
            try:
                rollback(app, record)
                record['state'] = 'rolled_back'
            except BaseException:
                record['state'] = 'rollback_failed'
                write_json(directory / 'transaction.json', record)
                raise Rejected('Deployment and rollback failed; recovery state retained') from None
            write_json(directory / 'transaction.json', record)
            raise Rejected('Deployment failed; previous container restored, business data preserved') from None
        finalize(app, directory, record)
        return record


def recover(app, directory, *, retry=False):
    with locks(app['locks']):
        record = read(directory / 'transaction.json', {})
        if record.get('state') == 'success':
            finalize(app, directory, record)
            return 'success'
        if record.get('state') not in ('stopping', 'switching') and not (retry and record.get('state') == 'rollback_failed'):
            return record.get('state')
        live = named_container(app['container'])
        if live and (live['Config'].get('Labels') or {}).get(OWNER) == record['token']:
            try:
                # A crash after starting an acceptable new container must not
                # kill it just because the acceptance record was not written.
                probe(app, expected_accounts=record['expected_accounts'])
                if live['Image'] != record['new_image_id'] or current(app) != record['source_sha']:
                    raise Rejected('Interrupted container image identity differs')
                run(['docker', 'update', '--restart', app['restart'], live['Id']])
                contract(app, inspect(live['Id']))
                record.update(state='success', completed_at=now())
                write_json(directory / 'transaction.json', record)
                finalize(app, directory, record)
                return 'success'
            except (Deferred, Rejected):
                pass
        try:
            rollback(app, record)
            record['state'] = 'rolled_back'
        except BaseException:
            record['state'] = 'rollback_failed'
            write_json(directory / 'transaction.json', record)
            raise Rejected('Interrupted deployment recovery failed') from None
        write_json(directory / 'transaction.json', record)
        write_json(directory / 'failed.json', {'sha': record['source_sha'], 'reason': 'Interrupted deployment rolled back', 'at': now()})
        return record['state']
