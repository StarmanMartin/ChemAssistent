---
name: milestone-development
description: ChemAssistent development workflow — the Opus orchestrator executes docs/milestones.md one milestone at a time with at most two Sonnet worker agents in the wt-a/ChemAssistent / wt-b/ChemAssistent worktrees, then reviews, merges, gates on pytest and reports. Use for every ChemAssistent milestone, work package, worker round, review/merge or milestone report; in this repo use it instead of two-worker-milestones.
---

# ChemAssistent milestone development

You are the **orchestrator** (Opus). You design, brief, review, fix merge interactions, commit,
merge and report. Up to two **Sonnet worker agents** implement. The owner (the repo owner) makes
all product, release and security decisions.

## Owner rules (binding)

- One milestone at a time: branch `milestone/mN-<slug>` from the latest `main`; merge back via a
  GitHub PR (like #25–#29). Stop after the milestone and report; start the next only on an
  explicit go-ahead.
- The main checkout must be clean before a milestone starts; if it has uncommitted changes, ask
  the owner what to do with them (never stash/discard on your own).
- **Never more than 2 workers at once.** Workers are Sonnet agents (Agent tool, `model: "sonnet"`,
  `subagent_type: "general-purpose"`, `run_in_background: true`). Workers never commit.
- Never merge code that fails tests or acceptance criteria. No unrelated changes. Stop on blockers.
- **Merging into `main` ships to users**: `install.sh` (curl one-liner) and `install.ps1` fetch
  GitHub `main`. Merge the PR only after the owner's OK.
- Pushing a branch with `.py`/`.svg`/`windows/**` changes runs `.github/workflows/windows.yml`
  and publishes a `windows-<branch>` release. Push only the milestone branch, never `wt/*`.
- Commit trailer and PR footer as in the current system attribution rules.
- Report after each milestone: name, features, tests/verification, review findings + fixes,
  branch/merge status, risks.

## Roadmap: `docs/milestones.md`

- Take the first milestone with status `planned` (or the one the owner names). If none is defined
  or it is vague, draft goal, work packages and acceptance criteria with the owner first.
- In the milestone branch set its status to `in progress`, at the end to `done (#PR)`.

## Secrets and isolation

- Never print secrets: `~/.config/chem-assistent/config.json` values, keyring entries,
  KI-Toolbox/Gemini/GitHub tokens, the Chemotion test login
  (`~/Deveolop/KIT/chemotionapi/tests/config.json`). The repo and its issues are public.
- Tests never reach the real keyring (autouse `FakeKeyring`) or the user's config, history,
  jobs, lessons or exchange folders; new tests patch these paths like the existing fixtures.
- Workers make no real model calls, no evals, no `report_api_bug` to GitHub, no
  `systemctl --user` on the installed service, no `install.sh`/`uninstall.sh`.

## Environment

- Repo `/home/martin/Deveolop/KIT/ChemAssistent` (GitHub `StarmanMartin/ChemAssistent`).
  `CLAUDE.md` is the architecture spec: read it before designing.
- Worktrees `../wt-a/ChemAssistent` (branch `wt/mN-a`) and `../wt-b/ChemAssistent` (`wt/mN-b`).
  The folder itself must be named `ChemAssistent`: the repo root has an `__init__.py`, so pytest
  imports it as a package named after the folder, and a hyphenated name fails to collect.
  `mkdir -p ../wt-a && git worktree add ../wt-a/ChemAssistent -b wt/mN-a milestone/mN-<slug>`,
  then symlink the
  git-ignored `.venv` and `workspace/` (chemotionapi checkout + skills) from the main checkout.
  Sync before each round: `git -C ../wt-a/ChemAssistent merge --ff-only milestone/mN-<slug>`.
- `.venv` (made from `/usr/bin/python3 --system-site-packages`) has chemotionapi `v401.1.1`, the
  line of the docker test instance (ELN 4.0.1, port 3579). If the two do not match, conftest skips
  every Chemotion test and names the line to install; see CLAUDE.md testing notes.
- Jupyter tests need `JUPYTER_TEST_BIN` (scratch venv with `jupyter_server ipykernel`).
- Design docs and round reports: git-ignored `dev/mN/`.

## A worker round

1. Design first for non-trivial milestones: `dev/mN/design.md` (numbered sections, an
   orchestrator-amendments §0 that overrides the rest, acceptance criteria). Get owner decisions
   on open choices.
2. Split into disjoint scopes with explicit **file ownership**. Seams that split well:
   `agent.py` / `cli_agent.py` / `mcp_bridge.py`; `app.py` / `explorer_view.py` / `settings.py`
   (GTK); `jupyter.py` / `jobs.py`; `config.py` / `keystore.py`; `headless.py` / `evals/`.
   Never give both workers `app.py` or `config.py` in one round.
3. **Orchestrator-owned shared files** — workers list the edits they need in their report, you
   apply them: `CLAUDE.md` (must describe every change), `README.md`, `install.sh`
   (`APP_FILES`/`APP_DIRS`), `windows/chem-assistent.spec` (`datas`), `setup.py`
   (`PACKAGE_MODULES`), `requirements.txt`, `docs/milestones.md`.
4. Brief = the worker's entire context. Include: absolute worktree path ("work only there, never
   touch the main checkout"), "read CLAUDE.md", binding design sections by path, exact scope,
   file ownership and what the other worker does, "do not commit", tests:
   `.venv/bin/python -m pytest tests/<targeted files> -m "not chemotion" -q` (no full suite;
   both worktrees share one Chemotion test instance, so only you run `-m chemotion`); pytest
   covers no GTK, so put new logic into GTK-free modules with tests (`tests/fake_ai.py`,
   `tests/fake_mcp.py`, `tests/pdf_maker.py`) and open no GTK windows unless stated; house style
   (stdlib first, optional dependencies imported lazily, Windows via `config.WINDOWS`/`FROZEN`,
   main thread never blocks on network/keyring); report file `dev/mN/round<k>-<topic>.md` (files,
   commands, results, deviations, needed shared-file edits); context updates from earlier rounds.
5. Launch both workers in one message; you are notified on completion. Never write watcher loops.
6. Review each diff yourself against the design and CLAUDE.md:
   - every edited existing test is a behavior change to justify;
   - look for reversals of documented rules (e.g. Jupyter code never auto-runs in `readonly`,
     no save while `keyring_problem` is set, `_close_open_calls()` before the next request,
     agent events reach GTK only via `GLib.idle_add`);
   - new files are in `install.sh`, the spec and (headless modules) `setup.py`; new icons exist
     in Adwaita; `install.ps1`/`uninstall.ps1` stay ASCII-only;
   - look at screenshots for UI work.
7. Commit in the worktree (`mN round k: <summary>`), merge into the milestone branch
   (`git merge --no-ff wt/mN-b`, then `wt/mN-a`), resolve conflicts (ownership workarounds →
   fold back into the owning module). Shared-file and CLAUDE.md updates as a separate commit.
8. Gate, **serially**:
   - `.venv/bin/python -m pytest tests/ -q`;
   - Chemotion instance up: `-m chemotion` (check it ran, not skipped for a version mismatch); Jupyter tests with
     `JUPYTER_TEST_BIN`;
   - GTK changes: run from source (`.venv/bin/python app.py --show`, or a test script with a
     stubbed `Agent.send` and throwaway paths per CLAUDE.md) and check a capture
     (`Gdk.pixbuf_get_from_window`);
   - Windows: only the CI build (`--self-test`) after pushing the milestone branch;
   - prompt/agent-behavior changes: propose an eval run (`evals/run_eval.py`, costs tokens);
     the owner decides.
   Fix cross-worker interaction bugs with one shared helper + regression test, committed separately.

## Finish

Push `milestone/mN-<slug>`, open the PR (behavior, work packages, verification commands and
results, screenshots, CLAUDE.md/installer notes), wait for the Windows build, get the owner's
OK, merge, mark the milestone `done (#PR)`, remove the worktrees
(`git worktree remove ../wt-a/ChemAssistent`, same for `wt-b`, branches deleted), report and stop.

## Pitfalls

- zsh does not word-split unquoted variables; `======` at line start is a zsh `=command` expansion.
- `pgrep -f "<pattern>"` in a loop matches its own shell — never use it as a watcher. Background
  processes must be tracked tasks, not `&`.
- WSLg: never open X11 GTK windows and type into them (hides the pointer until `wsl --shutdown`).
- Chemotion test data only in `AIExamples/ChemAssistent tests`, deleted again afterwards.
- The `.venv`/`workspace` symlinks show as untracked (`.gitignore`'s `.venv/` does not match a
  symlink): commit worker changes with explicit `git add <files>`, never `git add -A`.
- Scratchpad files vanish on reboot; keep decisions in `dev/` or `docs/milestones.md`.
- If a worker is blocked (quota, tooling), ask the owner whether to finish the round yourself and
  report it as an exception.
