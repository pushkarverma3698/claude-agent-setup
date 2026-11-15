---
name: agy-loop
description: Use when orchestrating Google Antigravity (agy) from Claude Code in an Architect -> Doer -> Adversarial Reviewer loop, especially for heavy multi-file tasks, refactors, migrations, or when offloading implementation and test-fix loops from Claude's context/quota.
---

# Brain/Doer Closed Loop (`agy-loop`)

Claude is the **Brain** (Architect + Adversarial Reviewer via `pr-adversary`). Antigravity is the **Doer** (`agy`), executing inside an isolated git worktree, running the free gate (`pnpm gate` / test suites) and real product probes, and opening a draft PR.

This loop isolates terminal/compiler noise inside Antigravity's context window while keeping Claude's context clean for architecture and adversarial verification.

```text
CLAUDE (Architect)  → claims branch · cuts .claude/worktrees/<slug> · verifies brief facts
   ↓  agy new --wait --model=pro "<brief>"
ANTIGRAVITY (Doer)  → failing test (Red) · implementation (Green) · gate · Draft PR + Close-Out
   ↓
CLAUDE (Adversary)  → agy-guard --conv <id> · pr-adversary disproof pass on worktree/PR
   ├─ PASS                  → gh pr ready · merge (if authorized) · remove worktree
   ├─ BLOCKER (<=15 lines)  → Claude fixes directly on branch · re-runs gate · pushes
   └─ BLOCKER (>15 lines)   → Cycle 2: agy send --wait <id> "<falsifiable BLOCKER brief>"
                                 ↓
                              ANTIGRAVITY fixes named BLOCKERs in same conversation (80%+ cache hit)
                                 ↓
                              CLAUDE re-verifies once → PASS or HARD STOP (escalate / tracker block)
```

---

## 1. When to use vs. skip

- **Use `agy-loop`:** Heavy tasks (schema migrations, multi-file features, third-party APIs, refactors across 3+ files, test-heavy modules) or when conserving Claude Pro usage quota.
- **Skip `agy-loop`:** Single-file < 20-line edits, config tweaks, or docs — do those directly.
- **Milestone slicing (> 400 LOC):** Never hand off a monolithic 1,000-line rewrite in one shot. Break heavy work into ordered slices (~400 LOC max per PR: e.g., Slice 1: schema + queries; Slice 2: service + routes) and run one loop per slice.

---

## 2. Step 1 — Pre-Dispatch Grounding & Worktree Setup (Claude)

Six of ten historical delegation defects came from flaws in Claude's brief, not Antigravity's code. Before dispatching:

1. **Verify target facts (`Checked first:`):**
   - Grep for existing functions, resolvers, types, and tests in the target module.
   - Measure any baseline counts or line numbers via commands — never write a literal from memory.
2. **Claim the task/branch first:**
   - Tracker repo: `tracker claim T-NNN` (or `tracker claim --project <name> '#<N>'`).
   - Other repo: `claim-branch <owner>/<repo> <type>/<slug> <base>` (`--resume` for a leftover).
   - Exit 3 = another session holds it; stop and pick other work.
3. **Cut an isolated git worktree (unless `tracker claim` already created one):**
   ```bash
   git -C <repo> fetch -q origin
   git -C <repo> worktree add -b <type>/<slug> <repo>/.claude/worktrees/<slug> origin/<type>/<slug>
   ```
   Antigravity works **exclusively** inside `<repo>/.claude/worktrees/<slug>`, never in the main checkout.

---

## 3. Step 2 — Dispatch Turn 1 (`agy new --wait`)

Write a self-contained brief (aim for <= 35 lines) containing all 6 sections:

```text
Use the delegated-task and doer-contract skills.

1. Worktree & Branch:
   - Work ONLY inside: <absolute_path_to_worktree>
   - Branch: <type>/<slug> (base: <base>, claim: <claim_sha>)
   - Issue / Task: <Fixes #N or Closes T-NNN>

2. Outcome & Acceptance Criteria (Moves: <outcome>):
   - <Criterion 1 — falsifiable behaviour>
   - <Criterion 2 — falsifiable behaviour>

3. Checked first (Reuse — do not rebuild):
   - <existing file:line / function / pattern to follow>

4. Forbidden (Non-goals):
   - Do not touch <adjacent files/modules>. Keep diff under 400 LOC.
   - No `any`, `@ts-ignore`, `eslint-disable`, or unrequested abstractions.

5. Verification:
   - Write the failing test first (Red), then implement (Green).
   - Run: <exact gate command, e.g. pnpm gate>
   - Live probe: <exact probe command, or "none required — covered by gate">

6. Handoff:
   - Stage explicit paths (`git add <paths>`), commit, `git push -u origin <type>/<slug>`,
     and open a DRAFT PR to `<base>` with sections: Moves / Checked first / What changed /
     How it was verified / NOT VERIFIED.
   - Output the mandatory Close-Out Report with raw verify output, then STOP.
```

Run in the background (`run_in_background: true` on Claude's `Bash` tool so Claude wakes reactively when Turn 1 finishes without polling):

```bash
AGY_ALLOW_PARALLEL=1 ~/.local/bin/agy new --wait --model=pro --title="<type>/<slug>" "<brief>"
```

Record `CONVERSATION_ID=<uuid>` from line 1 of the output.

---

## 4. Step 3 — Adversarial Review Gate (`pr-adversary`, Cycle 1)

When `agy new --wait` returns:

1. **Confirm the conversation is idle:**
   ```bash
   ~/.local/bin/agy-guard --conv "$CONVERSATION_ID" 5
   ```
2. **Invoke `pr-adversary` on the worktree and Draft PR:**
   - Inspect `git -C <worktree> status -sb` and `git -C <worktree> diff origin/<base>...HEAD`.
   - Check every acceptance criterion against actual hunks (`ON-TARGET` / `PARTIAL` / `OFF-TARGET`).
   - **Red-then-green proof:** In `<worktree>`, checkout `origin/<base> -- <non-test src files>`, run the new test file and confirm it **fails** (catches circular/tautological tests); then `git checkout HEAD -- <non-test src files>` and confirm it **passes**.
   - Run the gate command independently.
   - Classify findings into `PASS`, `NON-BLOCKER`, `BLOCKER (<= 15 lines mechanical)`, or `BLOCKER (Substantive)`.

---

## 5. Step 4 — Verdict Routing & Cycle 2 Loop (`agy send --wait`)

- **Route A (`PASS`):**
  - Clear the PR per `pr-adversary` (`gh pr ready <PR>`, post gate evidence comment).
  - Merge if the founder or job contract authorizes it, then `git worktree remove <worktree>`.

- **Route B (`BLOCKER <= 15 lines`, mechanical fix):**
  - Apply the surgical fix directly in `<worktree>`, re-run the gate, commit, `git push`, and proceed to Route A.

- **Route C (`BLOCKER > 15 lines`, logic/test/contract gap — Cycle 2):**
  - Send a falsifiable revision brief to the **same** `CONVERSATION_ID` via `agy send --wait` (reuses >80% prompt cache and the agent's full file context):
    ```bash
    ~/.local/bin/agy send --wait "$CONVERSATION_ID" "$(cat <<'EOF'
    Cycle 2 Adversarial Review — BLOCKER findings on <branch> (worktree: <worktree>):

    1. BLOCKER [<file:line>]: <one-sentence statement of defect>
       - Failure scenario: <concrete input/state -> wrong output>
       - Required proof: <exact test case or command that must fail before and pass after>

    Instructions:
    - Run `git -C <worktree> pull --ff-only` first.
    - Fix ONLY the named BLOCKER(s) inside `<worktree>`. Do not refactor unrelated code.
    - Re-run `<verify_cmd>` and paste raw output.
    - Stage explicit paths, commit, and `git push` to `<branch>` (do not open a new PR).
    - Reply with the Cycle 2 Close-Out Report and STOP.
    EOF
    )"
    ```
  - When `agy send --wait` returns, re-verify the new commit once in `<worktree>`.

- **Hard Circuit Breaker (Max 2 Cycles):**
  - Never run a Cycle 3. If a BLOCKER remains after Cycle 2, stop the loop: either push the remaining fix directly or mark the task blocked (`tracker block ... --needs founder`) with the exact evidence.
