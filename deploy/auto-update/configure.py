#!/usr/bin/env python3
"""Capture an existing deployment into private, portable host configuration."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import adapters
from common import CONFIG, atomic, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--web-url', required=True, help='Existing working web URL, including trailing slash')
    parser.add_argument('--resolve', help='Optional curl DNS override, e.g. example.com:443:127.0.0.1')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Root is required')
    config = json.loads(Path(__file__).with_name('config.example.json').read_text())
    app = config['application']
    env_path = Path(app['env_file'])
    if CONFIG.exists() or env_path.exists():
        raise SystemExit('Host configuration exists; update it explicitly instead of overwriting it')
    url = urlsplit(args.web_url)
    if url.scheme not in ('https', 'http') or not url.netloc or not url.path.endswith('/') or url.username or url.password:
        raise SystemExit('Expected a web URL ending in / without credentials')
    obj = adapters.inspect(app['container'])
    image = adapters.inspect(obj['Image'], image=True)
    live = dict(value.split('=', 1) for value in obj['Config'].get('Env', []))
    defaults = dict(value.split('=', 1) for value in image['Config'].get('Env', []))
    runtime = {key: value for key, value in live.items() if defaults.get(key) != value}
    if any('\n' in value or '\r' in value or '\x00' in value for value in runtime.values()):
        raise SystemExit('Docker env-file cannot preserve a multiline runtime value')
    mounts = [m for m in obj['Mounts'] if m['Destination'] == app['data_target'] and m['Type'] == 'bind' and m['RW']]
    if len(mounts) != 1:
        raise SystemExit('Expected one existing writable data bind')
    app['root'] = mounts[0]['Source']
    ports = obj['HostConfig']['PortBindings'].get(str(app['container_port']) + '/tcp', [])
    if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
        raise SystemExit('Expected one existing loopback port binding')
    app['host_port'] = int(ports[0]['HostPort'])
    app['health']['url'] = f'http://127.0.0.1:{app["host_port"]}/health'
    app['networks'] = {name: data.get('Aliases') or [] for name, data in obj['NetworkSettings']['Networks'].items()}
    app['restart'] = obj['HostConfig']['RestartPolicy']['Name']
    app['logging'] = {'driver': obj['HostConfig']['LogConfig']['Type'], 'options': obj['HostConfig']['LogConfig']['Config']}
    app['web'] = {'url': args.web_url, 'base_path': url.path}
    if args.resolve:
        app['web']['resolve'] = args.resolve
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic(env_path, ''.join(key + '=' + value + '\n' for key, value in sorted(runtime.items())).encode())
    try:
        adapters.contract(app, obj)
        adapters.probe(app)
        write_json(CONFIG, config)
    except BaseException:
        env_path.unlink()
        raise
    print(json.dumps({'configuration': str(CONFIG), 'credentials': 'captured privately', 'runtime': 'verified'}))


if __name__ == '__main__':
    main()
