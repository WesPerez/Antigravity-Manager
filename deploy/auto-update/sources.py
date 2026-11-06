"""Only the current MINE tip can become an automatic deployment candidate."""
import os
from pathlib import Path
import subprocess
import time

from common import Deferred, Rejected, PROJECT, SHA, DIGEST, digest, run
import ci_evidence as evidence


import ci_router


class GitHub(evidence.GitHub):
    def file(self, item, destination, *, limit=4 * 1024**2):
        expected = item.get('digest', '')
        size = item.get('size', item.get('size_in_bytes'))
        if not DIGEST.fullmatch(expected) or not isinstance(size, int) or not 0 < size <= limit:
            raise Rejected('CI artifact has no valid bounded digest/size')
        path = item['download_path']
        if not path.startswith('repos/') or '..' in path:
            raise Rejected('Invalid GitHub asset path')
        args = ['/usr/bin/gh', 'api', '--hostname', 'github.com', path]
        if not path.endswith('/zip'):
            args += ['-H', 'Accept: application/octet-stream']
        try:
            with Path(destination).open('xb') as output:
                child = subprocess.Popen(args, stdout=output, stderr=subprocess.DEVNULL,
                                         env={**os.environ, 'GH_PROMPT_DISABLED': '1'})
                deadline = time.monotonic() + 120
                try:
                    while child.poll() is None:
                        if time.monotonic() > deadline or output.tell() > limit:
                            raise Deferred('CI artifact download exceeded its budget')
                        time.sleep(0.2)
                    if child.returncode:
                        raise Deferred('CI artifact download failed')
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
            if Path(destination).stat().st_size != size or 'sha256:' + digest(destination) != expected:
                raise Rejected('CI artifact digest or size differs')
        except BaseException:
            Path(destination).unlink(missing_ok=True)
            raise


class Source:
    def __init__(self, app, root, github=None):
        self.app, self.root = app, Path(root)
        self.repo = app['repository']
        self.github = github or GitHub()
        self.cache = self.root / 'source.git'

    def git(self, *args):
        return run(['git', '--git-dir=' + str(self.cache), *args], timeout=120).strip()

    def sync(self):
        if not self.cache.exists():
            run(['git', 'init', '--bare', self.cache])
            self.git('remote', 'add', 'origin', 'https://github.com/' + self.repo + '.git')
        elif self.git('remote', 'get-url', 'origin') != 'https://github.com/' + self.repo + '.git':
            raise Rejected('Source cache belongs to another repository')
        ref = 'refs/heads/' + PROJECT['branch']
        self.git('fetch', '--quiet', '--no-tags', 'origin', '+' + ref + ':' + ref)
        return self.git('rev-parse', ref)

    def routed_candidate(self, sha):
        try:
            return ci_router.candidate(self.repo, self.app['workflow'],
                                       'refs/heads/' + PROJECT.get('branch', 'main'), sha)
        except RuntimeError as error:
            raise Deferred(str(error)) from None

    def candidate(self, current):
        if not SHA.fullmatch(current or ''):
            raise Rejected('Running revision must be a complete Git SHA')
        head = self.sync()
        routed = self.routed_candidate(head)
        if routed:
            if head == current:
                return None
            if routed.get('status') != 'completed':
                raise Deferred('CI router is building or preserving this revision')
            return routed
        if head == current:
            return None
        workflow = self.app['workflow'].split('/')[-1]
        rows = self.github.api(f'repos/{self.repo}/actions/workflows/{workflow}/runs?'
                               f'branch={PROJECT["branch"]}&head_sha={head}&per_page=50')['workflow_runs']
        rows = [r for r in rows if r.get('event') in ('push', 'workflow_dispatch')
                and r.get('head_branch') == PROJECT['branch'] and r.get('head_sha') == head
                and r.get('path') == self.app['workflow']]
        if not rows:
            raise Deferred('Waiting for the exact MINE tip CI')
        # MINE can be deliberately rebuilt on a new upstream base. Ancestry is
        # not a release ordering here: only the live branch tip is authorized.
        chosen = max(rows, key=lambda r: (r['id'], r.get('run_attempt', 1)))
        if chosen.get('status') != 'completed':
            raise Deferred('MINE CI is still running')
        return chosen

    def verified(self, candidate):
        if candidate.get('router_request'):
            try:
                return ci_router.verified(self.repo, self.app['workflow'],
                    'refs/heads/' + PROJECT.get('branch', 'main'), candidate['head_sha'],
                    candidate['router_request'], self.app.get('required_jobs', PROJECT.get('required_jobs', [])))
            except RuntimeError as error:
                raise Deferred(str(error)) from None
        try:
            return evidence.verified_run(self.github, self.repo, candidate['id'], self.app['workflow'],
                                         'refs/heads/' + PROJECT['branch'], candidate['head_sha'],
                                         PROJECT['required_jobs'])
        except evidence.EvidenceError as error:
            if evidence.billing_blocked(self.github, self.repo, candidate):
                fallback = Path('/usr/local/sbin/github-ci-fallback')
                if not fallback.is_file() or not os.access(fallback, os.X_OK):
                    raise Deferred('MINE CI is billing-blocked; waiting for hosted CI') from None
                run([fallback, 'ensure', '--repo', self.repo, '--run-id', candidate['id'],
                     '--worktree', self.cache], timeout=150)
                raise Deferred('Standby CI is preparing this exact revision') from None
            if candidate.get('conclusion') != 'success':
                raise Deferred('MINE CI did not succeed; waiting for a successful build') from None
            raise Rejected('Verified CI contract is incomplete: ' + str(error)) from None

    def artifact(self, verified, destination):
        try:
            item = evidence.locate_artifact(verified, self.app['artifact'])
        except evidence.EvidenceError:
            raise Rejected('Verified artifact is missing, expired or ambiguous') from None
        self.github.file(item, destination)
        return item['digest'].removeprefix('sha256:')
