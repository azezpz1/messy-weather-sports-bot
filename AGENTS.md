# Agent notes for this repo

Posts messy NFL game-day weather to Bluesky. Python (`uv`-managed), source in
`src/messy_weather_nfl_bot/`, tests in `tests/unit/` and `tests/integration/`.
See `README.md` for user-facing docs; this file is about *working on* the repo.

## Before every push

Run `./scripts/check.sh` (see README's "Development" section for the
commands it runs and the network-free variant) — even for small changes.
A large fraction of this repo's PR history is follow-up commits fixing
things CI or CodeRabbit caught after the fact, all of which this script
catches locally with no review round-trip needed. Don't rely on the
pre-push git hook alone unless you're sure it's installed in your session.

## Patterns that have bitten this repo before

Drawn from actual CodeRabbit/CodeQL findings and fixups in this repo's
history — check for these explicitly in anything you touch:

- **Shell/workflow injection**: GitHub Actions workflow steps that interpolate
  untrusted input (PR titles, branch names, tag names) directly into a `run:`
  shell command. Pass such values through `env:` and reference them as
  `"$VAR"`, never inline `${{ ... }}` in the script body.
- **Secrets leaking via process args**: don't pass tokens/credentials as CLI
  arguments to `curl` or other subprocesses (visible in process listings and
  often in logs) — use headers/env vars/stdin instead.
- **Unsanitized strings reaching logs, asserts, or ping bodies**: user- or
  API-controlled strings (error messages, alert text) have triggered
  CodeQL substring-sanitization findings more than once, including inside a
  *test* file after the production code was already fixed — the same unsafe
  pattern got reused. When you fix one occurrence, grep for the same pattern
  elsewhere (tests included) rather than assuming it's a one-off.
- **Timezone-naive datetimes**: NWS alert timestamps and similar external
  data have caused crashes when assumed to be timezone-aware; validate/reject
  rather than assume.
- **Release workflow checkout state**: `release-publish.yml` / the release
  scripts have previously broken on detached-HEAD checkouts and tag-ref
  parsing edge cases (e.g. `.git` suffix handling in repo names). If you
  touch release tooling, actually trace through what ref/HEAD state each
  step runs in rather than assuming `main` is checked out.
- **The release PR CI-approval gap is known and documented** in the README's
  "Releasing" section — don't rediscover it, and don't try to "fix" it by
  changing token permissions without reading that section first.

CodeRabbit reviews every PR (`.coderabbit.yaml`, `assertive` profile) — treat
its findings as a first pass to get ahead of, not a follow-up to react to.

## Branch hygiene

This repo has a lot of `Merge main into <branch>` commits in its history.
Before starting work, fetch and rebase/merge onto the current `main` rather
than working from a stale base — it avoids conflicts and means you're
testing against the code that will actually be there when the PR lands.
