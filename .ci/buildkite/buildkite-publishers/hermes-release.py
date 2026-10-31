#!/usr/bin/env python3
"""Bind verified Hermes release artifacts to the current GitHub run attempt.

All phases are read-only against providers. No phase publishes packages.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


HERE = Path(__file__).resolve().parent
h = module('hermes_artifacts', HERE / 'hermes.py')
a = module('hermes_actions', HERE.parent / 'buildkite-janus/hermes_actions.py')
PURPOSE = 'hermes-release-handoff-v1'
HANDOFF = 'handoff.json'
MAX_HANDOFF = 65536
STATUS_CONTEXT = 'buildkite/' + h.PIPELINE
BUILD_URL = 'https://buildkite.com/mirai-forge/' + h.PIPELINE + '/builds/'


class Actions:
    def __init__(self, api, github, deadline):
        self.api = api
        self.runs = a.Client(github, deadline)

    def json(self, path):
        return self.runs.json(path)

    def jobs(self, run_id):
        return self.pages(run_id, 'jobs')

    def artifacts(self, run_id):
        return self.pages(run_id, 'artifacts')

    def pages(self, run_id, kind):
        h.require(a.integer(run_id), 'Invalid workflow run ID')
        h.require(kind in ('jobs', 'artifacts'), 'Unknown workflow inventory')
        rows, total = [], None
        for page in range(1, 11):
            query = 'filter=all&' if kind == 'jobs' else ''
            path = '/actions/runs/' + str(run_id) + '/' + kind + '?' + query + 'per_page=100&page=' + str(page)
            headers = {'Authorization': 'Bearer ' + self.api.tokens['github'], 'Accept': 'application/vnd.github+json',
                       'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'janus-hermes-handoff'}
            value = h.decode(self.api.request(a.API + path, headers, h.MAX_JSON))
            h.require(isinstance(value, dict) and type(value.get('total_count')) is int
                      and 0 <= value['total_count'] <= 1000 and isinstance(value.get(kind), list)
                      and len(value[kind]) <= 100, 'Invalid workflow inventory')
            if total is None:
                total = value['total_count']
            h.require(total == value['total_count'], 'Workflow job inventory changed during pagination')
            rows.extend(value[kind])
            h.require(len(rows) <= total, 'Workflow job inventory exceeds its count')
            if len(rows) == total:
                return rows
            h.require(len(value[kind]) == 100, 'Incomplete workflow page')
        raise h.Rejected('Workflow job inventory exceeds bound')


def job_identity(row, workflow):
    h.require(isinstance(row, dict) and a.integer(row.get('id')) and type(row.get('run_id')) is int
              and row['run_id'] == workflow['runId'] and a.integer(row.get('run_attempt'))
              and row['run_attempt'] <= workflow['runAttempt'] and row.get('head_sha') == workflow['commit']
              and row.get('workflow_name') == 'Publish' and isinstance(row.get('name'), str), 'Workflow job source mismatch')
    h.require(row.get('url') == a.API + '/actions/jobs/' + str(row['id'])
              and row.get('run_url') == a.API + '/actions/runs/' + str(workflow['runId']), 'Workflow job URL mismatch')
    return {'id': row['id'], 'runId': row['run_id'], 'runAttempt': row['run_attempt'],
            'name': row['name'], 'commit': row['head_sha']}


def workflow_jobs(actions, workflow, *, producer=None, publishing=False):
    rows = actions.jobs(workflow['runId'])
    h.require(isinstance(rows, list) and 1 <= len(rows) <= 1000, 'Missing workflow jobs')
    identities, ids = [], set()
    for row in rows:
        identity = job_identity(row, workflow)
        h.require(identity['id'] not in ids, 'Duplicate workflow job ID')
        ids.add(identity['id'])
        identities.append((row, identity))
    builds = [(row, identity) for row, identity in identities if identity['name'] == 'build']
    h.require(builds, 'Missing source build job')
    newest = max(identity['runAttempt'] for _, identity in builds)
    selected = [(row, identity) for row, identity in builds if identity['runAttempt'] == newest]
    h.require(len(selected) == 1, 'Ambiguous source build attempt')
    row, build = selected[0]
    if not publishing:
        h.require(build['runAttempt'] == workflow['runAttempt'] and row.get('status') == 'in_progress'
                  and row.get('conclusion') is None, 'Source build job is not the active job')
        return {'producer': build, 'publisher': None}
    h.require((producer is None or h.same(build, producer)) and row.get('status') == 'completed' and row.get('conclusion') == 'success',
              'Handoff build failed or was superseded')
    publishers = [(row, identity) for row, identity in identities
                  if identity['name'] == 'publish-pypi' and identity['runAttempt'] == workflow['runAttempt']]
    h.require(len(publishers) == 1, 'Missing or ambiguous current publisher job')
    row, publisher = publishers[0]
    h.require(row.get('status') == 'in_progress' and row.get('conclusion') is None, 'Publisher job is not active')
    return {'producer': build, 'publisher': publisher}


def status_trigger(event):
    h.require(isinstance(event, dict) and event.get('state') == 'success'
              and event.get('context') == STATUS_CONTEXT and a.integer(event.get('id')),
              'Expected the completed Hermes release status')
    repository = event.get('repository')
    h.require(isinstance(repository, dict) and type(repository.get('id')) is int
              and repository['id'] == a.REPOSITORY_ID and repository.get('full_name') == a.REPOSITORY,
              'Status belongs to another repository')
    sha = h.digest(event.get('sha'), 40)
    target = event.get('target_url')
    h.require(isinstance(target, str) and re.fullmatch(re.escape(BUILD_URL) + r'[1-9][0-9]{0,15}', target),
              'Status does not identify an approved release build')
    number = int(target[len(BUILD_URL):])
    h.require(a.integer(number), 'Invalid status build number')
    return {'statusId': event['id'], 'buildNumber': number, 'commit': sha}


def status_release(api, trigger, approved):
    # The event is a routing hint, not authorization. Construct the fixed API
    # path instead of fetching its target_url, then authenticate the full build.
    h.policy(approved)
    build = api.api('buildkite', h.PREFIX + '/' + str(trigger['buildNumber']) + '?include_retried_jobs=true')
    h.require(isinstance(build, dict) and type(build.get('number')) is int
              and build['number'] == trigger['buildNumber'] and build.get('commit') == trigger['commit']
              and build.get('source') == 'webhook' and isinstance(build.get('pipeline'), dict)
              and build['pipeline'].get('slug') == h.PIPELINE
              and build['pipeline'].get('repository') == 'https://github.com/' + a.REPOSITORY + '.git',
              'Status build source mismatch')
    h.identifier(build.get('id'))
    h.completed_job(build)
    h.require(isinstance(build.get('meta_data'), dict), 'Missing protected release metadata')
    source = h.decode(build['meta_data'].get(h.SOURCE_KEY), 65536)
    h.require(isinstance(source, dict) and isinstance(source.get('release'), dict), 'Missing protected release source')
    value = source['release']
    release = h.release_identity({'id': value.get('id'), 'tag_name': value.get('tag'),
        'published_at': value.get('publishedAt'), 'prerelease': value.get('prerelease'), 'draft': False})
    expected = {'commit': trigger['commit'], 'release': release}
    h.frozen_source(source, build, expected, approved)
    h.check_release(api, expected)
    newest, job, frozen = h.newest_build(api, expected, approved)
    h.require(newest['id'] == build['id'] and newest['number'] == build['number']
              and job['id'] == h.completed_job(build)['id'] and h.same(frozen, source),
              'Status identifies a superseded release build')
    return expected


def expected_release(event, environment, phase, *, api=None, approved=None):
    h.require(phase in ('prepare', 'select', 'verify'), 'Unknown release verification phase')
    h.require(environment.get('GITHUB_JOB') == ('build' if phase == 'prepare' else 'publish-pypi'),
              'Unexpected release workflow job')
    workflow = a.from_environment(environment)
    if workflow['event'] == 'status':
        trigger = status_trigger(event)
        return {'workflow': workflow, 'trigger': trigger, 'release': status_release(api, trigger, approved)}
    h.require(workflow['event'] == 'release', 'Release verification requires a release event')
    release = h.expected_release(event, workflow['commit'], workflow['ref'])
    return {'workflow': workflow, 'release': release}


def validate_expected(expected):
    h.require(isinstance(expected, dict) and set(expected) in ({'workflow', 'release'}, {'workflow', 'release', 'trigger'}),
              'Invalid release request')
    workflow = a.request(expected['workflow'])
    h.require(workflow['event'] in ('release', 'status'), 'Manual runs cannot authorize a release publisher')
    release = expected['release']
    h.require(isinstance(release, dict) and set(release) == {'commit', 'release'}, 'Invalid release source')
    h.digest(release['commit'], 40)
    value = release['release']
    h.require(isinstance(value, dict) and set(value) == {'id', 'tag', 'publishedAt', 'prerelease'}, 'Invalid release identity')
    normalized = h.release_identity({'id': value['id'], 'tag_name': value['tag'], 'published_at': value['publishedAt'],
                                     'prerelease': value['prerelease'], 'draft': False})
    h.require(h.same(value, normalized), 'Invalid release identity')
    if workflow['event'] == 'release':
        h.require('trigger' not in expected and release['commit'] == workflow['commit']
                  and workflow['ref'] == 'refs/tags/' + value['tag'], 'Release ref differs from workflow')
    else:
        trigger = expected.get('trigger')
        h.require(isinstance(trigger, dict) and set(trigger) == {'statusId', 'buildNumber', 'commit'}
                  and a.integer(trigger['statusId']) and a.integer(trigger['buildNumber'])
                  and trigger['commit'] == release['commit'], 'Invalid status-to-release binding')
    return expected


def select_handoff(actions, expected):
    validate_expected(expected)
    identity = a.verify(actions, expected['workflow'])
    jobs = workflow_jobs(actions, expected['workflow'], publishing=True)
    name = 'hermes-release-handoff-' + str(identity['runId']) + '-' + str(jobs['producer']['id'])
    rows = actions.artifacts(identity['runId'])
    h.require(isinstance(rows, list) and len(rows) <= 1000, 'Invalid handoff artifact inventory')
    matches, ids = [], set()
    for row in rows:
        h.require(isinstance(row, dict) and a.integer(row.get('id')) and row['id'] not in ids, 'Invalid or duplicate handoff artifact ID')
        ids.add(row['id'])
        if row.get('name') == name:
            matches.append(row)
    h.require(len(matches) == 1, 'Missing or ambiguous source-job handoff artifact')
    artifact = matches[0]
    run = artifact.get('workflow_run')
    h.require(isinstance(run, dict) and type(run.get('id')) is int and run['id'] == identity['runId']
              and type(run.get('repository_id')) is int and run['repository_id'] == a.REPOSITORY_ID
              and type(run.get('head_repository_id')) is int and run['head_repository_id'] == a.REPOSITORY_ID
              and run.get('head_sha') == identity['commit'] and run.get('head_branch') == identity['ref'].split('/', 2)[2],
              'Handoff artifact belongs to another run or source')
    h.require(artifact.get('expired') is False and type(artifact.get('size_in_bytes')) is int
              and 0 < artifact['size_in_bytes'] <= MAX_HANDOFF + 8192, 'Handoff artifact is expired or oversized')
    url = a.API + '/actions/artifacts/' + str(artifact['id'])
    h.require(artifact.get('url') == url and artifact.get('archive_download_url') == url + '/zip', 'Handoff artifact URL mismatch')
    h.require(workflow_jobs(actions, expected['workflow'], publishing=True) == jobs
              and a.verify(actions, expected['workflow']) == identity, 'Workflow changed during handoff selection')
    return {'artifactId': artifact['id'], 'artifactName': name, 'workflow': identity, 'producerJob': jobs['producer']}


def private_parent(path):
    info = path.parent.lstat()
    h.require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
              'Release output parent must be a private caller-owned directory')


def read_handoff(path):
    private_parent(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        # GitHub artifact downloads restore ordinary files as 0644. The parent
        # must remain 0700; this non-secret report must never be writable by others.
        h.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                  and not info.st_mode & 0o022 and 0 < info.st_size <= MAX_HANDOFF, 'Unsafe release handoff file')
        return h.decode(stream.read(MAX_HANDOFF + 1), MAX_HANDOFF)


def validate_handoff(value, expected, identity, approved):
    fields = {'schemaVersion', 'purpose', 'workflow', 'producerJob', 'release', 'verification'}
    if 'trigger' in expected:
        fields.add('trigger')
    h.require(isinstance(value, dict) and set(value) == fields
              and type(value['schemaVersion']) is int and value['schemaVersion'] == 1
              and value['purpose'] == PURPOSE, 'Invalid release handoff schema')
    issued = value['workflow']
    h.require(isinstance(issued, dict) and set(issued) == set(identity)
              and a.integer(issued.get('runAttempt')) and issued['runAttempt'] <= identity['runAttempt']
              and h.same({**issued, 'runAttempt': identity['runAttempt']}, identity)
              and h.same(value['release'], expected['release']), 'Release handoff belongs to another run or release')
    h.require(h.same(value.get('trigger'), expected.get('trigger')), 'Release handoff belongs to another status event')
    producer = value['producerJob']
    h.require(isinstance(producer, dict) and set(producer) == {'id', 'runId', 'runAttempt', 'name', 'commit'}
              and a.integer(producer['id']) and type(producer['runId']) is int and producer['runId'] == identity['runId']
              and type(producer['runAttempt']) is int and producer['runAttempt'] == issued['runAttempt']
              and producer['name'] == 'build' and producer['commit'] == identity['commit'], 'Invalid source build job binding')
    verification = value['verification']
    h.require(isinstance(verification, dict) and verification.get('repository') == a.REPOSITORY
              and verification.get('commit') == expected['release']['commit']
              and type(verification.get('releaseId')) is int
              and verification['releaseId'] == expected['release']['release']['id']
              and verification.get('producerPolicySha256') == approved['producerPolicySha256'],
              'Release handoff source or producer policy mismatch')
    # The full report, including IDs, retries and digests, is compared against
    # fresh provider verification below. The report itself authorizes nothing.
    return value


def collect(api, actions, expected, approved, output, *, handoff=None):
    validate_expected(expected)
    h.policy(approved)
    private_parent(output)
    h.require(not output.exists() and not output.is_symlink(), 'Release output must not already exist')
    identity = a.verify(actions, expected['workflow'])
    if 'trigger' in expected:
        h.require(h.same(status_release(api, expected['trigger'], approved), expected['release']), 'Status release changed')
    if handoff is not None:
        validate_handoff(handoff, expected, identity, approved)
    producer = handoff['producerJob'] if handoff is not None else None
    jobs = workflow_jobs(actions, expected['workflow'], producer=producer, publishing=handoff is not None)
    # Quarantine the existing consumer's output until the workflow run is also
    # rechecked. A canceled/rerun workflow cannot leave publisher-visible files.
    with tempfile.TemporaryDirectory(prefix='.hermes-release-', dir=output.parent) as temporary:
        root = Path(temporary)
        verified = root / 'verified'
        verified.mkdir(mode=0o700)
        verification = h.consume(api, expected['release'], approved, verified / 'dist')
        if 'trigger' in expected:
            h.require(verification['buildNumber'] == expected['trigger']['buildNumber']
                      and h.same(status_release(api, expected['trigger'], approved), expected['release']),
                      'Status release changed during verification')
        verification['artifacts'].sort(key=lambda row: row['path'])
        verification['files'].sort(key=lambda row: row['name'])
        h.require(workflow_jobs(actions, expected['workflow'], producer=producer, publishing=handoff is not None) == jobs,
                  'Workflow jobs changed during verification')
        h.require(a.verify(actions, expected['workflow']) == identity, 'Release workflow changed during verification')
        result = {'schemaVersion': 1, 'purpose': PURPOSE, 'workflow': identity,
                  'producerJob': jobs['producer'], 'release': expected['release'], 'verification': verification}
        if 'trigger' in expected:
            result['trigger'] = expected['trigger']
        if handoff is not None:
            h.require(h.same(verification, handoff['verification']), 'Release build, job or artifacts changed since the handoff')
            result = {'schemaVersion': 1, 'purpose': 'hermes-release-publisher-v1', 'workflow': identity,
                      'publisherJob': jobs['publisher'], 'handoff': handoff, 'verification': verification}
        payload = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
        h.require(len(payload) <= MAX_HANDOFF, 'Release handoff exceeds bound')
        report = verified / (HANDOFF if handoff is None else 'publisher-proof.json')
        with report.open('xb') as stream:
            stream.write(payload)
        report.chmod(0o600)
        h.require(not output.exists() and not output.is_symlink(), 'Release output appeared during verification')
        verified.rename(output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'select', 'verify'))
    for name in ('policy', 'event', 'github-token-file', 'buildkite-token-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--handoff', type=Path)
    args = parser.parse_args()
    h.require((args.phase == 'verify') == (args.handoff is not None), 'Verify requires the producer-job handoff')
    h.require((args.phase != 'select') == (args.output is not None), 'Only prepare and verify create output directories')
    approved = h.policy(h.decode(args.policy.read_bytes()))
    handoff = read_handoff(args.handoff) if args.handoff is not None else None
    github, buildkite = h.token_file(args.github_token_file), h.token_file(args.buildkite_token_file)
    api = h.API(github, buildkite)
    expected = expected_release(h.decode(args.event.read_bytes()), os.environ, args.phase, api=api, approved=approved)
    actions = Actions(api, github, time.monotonic() + 150)
    result = select_handoff(actions, expected) if args.phase == 'select' else collect(api, actions, expected, approved, args.output, handoff=handoff)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except h.Pending:
        print('Hermes release build is not ready; no publisher output was exposed', file=sys.stderr)
        sys.exit(75)
    except KeyboardInterrupt:
        print('Hermes release verification canceled', file=sys.stderr)
        sys.exit(130)
    except Exception:
        print('Hermes release verification failed; no publishing action was taken', file=sys.stderr)
        sys.exit(1)
