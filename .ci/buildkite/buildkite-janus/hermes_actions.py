"""Fixed Hermes Actions identity checks for protected build coordination."""
import http.client
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request

REPOSITORY = 'aditzel/hermes-muninndb-plugin'
REPOSITORY_ID = 1210625758
WORKFLOW_ID = 261019053
WORKFLOW_PATH = '.github/workflows/publish.yml'
TOKEN_FILE = 'github-hermes-muninndb-actions-token'
API = 'https://api.github.com/repos/' + REPOSITORY
ERROR = 'Hermes workflow identity could not be verified'


def require(value):
    if not value:
        raise ValueError(ERROR)


def integer(value):
    return type(value) is int and 0 < value < 2**53


def decode(value):
    require(isinstance(value, (str, bytes)) and len(value) <= 1024 * 1024)
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result)
            result[key] = item
        return result
    def invalid(_):
        raise ValueError(ERROR)
    return json.loads(value, object_pairs_hook=unique, parse_constant=invalid)


def request(value):
    require(isinstance(value, dict) and set(value) == {'schemaVersion', 'repositoryId', 'workflowId', 'runId', 'runAttempt', 'event', 'commit', 'ref'})
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1
            and type(value['repositoryId']) is int and value['repositoryId'] == REPOSITORY_ID
            and type(value['workflowId']) is int and value['workflowId'] == WORKFLOW_ID)
    require(integer(value['runId']) and integer(value['runAttempt']) and value['runAttempt'] <= 999999)
    require(value['event'] in ('release', 'workflow_dispatch', 'status'))
    require(isinstance(value['commit'], str) and re.fullmatch(r'[0-9a-f]{40}', value['commit']) and value['commit'] != '0' * 40)
    ref = value['ref']
    require(isinstance(ref, str) and ref.startswith(('refs/heads/', 'refs/tags/')) and len(ref) <= 1024)
    name = ref.split('/', 2)[2]
    require(name and not name.startswith('-') and not name.endswith(('/', '.'))
            and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '~^:?*[\\' for c in name)
            and '..' not in name and '@{' not in name
            and all(part and not part.startswith('.') and not part.endswith('.lock') for part in name.split('/')))
    require(value['event'] != 'release' or ref.startswith('refs/tags/'))
    require(value['event'] != 'status' or ref == 'refs/heads/main')
    return value


def from_environment(env):
    require(env.get('GITHUB_REPOSITORY') == REPOSITORY and env.get('GITHUB_WORKFLOW') == 'Publish')
    require(env.get('GITHUB_WORKFLOW_REF') == REPOSITORY + '/' + WORKFLOW_PATH + '@' + env.get('GITHUB_REF', ''))
    require(env.get('GITHUB_WORKFLOW_SHA') == env.get('GITHUB_SHA'))
    for key in ('GITHUB_REPOSITORY_ID', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
        require(isinstance(env.get(key), str) and re.fullmatch(r'[1-9][0-9]{0,15}', env[key]))
    return request({'schemaVersion': 1, 'repositoryId': int(env['GITHUB_REPOSITORY_ID']), 'workflowId': WORKFLOW_ID,
                    'runId': int(env['GITHUB_RUN_ID']), 'runAttempt': int(env['GITHUB_RUN_ATTEMPT']),
                    'event': env.get('GITHUB_EVENT_NAME'), 'commit': env.get('GITHUB_SHA'), 'ref': env.get('GITHUB_REF')})


def run_identity(run, expected):
    request(expected)
    require(isinstance(run, dict) and run.get('id') == expected['runId'] and type(run.get('id')) is int
            and type(run.get('run_attempt')) is int and run['run_attempt'] == expected['runAttempt'] and integer(run.get('run_number')))
    for name in ('repository', 'head_repository'):
        repo = run.get(name)
        require(isinstance(repo, dict) and type(repo.get('id')) is int and repo['id'] == REPOSITORY_ID
                and repo.get('full_name') == REPOSITORY)
    require(type(run.get('workflow_id')) is int and run['workflow_id'] == WORKFLOW_ID
            and run.get('path') == WORKFLOW_PATH and run.get('name') == 'Publish'
            and run.get('event') == expected['event'] and run.get('head_sha') == expected['commit']
            and run.get('head_branch') == expected['ref'].split('/', 2)[2] and run.get('pull_requests') == [])
    require(run.get('url') == API + '/actions/runs/' + str(expected['runId']))
    require(run.get('html_url') == 'https://github.com/' + REPOSITORY + '/actions/runs/' + str(expected['runId']))
    require(integer(run.get('actor', {}).get('id')) and integer(run.get('triggering_actor', {}).get('id')))
    require(run.get('status') == 'in_progress' and run.get('conclusion') is None)
    return {**expected, 'runNumber': run['run_number']}


def verify(client, expected):
    request(expected)
    path = '/actions/runs/' + str(expected['runId'])
    current = run_identity(client.json(path), expected)
    exact = run_identity(client.json(path + '/attempts/' + str(expected['runAttempt'])), expected)
    require(current == exact)
    # A rerun or cancellation during the attempt read must not be admitted.
    require(run_identity(client.json(path), expected) == exact)
    return exact


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


class Client:
    def __init__(self, token, deadline, *, opener=None, clock=time.monotonic):
        require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_.-]{16,4096}', token))
        self.token, self.deadline, self.clock, self.count = token, deadline, clock, 0
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def remaining(self):
        value = self.deadline - self.clock()
        require(value > 0)
        return min(15, value)

    def json(self, path):
        require(isinstance(path, str) and re.fullmatch(r'/actions/runs/[1-9][0-9]{0,15}(?:/attempts/[1-9][0-9]{0,5})?', path))
        require(self.count < 12)
        self.count += 1
        url = API + path
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'janus-hermes-workflow'}
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.remaining()) as response:
                require(response.geturl() == url and response.status == 200)
                chunks, size = [], 0
                while True:
                    self.remaining()
                    part = response.read1(min(65536, 1024 * 1024 + 1 - size))
                    self.remaining()
                    if not part:
                        break
                    size += len(part)
                    require(size <= 1024 * 1024)
                    chunks.append(part)
            return decode(b''.join(chunks))
        except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise ValueError(ERROR) from None


def token_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
                and not info.st_mode & 0o077 and 0 < info.st_size <= 4096)
        return stream.read(4097).decode('ascii').strip()


if __name__ == '__main__':
    try:
        require(os.geteuid() == 0 and len(sys.argv) == 2)
        expected = request(decode(sys.argv[1]))
        require(expected['event'] == 'workflow_dispatch')
        client = Client(token_file('/run/secrets/' + TOKEN_FILE), time.monotonic() + 45)
        print(json.dumps(verify(client, expected), sort_keys=True))
    except Exception:
        print(ERROR, file=sys.stderr)
        sys.exit(1)
