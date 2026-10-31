# Ordinary CI operations

| Event | Execution |
| --- | --- |
| Push to `main` | Buildkite `hermes-muninndb-shadow-ci` on Janus |
| Same-repository PR, any base branch | Buildkite on Janus |
| Fork PR, any base branch | GitHub `CI` Python 3.11/3.12 matrix on hosted Ubuntu |
| GitHub `workflow_dispatch` | Hosted recovery matrix on the selected ref |

The repository workflow only routes hosted work. Image-owned admission independently
checks the repository, source, PR identity and allowed events before running code
on Janus. Do not enable fork builds in the provider to repair a routing problem.
Manual CI in Buildkite remains a separate commissioning item; the hosted dispatch
is retained as a working recovery path. Publishing is outside this cutover.

## Provider settings

The pipeline's repository must be `aditzel/hermes-muninndb-plugin`, with protected
`janus-ci bootstrap` on `janus-linux`. Keep:

- Push builds enabled with branch pattern `main`; tags and third-party forks off.
- PR opened/updated/reopened builds enabled without a PR base-branch restriction.
- Existing-commit skipping and intermediate-build skipping/cancellation off.
- Default and maximum command timeouts at 360 minutes; both generated lanes at 360.
- Commit status reporting and separate PR statuses enabled.

The expected PR context is `buildkite/hermes-muninndb-shadow-ci/pr`; branch builds
use `buildkite/hermes-muninndb-shadow-ci`. Match the actual status at the exact
commit before relying on it. The hosted `test (3.11)` and `test (3.12)` jobs skip
same-repository PRs intentionally; their skipped result is not native CI evidence.
This change does not introduce or alter GitHub branch-protection rules.

Neither ordinary lane uploads artifacts. Each builds and installs its wheel in
private temporary storage, verifies materialization without activation, and cleans
up. PyPI publication and its artifacts remain in the separate release workflow.

## Acceptance before merge

Record the source SHA, image digest, pipeline/build/job identities and GitHub status
for both Python lanes. Require a real PR webhook build, failure/cancellation and
retry behavior, fork routing and a successful hosted recovery matrix. Previously
successful UI builds alone do not prove webhook delivery. Verify the main-push
webhook and both lanes at the merge commit immediately after landing; restore
hosted automation if the cutover does not deliver it.

## Recovery

Run the hosted matrix on an exact reviewed ref when Janus is unavailable:

```sh
gh workflow run ci.yml --repo aditzel/hermes-muninndb-plugin --ref <reviewed-branch-or-tag>
```

Verify the resulting run's `head_sha` and that both Python jobs passed. A successful
dispatch proves recovery for that revision, not that Buildkite is healthy.

To restore automatic hosted CI, revert the cutover's workflow routing: add
`push: { branches: [main] }` and remove the job-level `if` from `test`. Preserve
`pull_request`, `workflow_dispatch`, the full matrix and steps. Restore and verify
hosted checks before disabling Buildkite webhook processing; duplicate checks
during recovery are preferable to a gap. Preserve the reviewed runner image and
provider settings in the commissioning record for rollback.
