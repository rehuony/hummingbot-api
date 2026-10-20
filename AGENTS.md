# Repository instructions

## Purpose and ownership

This is a personal, independently maintained Hummingbot API repository:
https://github.com/rehuony/hummingbot-api, branch `main`, remote `origin`.
Prioritize the owner's actual workflow, usability, reliability, and privacy.
Do not solicit external Issues, feature requests, pull requests, contributions,
or community support. Keep these GitHub entry points and commit comments disabled.
Public visibility and the original MIT license remain intentional.

The original project and the verified development baseline are recorded in
`README.md`. Preserve the license, copyright notices, and attribution.
Hummingbot core, Gateway, exchanges, and market data providers remain legitimate
dependencies; independent maintenance does not mean removing them.

## Engineering

- Understand the relevant code, callers, tests, and existing conventions first.
- Prefer the simplest correct solution, existing utilities, and mature dependencies.
- Keep responsibilities clear and implement the smallest coherent change.
- Research nontrivial decisions and check earlier fixes before inventing mechanisms.
- Preserve compatibility only where required by users, public APIs, persisted data,
  migrations, or an explicit product requirement. Remove obsolete paths.
- Use English for maintenance documentation, comments, and agent guidance.
- If `.codegraph/` exists, use CodeGraph before text search for code discovery.
  Do not create an index automatically.

## Git and publication

Read `docs/maintenance.md` before changing installation or release behavior.
Use `main` tracking `origin/main`; do not restore an upstream remote or an upstream
synchronization workflow. The October 2026 consolidation was a one-time migration.
Create a new focused commit for subsequent changes and use ordinary `git push`.
Do not repeatedly amend the aggregate commit, rewrite published history, or force-push
as a maintenance default. Use a new revert commit for normal rollback.

Before any exceptional history rewrite or branch deletion, verify explicit authorization,
save commits, refs, and uncommitted work privately outside the checkout, and assign one
executor to Git writes. Preserve unrelated branches and worktrees.

## Validation and operational boundaries

- Use the existing `environment.yml`, Conda tooling, and Dockerfile. There is currently
  no tracked dependency lockfile. Preserve one if added later; do not silently replace it.
- Run relevant offline tests, import/compile checks, and the repository's lint tools.
  Check shell, Compose, workflow, links, and residual references when changing deployment.
- Validate changes with isolated configurations and mock services; never place real trades.
- Keep credentials, `.env`, bot data, logs, database dumps, and backups out of commits,
  Docker build contexts, CI logs, and public attachments.
- Git update, commit, push, build, deployment, startup migrations, and restart are distinct.
  Do not deploy or restart services merely because publication was requested.
- Never run `make reset`, `make emqx-auth-reset`, `docker compose down -v`, production
  migrations, or overwrite runtime configuration without specific authorization.
- Preserve authentication, credential protection, local logs, health checks, diagnostics,
  transactional behavior, schema migrations, and persistent data.
- Do not reintroduce telemetry, crash uploads, content sharing, feedback collectors, or
  official API image self-upgrades. Test that old profiles cannot enable bot reporting.
- Verify the published SHA and its CI result. A local commit or build is not deployment.
