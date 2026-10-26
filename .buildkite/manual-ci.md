# Buildkite manual CI pilot

The separate [Hermes manual CI pipeline](https://buildkite.com/mirai-forge/hermes-muninndb-manual-ci)
is active on Janus for commissioning. Ordinary push/PR CI remains on
`hermes-muninndb-shadow-ci`; its [reviewed operations and rollback guide](https://github.com/aditzel/hermes-muninndb-plugin/blob/500da82d8b65cbd7bf2fed6d7df32d2752b533a1/.buildkite/ci-operations.md)
still applies. Keep the GitHub-hosted manual recovery control until the remaining
manual acceptance gates are complete.

## Start a build

Use **New build** in the manual pipeline with no environment overrides or custom
steps. Select a branch such as `main`, or an explicit `refs/tags/<tag>` reference.
Use `HEAD` to resolve that ref once, or supply a full commit SHA. An explicit
branch commit must belong to the branch; an explicit tag commit must equal the
tag's peeled target. A bare name without a provider tag selects a branch.

The protected controller authenticates the Buildkite creator and current attempt,
freezes the source commit and job graph, and generates two independent jobs:

| Job | Command allowance | Provider limit |
| --- | --- | --- |
| `shadow-python-3-11` | 360 minutes | 372 minutes |
| `shadow-python-3-12` | 360 minutes | 372 minutes |

The extra provider allowance covers admission, checkout and finalization. These
are the configured manual limits; the ordinary pipeline retains its separate
360-minute whole-job ceiling. Retrying an admitted job uses the frozen commit,
even if its selected branch or tag subsequently moves or disappears.

The jobs use the same Python CI commands as ordinary CI. During authorized
finalization, the image-owned Janus controller uploads `janus-artifact-index.json`
with the tested source, job attempt and result. The repository commands do not
create this index: the controller produces it even when the declared payload
list is `artifacts: []`. Failed identity verification can prevent its upload.
These jobs do not upload distribution payloads or publish to PyPI. The
distribution-build and release/publishing routes are separate. Reader credentials
stay in the root controller and are never passed to repository processes.

## Verified recovery and remaining gates

On 2026-10-25, [manual build 12](https://buildkite.com/mirai-forge/hermes-muninndb-manual-ci/builds/12)
at `500da82d8b65cbd7bf2fed6d7df32d2752b533a1` rejected an invalid controller
reader during bootstrap checkout. The denied attempt created no source receipt,
downstream jobs or artifacts. After restoring the approved reader, an in-place
retry passed both Python lanes and produced two indexes bound to their successful
job attempts and exact source commit.

Separately, [manual build 13](https://buildkite.com/mirai-forge/hermes-muninndb-manual-ci/builds/13)
tested reader authentication loss during execution. The next provider observation
stopped the Python 3.11 probe after about 120 seconds; its independent Python 3.12
sibling passed. After reader restoration, [build 14](https://buildkite.com/mirai-forge/hermes-muninndb-manual-ci/builds/14)
passed both lanes at the same reviewed `500da82` source used by build 12 and
uploaded two controller indexes, each with an empty payload list and the exact
successful job/source identity. This exercises HTTP 401, not network/5xx outages.

On 2026-10-26 UTC, activation rollback was exercised by archiving both manual
pipelines and the later MCP pilot, draining the queue, and restoring the saved
14-profile image (`9f6a51aea9e1`) and Compose configuration. The manual profiles
were absent; [ordinary build 21](https://buildkite.com/mirai-forge/hermes-muninndb-shadow-ci/builds/21)
passed both Python lanes. After restoring the normal 17-profile image
(`ab632d6ba332`) and its original secret mounts, all three pipelines were
unarchived and the queue resumed. [Manual build 16](https://buildkite.com/mirai-forge/hermes-muninndb-manual-ci/builds/16)
passed at `500da82d8b65cbd7bf2fed6d7df32d2752b533a1`; both controller indexes
were checked against provider hashes and exact source/job identities. Cleanup
left no job processes, checkout or controller state, and eight unrelated
containers were unchanged. This proves activation rollback for these saved
configurations; it does not complete the remaining acceptance below.

Retiring hosted manual recovery requires recorded live acceptance of every remaining gate:

- API-origin creation and the remaining manual admission/refusal cases.
- Network/5xx failures, command-timeout and remaining-budget handling, queue
  interference, cancellation cleanup and retry behavior.
- Ordinary trusted-PR admission, fork exclusion on Janus with hosted fork
  execution, exact-commit GitHub status reporting and a real `main` push build,
  as required by the linked ordinary operations guide.

Keep the hosted recovery control until all these gates pass for the configuration
being commissioned. A green pilot alone does not complete manual cutover.
