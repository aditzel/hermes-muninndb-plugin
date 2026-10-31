# Buildkite release integration

This is inactive preparation. Leave `JANUS_HERMES_RELEASE_ENABLED` unset until
the native release pipeline and the complete handoff have been commissioned.
The ordinary CI workflow and manual **Publish / Run workflow** build remain
unchanged. Manual builds do not publish.

## Routing

With the variable absent or any value other than `true`, `release:published`
continues to build on the existing Linux runner. The final PyPI job uses a
GitHub-hosted Ubuntu runner for trusted publishing, preserving the `pypi`
environment, package name, attestations and failure on an existing version.

With the variable set to `true`, the protected Buildkite pipeline
`hermes-muninndb-release-build` owns release builds. Configure that pipeline for
native `release:published` events only, never GitHub `status` events. Its success
status with context `buildkite/hermes-muninndb-release-build` starts the hosted
verification jobs in `.github/workflows/publish.yml` on the default branch.
Neither hosted job waits for Buildkite's queue or execution; each is limited to
15 minutes. Other status contexts and unsuccessful statuses skip these jobs.

The `build` job verifies authenticated release, source, retry and artifact
records, then preserves the `python-package-distributions` artifact. It also
uploads `hermes-release-handoff-<workflow.runId>-<producerJob.id>`, with both
numeric IDs taken from the verifier. The publisher selects that exact artifact
ID from the current run, downloads the handoff, rechecks provider state, and
downloads and verifies the original distribution bytes again. It does not use
the loose GitHub distribution artifact in this route. Publisher-only retries
may reuse the latest successful producer in the same run; a newer producer
invalidates an older handoff.

Publisher concurrency uses the verified numeric release ID, so separate releases
sharing a commit are not grouped together. It prevents overlapping publishers
for that release; it is not an exactly-once guarantee. GitHub may replace an
older pending job in the same group. A repeated publish still fails if PyPI
already has the version; `skip-existing` is deliberately not enabled.

## Credentials and helper updates

Provision a separately scoped, read-only Buildkite credential as
`BUILDKITE_HERMES_RELEASE_READ_TOKEN`, with access only to the release pipeline's
build and artifact records. The job's GitHub token has `contents: read` and
`actions: read`; only the publisher additionally has `id-token: write`.
No checkout token is persisted and no package code is installed by a verifier.

The helper bundle is checked out at immutable Hermes commit
`380cf85ff1f20e8dc4f78af35716691680c68383`, independently of the event's release
commit and default-branch workflow commit. Keep that commit reachable when
merging: use a merge that preserves it, rather than squashing or rebasing it
away. See [the bundle provenance](../.ci/buildkite/provenance.json) and
[update rules](../.ci/buildkite/README.md). Review upstream changes and producer
policy hashes before creating a new bundle commit and updating both workflow
pins in a later commit.

Token files live in private directories and are removed at the end of each
verification step. The publisher's verified packages live under a private
workspace directory, and the PyPI container receives its relative path so the
runner's workspace mount can expose them. Final cleanup removes that directory
even after ordinary step failure. The build job uses the runner's temp directory.

## Commissioning and rollback

Before enabling the variable, verify scoped credentials, the matching protected
runner image and policy, Buildkite's GitHub App access and status reporting,
native release identity, and actual status/API/artifact redirect shapes. Prove
pass/fail, stale and duplicate statuses, retry, cancellation, timeout, queue and
execution budgets, and rollback without publishing a real package. Validate the
default-branch workflow and PyPI environment/trusted-publisher identity; merging
this preparation alone is not commissioning. Package or release publication
needs separate authorization.

To roll back future release routing, unset the variable. Inspect and explicitly
cancel or reconcile already-started runs before retrying a release; changing a
variable does not undo a run or a published version. The original release build
path and artifact name are retained. The manual workflow's separate Janus
migration and deadline parity remain outstanding.
