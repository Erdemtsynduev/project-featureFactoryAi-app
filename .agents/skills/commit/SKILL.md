---
name: commit
description: Create reviewed Git commits in Feature Factory AI when asked to commit or publish changes. Check staged scope, run relevant verification, enforce Conventional Commit messages without tool attribution, and push only when the user explicitly requests it.
---

# Commit

This repository is one Git repository containing independently packaged Python
libraries. Read root AGENTS.md and preserve the user's current scope. Do not copy
unrelated workspace/submodule rules into this project.

1. Inspect `git status --short`, staged/unstaged diffs, branch, remote and recent
   history. Preserve unrelated changes and existing staging. A request to commit
   the completed task authorizes its owned files; do not ask for that scope again.
2. Finish the task's relevant checks. For changes to runtime contracts or package
   installation use `uv run --locked python tools/qualify.py --build`. Documentation
   changes need accurate links and examples, not a new endurance run. Never start
   a model call or a long soak merely to make a commit.
3. Stage explicit paths with `git add -- <paths>`. Do not stage reports, databases,
   credentials, virtual environments, build outputs or unrelated user changes.
   Review `git diff --cached --stat`, `git diff --cached --check` and the actual
   staged content. Inspect suspicious files without printing secret values.
4. Use Conventional Commits: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`,
   `build`, `ci`, `chore`, `style` or `revert`; optional scope and breaking `!`.
   Keep the subject within 72 characters, without a trailing period. Split unrelated
   changes; describe the outcome, not the conversation or who produced it.
5. Write the exact message to a scratch file under ignored `reports/`, then run
   `uv run --locked python tools/check_commit_message.py --file <file>`.
   No `Co-Authored-By`, generated/assisted-by text, bot signatures or other
   tool attribution. Provider names may describe an actual feature, not authorship.
6. Commit with `git commit -F <file>` using configured Git identity. Do not override
   author, bypass hooks, amend or rewrite history unless explicitly requested.
   Review an in-scope hook failure, fix it and retry; do not use `--no-verify`.
7. A local commit request does not authorize a push. When push is explicitly
   requested, verify the exact remote/branch and use a normal push. Preserve remote
   history; never force-push to solve a rejection. Report a genuine access/history
   blocker while preserving the completed local commit.
8. Verify the commit message, working-tree status and, when pushed, remote HEAD.
   Report commit hashes, checks and any remaining changes or qualification limits.

Run from the repository root. This skill and its message checker are owned here;
no absolute workstation paths or external workspace scripts are required.
