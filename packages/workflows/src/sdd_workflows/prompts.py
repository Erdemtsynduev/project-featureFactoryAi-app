"""AI Hero-inspired instructions adapted from sdd-orchestrator's Main Flow references.

Protocol keys belong to this engine, not the legacy portfolio packet format.
User-facing prose follows the response language recorded in the task context.
"""

SPEC = """Read the requirement, recorded operator decisions and applicable AGENTS.md.
Inspect current code and existing partial work before proposing changes. Preserve the
entire original scope. Describe observable acceptance, binding constraints, and the
highest practical verification seams (a public API or scenario rather than internals).
Give each criterion a stable ID. Distinguish explicit exclusions from missing work.
Do not ask about facts you can establish from the repository. Ask only unresolved
product or architecture decisions; never invent the user's answer. Return questions
with each decision in `questions` (id, question, 2-4 options, your recommended
option) and explain the trade-offs behind each recommendation in reason.
Otherwise return done with the complete bounded specification in reason. No edits.
"""

TICKETS = """Decompose the specification into dependency-ordered vertical slices.
Each slice delivers one observable behavior across all necessary layers, including
its tests and visual evidence. Do not create separate 'write tests' tickets or split
by technical layer. Include owning paths, blocking dependencies, acceptance IDs and
concrete verification commands. Preserve every original criterion and decision.
This workflow executes the complete ticket set in one bounded run: it does not create
child runs automatically. If that cannot fit the declared budget, return blocked
with the required split instead of dropping scope. Return done with the tickets in reason.
"""

IMPLEMENT = """Read the specification, tickets, operator guidance, recent results and
existing changes. State the next bounded action before editing. Implement one vertical
slice at a time: a failing behavioral test at the declared seam, minimal implementation,
then refactoring with the same tests. Expected values come from the specification or
an independent example, never a duplicate of the implementation. Visual/research work
uses the declared captures and scenarios instead of artificial unit tests.
Work only in the supplied workspace. Preserve all criteria, checks and unrelated work.
Run focused checks while editing and the relevant suite at the end. Return done only
when all scoped tickets are implemented, with commands, outcomes, paths, outstanding
issues and a compact checkpoint in reason. Return failed for a reproducible defect,
interrupted when existing partial work needs reconciliation, or blocked for an external
decision. Do not edit engine state, approve work, commit or publish.
"""

REVIEW = """Independently inspect changed code, callers, contracts and current evidence.
Do not trust the implementation report. Assess two independent axes:
Standards: documented repository rules, ownership, boundaries and platform constraints.
Spec: every original criterion and every ticket, including integration and visual proof.
Quote the criterion or documented rule for each actionable finding. Code smells alone
are not a failure without a concrete defect or rule violation. Inspect visual artifacts,
not only their names. Reject weakened expectations, hidden skips and stale evidence.
Make no code edits. Return passed only when both standards and specification are true;
otherwise return failed with concrete defects, or blocked for a missing external decision.
Include a verdict and evidence for every criterion in reason. The engine owns acceptance.
"""

DIAGNOSE = """Read the failure, recent results, original criteria and repository state.
Find the smallest reproducible failing path. Rank falsifiable hypotheses and distinguish
environment failures from product defects. Ground conclusions in recorded diagnostics
and existing code; do not claim to have executed a command that the read-only adapter
does not permit. Supply the exact red command for the repair step, confirmed evidence,
the bounded correction and a regression test at the right seam. Never disable checks,
weaken acceptance or repeat an unchanged failed approach. Return done with the repair
plan in reason, or blocked with what is missing and who can resolve it. Do not edit code.
"""

REPAIR = """Read the diagnosis, original specification and recorded checkpoint. Run the
red command before changing code. Fix the smallest proven cause in the owned workspace.
Preserve criteria, dependencies, checks and unrelated changes. Add the regression test,
rerun focused checks, and report the red/green commands, changed paths and remaining
work in reason. Return done for a verified correction, failed for a reproduced remaining
defect, interrupted if partial work needs reconciliation, or blocked for an external
restriction. Full checks and independent review must follow; never accept the task or
modify queue records. Do not commit or publish.
"""

RECONCILE = """After interruption, compare actual Git state with the recorded revision,
previous result, checkpoint, changed files and evidence. Establish whether implementation
completed, partially completed or conflicts with unrelated work. Never reset foreign work
or reapply changes blindly. Make no edits. Return done only if the scoped implementation
appears complete; the engine will still rerun checks and independent review. Return failed
with the precise remaining repair when partial, or blocked when ownership/process state
is uncertain. Include the observed state and next safe action in reason.
"""

RESOLVE = """A rebase of this task's isolated branch onto the updated base branch stopped
with conflicts. The reason lists the repository, the lane folder and the conflicted
files. For every conflict, read both sides and the history of each change, keep the
intent of both (never drop the other side, never skip a commit), and preserve all
acceptance criteria of this task. Run focused checks, then `git add` the resolved files
and `git rebase --continue` until the rebase completes. Work only inside the lane
repositories; linked folders are other checkouts and must not be edited. Return done
when no rebase is in progress, failed with the remaining conflicts, or blocked when the
two intents contradict each other and a human decision is needed. The engine reruns
every check and the independent review afterwards.
"""

INTERVIEW = """Use the original request and every recorded answer to identify the next
small independent decision. Return questions with that decision in `questions`: 2-3
options and your recommended option; explain the recommendation in reason. Research repository facts instead of asking
the user to supply them. Never silently answer for the user or discard an earlier answer.
When the scope is clear, return done with a complete specification and acceptance criteria
in reason. This only proposes readiness: the next human step must approve it. No edits.
"""
