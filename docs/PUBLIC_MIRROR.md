# Public mirror contract

The private repository is the implementation source. The public repository is
a reviewed projection of one exact private commit, not an independently edited
release line.

`public-mirror.json` records the source private commit, runtime version,
transformation version, and reviewed hash pairs for JavaScript files whose
public comments intentionally differ. `scripts/verify_public_mirror.py` checks:

- every Python runtime file has the same AST after removing docstrings and
  OpenAPI `title`/`description` text;
- public metadata snapshots and public evaluation queries match semantically;
- dependency locks and `database/SOURCES.yml` are identical;
- reviewed JavaScript pairs still have the hashes approved during the sync;
- private holdout, deployment workflow, collaboration records, and development
  logs do not exist in the public repository;
- the public evaluation manifest consumes only public query sets.

## Sync procedure

1. Commit and test the private source. Do not certify a dirty private tree.
2. Project runtime code, public datasets, public evaluation inputs, locks, and
   public-safe documentation into a clean public worktree.
3. Review every intentional text-only or JavaScript difference. Update a hash
   pair only after that review.
4. Set `source_private_commit` to the exact private commit and keep
   `runtime_version` equal to `WEB_API_VERSION` in both repositories.
5. Run:

   ```powershell
   python scripts/verify_public_mirror.py `
     --private-root C:\path\to\biodata-agent-private `
     --public-root C:\path\to\biodata-agent
   ```

6. Run both full quality profiles and delivery scans before publishing.

Public CI validates the manifest and public-only exclusions without private
source access. Full cross-repository certification is a release-maintainer step
because CI must not receive private source access.
