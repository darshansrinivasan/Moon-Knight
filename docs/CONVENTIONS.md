# Conventions

Three people commit to `main` directly, often minutes apart, and every push
deploys to production. This document is what keeps that workable.

## Setup, once per clone

```sh
./scripts/setup-dev.sh
```

This is not optional bookkeeping — it is the only way the shared git hooks reach
your clone. `.git/hooks` is not versioned and git deliberately refuses to let a
repository set its own hooks path (a clone could then run code you had not
read), so the committed `.githooks/` directory needs a one-time pointer.

The script also sets `pull.rebase`, which is the setting that actually keeps
`main` clean. See [Working on main](#working-on-main).

## Commit messages

We follow [Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/).

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

The colon **and the space** are both required (spec rule 1). This is the one
thing that changed from how this repo used to work: `Feat:SupportWeeklyDashboard`
is not valid, `feat: add support weekly dashboard` is.

### Types

| Type | Use it when |
|---|---|
| `feat` | a new capability, user-visible or API-visible |
| `fix` | a bug fix |
| `refactor` | the behaviour is identical and the code is different |
| `perf` | the behaviour is identical and it is faster |
| `docs` | documentation and specs only |
| `test` | tests only |
| `style` | formatting, whitespace, naming — no logic change |
| `build` | Dockerfile, requirements.txt, Procfile, railway.json |
| `ci` | anything under `.github/` |
| `chore` | maintenance that fits nothing above |
| `revert` | reverting an earlier commit |

`feat` and `fix` are mandated by the spec (rules 2 and 3); the rest of this list
is ours (rule 14 permits it).

**`Enhanc` is gone.** It was this repo's most-used type and has no Conventional
equivalent, because it conflated two different things. Split it:

- an improvement that adds a capability → `feat`
- an improvement to code that already worked → `refactor`, `perf`, or `style`

### Scopes

Optional, and a noun in parentheses (rule 4). Use the module or page the change
belongs to, which for this codebase means roughly:

`rules`, `qc`, `auth`, `slack`, `funcheck`, `report`, `leaderboard`, `open`,
`drilldown`, `evidence`, `scorer`, `vault`, `admin`, `dashboard`

```
fix(rules): restore admin access to the rubric
refactor(slack): collapse the two mention builders
perf(drilldown): stop re-querying the ticket table per row
```

### Breaking changes

Either mark the prefix with `!` (rule 13), or add a footer (rule 12), or both:

```
feat(auth)!: operators own the grading rules

BREAKING CHANGE: admins no longer edit the rubric; grant Operator instead.
```

`BREAKING CHANGE` **must be uppercase** (rules 12 and 15) — `BREAKING-CHANGE` is
accepted as a synonym (rule 16). A lowercase token is silently ignored by every
changelog and release tool, which means the breaking change ships unannounced.
The checker rejects the wrong case for exactly that reason.

### Case

Per spec rule 15, type and scope are **not** case-sensitive, so `Feat: x` is
valid and the checker does not reject it. House style is lowercase throughout,
including the description. Write descriptions as prose, not PascalCase:
`feat: add weekly support dashboard`, not `feat: AddWeeklySupportDashboard`.

### Two rules of our own

Not from the spec, and labelled as ours in the error output:

- subject line capped at 72 characters
- no trailing full stop on the subject

### Examples

```
feat: add weekly support dashboard
fix(rules): restore admin access to the rubric
refactor(slack): collapse the two mention builders
docs: add the v6 core engine spec
test(roles): pin the admin row of the rubric matrix
ci: run the suite on push to main
feat(auth)!: operators own the grading rules
```

## What enforces this

Three layers, and it is worth knowing which one will catch you and when.

**1. The `commit-msg` hook — the real gate.** Runs before the commit object
exists, so a rejected message costs you nothing: fix it and commit again. This is
where you want to be caught.

```sh
git commit --amend          # fix a message you already committed locally
git commit --no-verify      # deliberate exception; you own the consequence
```

**2. CI — a backstop, advisory on purpose.** The `commit-messages` job re-checks
every commit a push introduced, so a `--no-verify` bypass still shows up. It is
marked `continue-on-error`, so it reports and does **not** block the deploy.

That is deliberate. By the time CI sees a bad message, the commit is on shared
`main`, where the only way to fix the text is a force-push — and force-pushing a
branch three people are committing to is considerably worse than a badly named
commit. If you ever want it to block, delete the `continue-on-error:` line in
`.github/workflows/tests.yml`, but agree first on how a bad message on `main`
gets repaired.

**3. The `tests` job — this one does block.** See below.

## What blocks a deploy

`main` auto-deploys to Railway on every push. The `tests` job in
`.github/workflows/tests.yml` runs the full suite, and Railway's **Wait for CI**
holds the deployment in `WAITING` until it finishes:

- suite passes → the deploy proceeds
- suite fails → the deploy is `SKIPPED` and production keeps running the last
  good commit

So a red suite costs you a deploy, not an outage.

### Running the suite yourself

Python **3.10 or newer is required**, not preferred. `db.py` annotates
`dict | None` at module scope, which 3.9 evaluates at import time and rejects, so
on a 3.9 interpreter the suite dies at `import db` before a single assertion
runs. On macOS the system `python3` is 3.9.

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHON=.venv/bin/python ./tests/run.sh
```

`run.sh` builds a throwaway database and master key per suite and every outbound
call in the suites is faked, so it needs no secrets and touches nothing real.

Set `QC_TEST_STDERR=1` to see tracebacks. Without it, stderr goes to `/dev/null`
and a suite that dies on an import reports only `SUITES FAILED: t_x` with no
cause. CI sets it.

## Working on main

Git already stops the worst outcome: a push from a stale clone is **rejected**
as a non-fast-forward, so you cannot overwrite someone else's commits without
`--force`. Don't use `--force` on `main`.

What git does *not* decide for you is how you catch up, and its default is to
refuse to guess. `setup-dev.sh` sets:

```sh
git config pull.rebase true      # rebase instead of a merge commit
git config rebase.autoStash true # a dirty tree does not block the rebase
git config fetch.prune true      # drop branches deleted on the remote
```

Rebase, not merge, for two reasons: `main` stays linear and readable, and your
commits stay attributable to you instead of being buried under merge commits from
whoever pulled last. `git log --oneline` has been merge-free for 100+ commits;
this keeps it that way.

## Not yet enforced server-side

Everything above is a local guardrail and can be bypassed with `--no-verify` or
`--force`. Making it binding needs a **GitHub ruleset on `main`**, which requires
repository admin:

- require linear history (rejects merge commits at the server)
- block force pushes
- require the `tests` check to pass

Worth asking for. Until then, the conventions hold because everyone runs
`setup-dev.sh`, not because GitHub makes them.
