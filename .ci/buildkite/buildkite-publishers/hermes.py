#!/usr/bin/env python3
"""Read-only Hermes release artifact consumer. Never installs or publishes code."""
import argparse
import datetime as dt
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

REPO = 'aditzel/hermes-muninndb-plugin'
PIPELINE = 'hermes-muninndb-release-build'
MANUAL_PIPELINE = 'hermes-muninndb-manual-build'
PREFIX = '/v2/organizations/mirai-forge/pipelines/' + PIPELINE + '/builds'
MANUAL_PREFIX = '/v2/organizations/mirai-forge/pipelines/' + MANUAL_PIPELINE + '/builds'
REPO_PREFIX = '/repos/' + REPO
SOURCE_KEY = 'janus-source-v1'
INDEX = 'janus-artifact-index.json'
BUNDLE = 'python-package-distributions.zip'
MANIFEST = 'janus-release-manifest.json'
MAX_JSON = 4 * 1024 * 1024
MAX_PACKAGE = 64 * 1024 * 1024
ORIGINS = {'github': 'https://api.github.com', 'buildkite': 'https://api.buildkite.com'}
VERSION = r'([A-Za-z0-9_.!+]+)'
WHEEL = re.compile(r'hermes_muninndb_plugin-' + VERSION + r'-(?:[0-9][A-Za-z0-9_]*-)?[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+\.whl')
SDIST = re.compile(r'hermes[_-]muninndb[_-]plugin-' + VERSION + r'\.tar\.gz')


class Rejected(Exception):
    """Fixed diagnostics only: provider bodies/URLs may contain credentials."""


class Pending(Rejected):
    """No verified terminal build yet; a bounded caller may retry this read."""


def require(condition, message):
    if not condition:
        raise Rejected(message)


def digest(value, length=64):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(length) + '}', value), 'Invalid digest')
    return value


def identifier(value):
    require(isinstance(value, str), 'Invalid provider UUID')
    try:
        require(str(uuid.UUID(value)) == value, 'Noncanonical provider UUID')
    except ValueError:
        raise Rejected('Invalid provider UUID') from None
    return value


def positive(value):
    return type(value) is int and 0 < value < 2**63


def utc(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z', value), 'Invalid timestamp')
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise Rejected('Invalid timestamp') from None


def decode(raw, limit=MAX_JSON):
    require(isinstance(raw, (str, bytes)) and len(raw) <= limit, 'JSON exceeds bound')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise Rejected('Invalid JSON') from None


def same(left, right):
    return json.dumps(left, sort_keys=True, separators=(',', ':')) == json.dumps(right, sort_keys=True, separators=(',', ':'))


def release_identity(value):
    require(isinstance(value, dict) and positive(value.get('id')), 'Invalid release ID')
    tag = value.get('tag_name')
    require(isinstance(tag, str) and 0 < len(tag) <= 1024 and not tag.startswith(('-', '/'))
            and not tag.endswith(('/', '.')) and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '~^:?*[\\' for c in tag)
            and '..' not in tag and '@{' not in tag
            and all(part and not part.startswith('.') and not part.endswith('.lock') for part in tag.split('/')), 'Invalid release tag')
    require(value.get('draft') is False and type(value.get('prerelease')) is bool, 'Release is not published')
    utc(value.get('published_at'))
    return {'id': value['id'], 'tag': tag, 'publishedAt': value['published_at'], 'prerelease': value['prerelease']}


def expected_release(event, commit, ref):
    require(isinstance(event, dict) and event.get('action') == 'published'
            and isinstance(event.get('repository'), dict) and event['repository'].get('full_name') == REPO,
            'Expected the Hermes published release event')
    release = release_identity(event.get('release'))
    require(ref == 'refs/tags/' + release['tag'], 'GitHub ref differs from release')
    return {'release': release, 'commit': digest(commit, 40)}


def policy(value):
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'repository', 'pipeline', 'producerPolicySha256'}
            and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and value['repository'] == REPO and value['pipeline'] == PIPELINE, 'Unapproved consumer policy')
    digest(value['producerPolicySha256'])
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


class API:
    def __init__(self, github_token, buildkite_token, *, opener=None, clock=time.monotonic, pipeline=PIPELINE):
        require(all(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_.-]{16,4096}', token)
                    for token in (github_token, buildkite_token)), 'Invalid API credential encoding')
        require(pipeline in (PIPELINE, MANUAL_PIPELINE), 'Unapproved artifact pipeline')
        self.prefix = PREFIX if pipeline == PIPELINE else MANUAL_PREFIX
        self.tokens = {'github': github_token, 'buildkite': buildkite_token}
        self.clock, self.deadline, self.requests = clock, clock() + 150, 0
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def remaining(self):
        left = self.deadline - self.clock()
        require(left > 0 and self.requests < 80, 'Consumer request/deadline budget exhausted')
        return min(30, left)

    def request(self, url, headers, limit, redirect=False):
        timeout = self.remaining()
        self.requests += 1
        response = None
        try:
            try:
                response = self.opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            require(response.geturl() == url, 'Unexpected implicit redirect')
            if redirect:
                require(response.code == 302, 'Expected artifact download redirect')
                location = response.headers.get('Location')
                require(isinstance(location, str) and len(location) <= 8192, 'Missing artifact location')
                return location
            require(response.code == 200, 'Provider request failed')
            chunks, size = [], 0
            while True:
                self.remaining()
                chunk = response.read1(min(1024 * 1024, limit + 1 - size))
                self.remaining()
                if not chunk:
                    break
                size += len(chunk)
                require(size <= limit, 'Provider response exceeds bound')
                chunks.append(chunk)
            return b''.join(chunks)
        except (OSError, http.client.HTTPException, ValueError):
            raise Rejected('Provider transport failed') from None
        finally:
            if response is not None:
                response.close()

    def api(self, service, path, *, redirect=False):
        require(service in ORIGINS and isinstance(path, str), 'Unknown provider')
        parsed = urllib.parse.urlsplit(path)
        require(not parsed.scheme and not parsed.netloc and not parsed.fragment
                and path.startswith(self.prefix if service == 'buildkite' else REPO_PREFIX + '/'), 'API path outside scope')
        if service == 'buildkite':
            require(re.fullmatch(re.escape(self.prefix) + r'(?:/[1-9][0-9]*(?:/artifacts|/jobs/[0-9a-f-]{36}/artifacts/[0-9a-f-]{36}/download)?)?', parsed.path), 'Buildkite path outside scope')
        else:
            require(re.fullmatch(re.escape(REPO_PREFIX) + r'/(?:releases/[1-9][0-9]*|git/ref/tags/[A-Za-z0-9_.%!-]+|git/tags/[0-9a-f]{40})', parsed.path), 'GitHub path outside scope')
        headers = {'Authorization': 'Bearer ' + self.tokens[service], 'Accept': 'application/json', 'User-Agent': 'janus-release-consumer'}
        if service == 'github':
            headers['X-GitHub-Api-Version'] = '2026-03-10'
        raw = self.request(ORIGINS[service] + path, headers, MAX_JSON, redirect)
        return raw if redirect else decode(raw)

    def download(self, path, size):
        location = self.api('buildkite', path, redirect=True)
        target = urllib.parse.urlsplit(location)
        require(target.scheme == 'https' and target.netloc == 'buildkiteartifacts.com'
                and target.path.startswith('/artifacts/') and not target.fragment
                and all(32 < ord(c) < 127 for c in location), 'Artifact redirect outside approved storage')
        # New request, no API Authorization header, no proxy or redirect follow.
        return self.request(location, {'Accept': 'application/octet-stream'}, size)

    def pages(self, path, parameters=None):
        rows = []
        for page in range(1, 11):
            query = urllib.parse.urlencode({**(parameters or {}), 'per_page': 100, 'page': page})
            batch = self.api('buildkite', path + '?' + query)
            require(isinstance(batch, list) and len(batch) <= 100, 'Invalid provider page')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise Rejected('Provider pagination bound exhausted')


def check_release(api, expected):
    current = api.api('github', REPO_PREFIX + '/releases/' + str(expected['release']['id']))
    require(release_identity(current) == expected['release'], 'GitHub release changed')
    ref = api.api('github', REPO_PREFIX + '/git/ref/tags/' + urllib.parse.quote(expected['release']['tag'], safe=''))
    require(isinstance(ref, dict) and ref.get('ref') == 'refs/tags/' + expected['release']['tag'], 'GitHub tag identity changed')
    obj, seen = ref.get('object'), set()
    for _ in range(10):
        require(isinstance(obj, dict), 'Missing GitHub tag object')
        sha = digest(obj.get('sha'), 40)
        if obj.get('type') == 'commit':
            require(sha == expected['commit'], 'Release tag moved')
            return
        require(obj.get('type') == 'tag' and sha not in seen, 'Invalid annotated tag chain')
        seen.add(sha)
        tag = api.api('github', REPO_PREFIX + '/git/tags/' + sha)
        require(isinstance(tag, dict) and tag.get('sha') == sha, 'Annotated tag identity mismatch')
        obj = tag.get('object')
    raise Rejected('Annotated tag chain exceeds bound')


def frozen_source(source, build, expected, approved):
    fields = {'schemaVersion', 'pipelineSlug', 'repository', 'buildId', 'sourceSha', 'testedSha', 'baseSha',
              'pullRequest', 'sourceBranch', 'baseBranch', 'release'}
    require(isinstance(source, dict) and set(source) == fields and type(source['schemaVersion']) is int
            and source['schemaVersion'] == 1, 'Invalid protected source schema')
    require(source['pipelineSlug'] == PIPELINE and source['repository'] == REPO
            and source['buildId'] == build['id'] and source['sourceSha'] == source['testedSha'] == expected['commit']
            and source['sourceBranch'] == build['branch'] and source['baseSha'] is None
            and source['baseBranch'] is None and source['pullRequest'] is None, 'Protected source identity mismatch')
    require(same(source['release'], {**expected['release'], 'policyDigest': approved['producerPolicySha256']}),
            'Unapproved release or producer policy')
    return source


def newest_build(api, expected, approved):
    rows = api.pages(PREFIX, {'commit': expected['commit'], 'created_from': expected['release']['publishedAt'],
                             'include_retried_jobs': 'true'})
    if not rows:
        raise Pending('No matching provider build yet')
    candidates, unfrozen, ids, numbers = [], [], set(), set()
    for build in rows:
        require(isinstance(build, dict) and positive(build.get('number')), 'Invalid build identity')
        identifier(build.get('id'))
        require(build['id'] not in ids and build['number'] not in numbers, 'Duplicate provider build')
        ids.add(build['id']); numbers.add(build['number'])
        require(build.get('commit') == expected['commit'] and utc(build.get('created_at')) >= utc(expected['release']['publishedAt']), 'Build escaped selection')
        require(isinstance(build.get('pipeline'), dict) and build['pipeline'].get('slug') == PIPELINE
                and build['pipeline'].get('repository') == 'https://github.com/' + REPO + '.git', 'Provider pipeline changed')
        require(build.get('source') == 'webhook', 'Unexpected build origin')
        metadata = build.get('meta_data')
        require(isinstance(metadata, dict), 'Invalid provider metadata')
        if SOURCE_KEY not in metadata:
            unfrozen.append(build)
            continue
        source = decode(metadata[SOURCE_KEY], 65536)
        require(isinstance(source, dict) and isinstance(source.get('release'), dict), 'Invalid build release source')
        # Another release may legitimately point at the same commit. It cannot
        # provide this release's packages, but its known distinct ID is ignored.
        if source['release'].get('id') != expected['release']['id']:
            other = source['release']
            observed = release_identity({'id': other.get('id'), 'tag_name': other.get('tag'),
                'published_at': other.get('publishedAt'), 'prerelease': other.get('prerelease'), 'draft': False})
            frozen_source(source, build, {'commit': expected['commit'], 'release': observed}, approved)
            continue
        frozen_source(source, build, expected, approved)
        candidates.append((build, source))
    newest = max((build['number'] for build, _ in candidates), default=0)
    for unknown in unfrozen:
        if unknown['number'] > newest:
            if unknown.get('state') in ('creating', 'scheduled', 'running', 'blocked'):
                raise Pending('Build source is not frozen yet')
            raise Rejected('Terminal build has no frozen source')
    if not candidates:
        raise Pending('No build for the requested release')
    build, source = max(candidates, key=lambda item: item[0]['number'])
    if build.get('state') in ('creating', 'scheduled', 'running', 'blocked'):
        raise Pending('Newest release build is not terminal yet')
    return build, completed_job(build), source


def completed_job(build):
    require(build.get('state') == 'passed' and utc(build.get('finished_at')) >= utc(build['created_at']), 'Newest release build has not passed')
    jobs = build.get('jobs')
    require(isinstance(jobs, list) and 2 <= len(jobs) <= 100, 'Unexpected release job graph')
    by_id, current = {}, {}
    for job in jobs:
        require(isinstance(job, dict) and job.get('type') == 'script' and job.get('command') in ('janus-ci bootstrap', 'janus-ci build'), 'Unexpected release job')
        key = job['command'].split()[-1]
        ident = identifier(job.get('id'))
        require(ident not in by_id and type(job.get('retried')) is bool, 'Ambiguous job attempt')
        by_id[ident] = job
        if not job['retried']:
            require(key not in current and job.get('retried_in_job_id') is None, 'Duplicate current job attempt')
            require(job.get('state') == 'passed' and type(job.get('exit_status')) is int and job['exit_status'] == 0
                    and job.get('soft_failed') is False and job.get('signal') is None, 'Release job did not pass normally')
            require(job.get('agent_query_rules') == ['queue=janus-linux'], 'Release ran outside approved queue')
            require(utc(job.get('started_at')) <= utc(job.get('finished_at')) <= utc(build['finished_at']), 'Invalid job timeline')
            current[key] = job
        else:
            require(job.get('state') in ('passed', 'failed', 'canceled', 'timed_out', 'broken')
                    and utc(job.get('finished_at')) <= utc(build['finished_at']), 'Previous attempt is not terminal')
    require(set(current) == {'bootstrap', 'build'}, 'Release job graph is incomplete')
    for ident, job in by_id.items():
        seen = set()
        while job['retried']:
            require(ident not in seen, 'Cyclic retry chain')
            seen.add(ident)
            target = job.get('retried_in_job_id')
            require(target in by_id and by_id[target]['command'] == job['command'], 'Broken retry chain')
            ident, job = target, by_id[target]
    return current['build']


def artifacts(api, build, job, prefix=PREFIX):
    require(prefix in (PREFIX, MANUAL_PREFIX), 'Unapproved artifact pipeline')
    rows = api.pages(prefix + '/' + str(build['number']) + '/artifacts')
    selected = {}
    ids = set()
    for row in rows:
        require(isinstance(row, dict), 'Invalid provider artifact')
        ident = identifier(row.get('id'))
        require(ident not in ids, 'Duplicate artifact ID')
        ids.add(ident)
        if row.get('job_id') != job['id']:
            continue
        name = row.get('path')
        require(name in (INDEX, BUNDLE) and name not in selected and row.get('state') == 'finished', 'Unexpected or duplicate current artifact')
        size = row.get('file_size')
        require(type(size) is int and 0 < size <= (65536 if name == INDEX else MAX_PACKAGE), 'Artifact size outside budget')
        digest(row.get('sha1sum'), 40)
        path = prefix + '/' + str(build['number']) + '/jobs/' + job['id'] + '/artifacts/' + ident
        require(row.get('url') == ORIGINS['buildkite'] + path and row.get('download_url') == ORIGINS['buildkite'] + path + '/download', 'Artifact URL identity mismatch')
        selected[name] = {'id': ident, 'jobId': job['id'], 'path': name, 'bytes': size, 'sha1': row['sha1sum'], 'downloadPath': path + '/download'}
    require(set(selected) == {INDEX, BUNDLE}, 'Incomplete release artifacts')
    return selected


def download(api, row):
    data = api.download(row['downloadPath'], row['bytes'])
    require(len(data) == row['bytes'] and hashlib.sha1(data).hexdigest() == row['sha1'], 'Provider artifact hash/size mismatch')
    return data


def packages(index_bytes, bundle_bytes, source, job):
    index = decode(index_bytes, 65536)
    fields = {'schemaVersion', 'source', 'jobId', 'commandResult', 'resultAtSnapshot', 'cancellationSignalAtSnapshot', 'artifacts'}
    require(isinstance(index, dict) and set(index) == fields and type(index['schemaVersion']) is int and index['schemaVersion'] == 1
            and same(index['source'], source) and index['jobId'] == job['id'], 'Artifact index source mismatch')
    require(type(index['commandResult']) is int and index['commandResult'] == 0 and type(index['resultAtSnapshot']) is int
            and index['resultAtSnapshot'] == 0 and index['cancellationSignalAtSnapshot'] is None, 'Artifact index records failure')
    require(same(index['artifacts'], [{'path': BUNDLE, 'status': 'present', 'bytes': len(bundle_bytes),
                                  'sha256': hashlib.sha256(bundle_bytes).hexdigest()}]), 'Protected bundle digest mismatch')
    require(len(bundle_bytes) <= MAX_PACKAGE, 'Bundle exceeds bound')
    try:
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as archive:
            entries = archive.infolist()
            require(len(entries) == 3 and len({entry.filename for entry in entries}) == 3, 'Duplicate or extra archive members')
            require(all(entry.compress_type == zipfile.ZIP_STORED and not entry.flag_bits & 1
                        and stat.S_ISREG(entry.external_attr >> 16) and entry.file_size == entry.compress_size
                        and entry.file_size <= (65536 if entry.filename == MANIFEST else MAX_PACKAGE)
                        for entry in entries), 'Unsafe archive encoding or member')
            names = [entry.filename for entry in entries]
            wheels = [(name, WHEEL.fullmatch(name)) for name in names if WHEEL.fullmatch(name)]
            sdists = [(name, SDIST.fullmatch(name)) for name in names if SDIST.fullmatch(name)]
            require(MANIFEST in names and len(wheels) == len(sdists) == 1
                    and wheels[0][1].group(1) == sdists[0][1].group(1), 'Unexpected distribution filenames')
            manifest = decode(archive.read(MANIFEST), 65536)
            require(isinstance(manifest, dict) and set(manifest) == {'schemaVersion', 'source', 'jobId', 'files'}
                    and type(manifest['schemaVersion']) is int and manifest['schemaVersion'] == 1
                    and same(manifest['source'], source) and manifest['jobId'] == job['id'], 'Package manifest source mismatch')
            payloads = {name: archive.read(name) for name in sorted((wheels[0][0], sdists[0][0]))}
            require(all(payloads.values()) and same(manifest['files'], [{'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                                                  for name, data in payloads.items()]), 'Package manifest hashes mismatch')
            return payloads
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise Rejected('Invalid release archive') from None


def consume(api, expected, approved, output):
    policy(approved)
    require(not output.exists() and not output.is_symlink(), 'Output must not already exist')
    parent = output.parent.lstat()
    require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid() and not parent.st_mode & 0o077,
            'Output parent must be an existing private directory owned by the caller')
    check_release(api, expected)
    build, job, source = newest_build(api, expected, approved)
    inventory = artifacts(api, build, job)
    index = download(api, inventory[INDEX])
    packed = download(api, inventory[BUNDLE])
    payloads = packages(index, packed, source, job)
    # Re-read both providers after all downloads, before exposing any package.
    check_release(api, expected)
    latest_build, latest_job, latest_source = newest_build(api, expected, approved)
    require((latest_build['id'], latest_job['id'], latest_source) == (build['id'], job['id'], source), 'Release was superseded during download')
    require(artifacts(api, latest_build, latest_job) == inventory, 'Artifact inventory changed during download')
    staging = Path(tempfile.mkdtemp(prefix='.janus-release-', dir=output.parent))
    try:
        for name, data in payloads.items():
            target = staging / name
            with target.open('xb') as stream:
                stream.write(data)
            target.chmod(0o600)
        require(not output.exists() and not output.is_symlink(), 'Output appeared during verification')
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'repository': REPO, 'releaseId': expected['release']['id'], 'commit': expected['commit'],
            'buildId': build['id'], 'buildNumber': build['number'], 'jobId': job['id'],
            'producerPolicySha256': approved['producerPolicySha256'],
            'artifacts': [{key: row[key] for key in ('id', 'jobId', 'path', 'bytes', 'sha1')} for row in inventory.values()],
            'files': [{'name': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in payloads.items()]}


def token_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077 and info.st_size <= 4096, 'Unsafe token file')
        return stream.read(4097).decode('ascii').strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('policy', 'event', 'github-token-file', 'buildkite-token-file', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--ref', required=True)
    args = parser.parse_args()
    expected = expected_release(decode(args.event.read_bytes()), args.commit, args.ref)
    approved = policy(decode(args.policy.read_bytes()))
    api = API(token_file(args.github_token_file), token_file(args.buildkite_token_file))
    result = consume(api, expected, approved, args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Pending as error:
        print(str(error), file=sys.stderr)
        sys.exit(75)
    except Rejected as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print('Release artifact verification failed', file=sys.stderr)
        sys.exit(1)
