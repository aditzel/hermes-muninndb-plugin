# Protected Janus Python CI

Main pushes and same-repository pull requests against any base branch run on
Buildkite's `hermes-muninndb-shadow-ci` pipeline on Janus. The existing slug is
retained so status contexts stay stable. Fork PRs and manual recovery runs keep
read-only hosted Ubuntu 24.04 workers; checkout never persists credentials.
Both Python lanes run independently. Hosted recovery retains `fail-fast: false`.
See [CI operations and rollback](ci-operations.md) for triggers, status checks,
commissioning gates and recovery commands. The separate Buildkite manual-CI
route still needs commissioning; this change moves ordinary push/PR execution.

The pipeline reference calls `janus-ci bootstrap` using the deployed
image-owned `hermes-muninndb-shadow-ci` policy. No repository
pipeline file grants admission. Each independent lane preserves the original
360-minute Actions bound and has an empty environment allowlist and artifact list:

| Job | Protected interpreter | Command |
| --- | --- | --- |
| `shadow-python-3-11` | Python 3.11.16 | `python -I -B scripts/buildkite/check.py 3.11` |
| `shadow-python-3-12` | Python 3.12.12 | `python -I -B scripts/buildkite/check.py 3.12` |

The shared image supplies checksum-pinned Python, venv/ensurepip and TLS support.
Jobs run without credentials as UID 10001 in fresh restricted containers. The
native admission, trusted source/merge validation and statuses are tracked by
commissioning evidence for each exact source and image. A successful native run
verifies only that recorded revision. The 360-minute provider limit bounds the
whole job, including setup and cleanup; it is not 360 minutes plus overhead.

Each lane creates private check and wheel virtualenvs. It preserves the original
pip upgrade, editable development install, pytest, wrapper compilation, package
build, wheel install and materialized-plugin import. Child environments allow only
local HOME/HERMES_HOME/XDG/TMPDIR paths and required runtime variables. Inherited
credentials, pip configuration, Python paths and pytest options are not forwarded.
The wheel CLI uses `--no-activate --json`; verification rejects activation and a
plugin path outside the temporary Hermes home. `MANIFEST.in` includes this wrapper
so tests also work in source distributions.

TERM, INT and HUP record the first cancellation signal. Each command runs in its
own process group; cleanup sends TERM, waits at most three seconds, then KILLs
remaining group members and reaps the direct child. The private virtualenvs, homes,
configuration and caches are removed on success, ordinary failure and handled
cancellation. The controller still owns exhaustive UID cleanup, including escaped
process sessions and hard termination; wrapper tests do not prove provider cleanup.

## Separate publishing boundary

`.github/workflows/publish.yml` is unchanged. Its Python 3.12 distribution build,
original wheel/sdist artifact, release-only PyPI job, protected `pypi` environment,
OIDC identity and attestations remain authoritative. This ordinary CI cutover
neither publishes artifacts nor replaces release/manual input or publishing
handoffs. Release preparation remains in separate, unmerged pull requests.

Dependencies retain their existing unpinned pip upgrades and project constraints;
no reproducible dependency-resolution claim is made. Before merging the cutover, verify both
native PR/main lanes, exact source and base identity, failure/cancellation/status
behavior, hosted fork isolation, required statuses and a successful hosted manual
fallback. Keep the cutover PR unmerged until those gates pass, then verify the
main-push webhook at the merge commit.
