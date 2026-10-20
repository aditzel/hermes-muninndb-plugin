# Buildkite release verification helpers

These standard-library helpers are copied byte-for-byte from the Devops commit
recorded in `provenance.json`. They verify the current GitHub workflow jobs,
release and tag, protected Buildkite source/build/retry evidence, and exact
distribution bytes. They never install package code or publish to a registry.

Keep the relative layout: `hermes-release.py` imports `hermes.py` and
`../buildkite-janus/hermes_actions.py`. The policy is independently pinned to the
reviewed release producer; never replace it with a value from an artifact.

The publishing workflow must check out this bundle at a literal reviewed commit,
with persisted Git credentials disabled. It must never load helpers from a
release tag, event-supplied ref, downloaded package, or unreviewed moving branch.
A future helper update needs a source/hash review, matching producer-policy
verification, and a separate update of the workflow's literal commit pin.

The status route is preparation until native release events, Buildkite status
reporting, scoped credentials, matching runner policy, retries, cancellation and
rollback have been verified. Keep `JANUS_HERMES_RELEASE_ENABLED` unset until those
gates pass. Manual CI migration is separate.
