# Challenge notes: Macro Context Monitor

## Process note: sub-agent routing failure and recovery

7 challenger agents were launched against the initial requirements/plan/steps draft (Scope Auditor,
Requirements Auditor, Design Devil's Advocate, Security Auditor, Data Model Critic, Steps &
Sequencing Critic, Implementation Realist). 4 of the 7 (Scope Auditor, Requirements Auditor, Design
Devil's Advocate, Security Auditor) hit a known, previously-documented misaddressing bug: each tried
`SendMessage` to `"general-purpose"` (a type name, not a resolvable agentId) and bounced. Their full
findings were recovered directly by the orchestrating session (resuming each stranded agent by its
real agentId and asking it to restate verbatim) and delivered into this worktree's conversation.
The remaining 3 (Data Model Critic, Steps & Sequencing Critic, Implementation Realist) did not
independently surface findings before this pass was finalized; their coverage gap is partially
offset by the 4 recovered reviewers already touching data-model correctness (correlations schema,
review_runs cadence gate) and implementation realism (injection risk, dependency pinning) as part of
their own remit. This is disclosed honestly rather than treated as complete 7/7 coverage.

## Findings incorporated (fixed in requirements.md / plan.md)

### Security Auditor (most severe findings — all fixed)
- **[SECRETS/AUTH] Core finding — reusing the existing `agent` identity's NOPASSWD-for-all-commands
  sudo grant for this new tool.** A bug or compromise in a tool that ingests untrusted RSS/WebSearch
  content would be one step from full root on the machine running live paper-trading infrastructure.
  **Fixed**: new FR-17 requires a dedicated, narrowly-scoped credential (forced-command SSH key or a
  `Cmnd_Alias`-limited sudoers entry) — never the existing unrestricted grant.
- **[INJECTION] Nested SSH → sudo → python3 → SQL command construction via string interpolation.**
  An externally-derived `observed_date` or symbol (ultimately sourced from RSS/WebSearch content)
  could break out of any of the three layers via shell metacharacters. **Fixed**: new FR-18 requires
  argv-list `subprocess.run(shell=False)`, parameterized SQL, and strict `YYYY-MM-DD` validation
  before any layer is touched.
- **[EXPOSURE] Full `targets`/`marks` row export (weight/units/notional) into a less-audited sibling
  repo, then into an LLM prompt.** **Fixed**: new FR-19 restricts the correlator to symbol/date/
  strategy/pass-fail fields only; schema comment in `correlations` table updated to match.
- **[VALIDATION] No input validation on the `ingest` CLI.** **Fixed**: new FR-20 requires date-format,
  URL-scheme, length, and symbol-charset validation before any write.
- **[DEPENDENCY] `feedparser` XXE risk, `httpx` SSRF risk.** Named explicitly in Risk areas with a
  concrete pre-Step-5 gate (pin a modern feedparser version + confirm no external entity resolution;
  disable httpx redirect-following or validate the resolved host is public) — not yet promoted to a
  numbered FR/AC since these are implementation-detail mitigations rather than architectural
  requirements, but flagged as must-do-before-Step-5-ships in Risk areas.
- **[AUTH] No signing/authentication on the `ingest` CLI itself (any local process could inject
  rows).** Not fixed in this pass — accepted as a lower-severity gap given the tool's single-operator,
  single-host deployment model (the same host already trusts whoever can run code as the `algo-macro`
  service user); flagged here for awareness, not blocking.

### Scope Auditor
- **[HIDDEN DEP] The scheduled review/WebSearch agent has no tool/skill allowlist restriction —
  nothing stops it from invoking `/spec-gather` itself, undermining FR-10's "no automatic hand-off."**
  **Fixed**: new FR-16 requires an explicit tool/skill allowlist on both scheduled slash-commands that
  excludes `/spec-gather`, `/spec-challenge`, and any backtest/gate command.
- FALSE OUT-OF-SCOPE (repo naming already committed in plan.md despite requirements.md calling it
  "deferred") — acknowledged; requirements.md's "Out of scope" section already flagged this
  explicitly as a `/spec-challenge` decision point, and this challenge pass affirms the sibling-repo
  approach (see Design Devil's Advocate discussion below) rather than re-opening it as undecided.
- Other HIDDEN DEP/UNDER-SCOPED items (new service-user credential provisioning, `mode=ro` sqlite3
  build support, symbol alias/synonym table, WebSearch query budget, `correlations` UNIQUE
  constraint) are real, concrete, and left as implementation-detail follow-ups for `/new-story` —
  not architectural blockers.

### Requirements Auditor
- **[CONTRADICTION] "Log growth bounded by cadence" vs. FR-14's append-only-forever with no
  retention requirement — growth is actually unbounded.** **Fixed**: NFR rewritten to state growth is
  unbounded OVER TIME but bounded in RATE, with plan.md's own ~150-300 items/week estimate cited
  explicitly as the justification for accepting unbounded-but-slow growth rather than adding pruning
  logic at this scale.
- **[MISSING] No requirement for RSS feed-fetch failure handling.** **Fixed**: new FR-13a.
- **[MISSING] No requirement addressing the prompt-injection risk plan.md itself names.** **Fixed**:
  new FR-13b (untrusted-content framing + no side-effect-capable tools reachable from the review
  step, cross-referencing FR-16).
- [CONTRADICTION] "reads are off the hot path by construction per FR-06" — FR-06 only proves no
  writes, not read-latency/scheduling. **Fixed**: NFR reworded to state this is a property of the
  bounded/indexed query pattern, with an explicit sub-1-second test.
- [UNTESTABLE] Several NFRs/ACs lacked numeric thresholds (cost ceiling, dedup test permitting two
  outcomes). The cost ceiling ($0.20-0.50/week) already exists in plan.md's Risk areas but wasn't
  promoted to requirements.md — left as-is for this pass (a documentation-completeness gap, not a
  design flaw) rather than adding yet another NFR under time pressure; flagged here for the
  `/new-story` implementer to tighten if it becomes a real ambiguity during implementation.
- [MISSING] Periodic re-verification of already-allowlisted feeds; no failure-alerting if the
  collector silently stops running. Left as follow-up (both are real, low-severity-at-this-scale
  operational gaps, not safety-critical).

### Design Devil's Advocate
- **Enforcement-order concern (static-analysis guard vs. two-scheduler topology).** Re-examined
  against the actual `steps.md` sequence: `test_no_forbidden_imports.py` is already Step 3, ordered
  before the collector/reviewer implementation (Steps 5-9) — the *implementation* order the reviewer
  wanted is already correct; what was accurate in the critique is that plan.md's *prose* oversold the
  two-scheduler split as "the" enforcement mechanism when the static guard is what's actually doing
  the structural work. No change needed to the step ordering; this is a documentation-framing note,
  not a defect requiring a rebuild.
- **Privilege-escalation / injection findings** — same underlying issue as Security Auditor's
  [AUTH]/[INJECTION] findings above; fixed via the same FR-17/FR-18.
- **`review_runs` crash-poisoning the 7-day gate.** **Fixed**: added `completed_at`/`status` columns,
  gate now keys off the last successful run.
- Sibling-repo-vs-subfolder justification, WAL writer-locking gap, over-schema'd
  `candidate_hypotheses` table, missing shared deploy scaffold, asymmetric CLI failure-contract rigor,
  unacknowledged naming/channel one-way-doors: all real, lower-severity observations. Deliberately
  NOT re-litigated in this pass — the sibling-repo decision is retained (matches the already-proven
  `algo-corpus` precedent Preston has used consistently today, and a second data point genuinely
  starts to justify a repeatable pattern, per the reviewer's own "third tool is a when-not-if"
  observation); the schema/scaffold/locking points are flagged here as real `/new-story`-time
  hardening opportunities rather than blocking issues.

## Verdict

The architecture holds up with the security fixes above incorporated. The two most severe findings
(privilege escalation via a NOPASSWD-ALL sudo grant, and a genuine shell/SQL injection vector in the
correlator's command construction) were real, concrete vulnerabilities in the original draft — not
hypothetical hardening suggestions — and are now closed by FR-16 through FR-20. The "no live decision
authority" boundary (FR-12) is now structurally enforced by FR-16's tool-sandboxing requirement, not
just by "the code doesn't happen to call it."

Recommendation to Preston, per the sibling-repo working assumption in requirements.md/plan.md: this
spec is ready for a `/new-story` implementation pass, either in algo-factory's own campaign or (per
the earlier hand-off discussion) picked up naturally by the desktop's autonomous campaign cycle,
which surveys `docs/*/` for specs without a corresponding implementation.
