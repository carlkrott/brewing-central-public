# Brewing Central — current work ledger

This is the finite handoff for the phone dashboard work. It prevents intentional idle states and deferred operator actions from being reopened as recurring defects.

This public ledger summarizes the supported source state. Private planning
records and operational receipts are intentionally not part of the public
repository. Source and test status is represented by the repository workflows
and the checked-in public documentation.

## Status categories

- Complete: verified in source/tests or live evidence named in the plan.
- Next: the current bounded implementation slice.
- Intentionally disabled: expected not to run unless its policy is enabled.
- Operator-deferred: requires a physical or explicit operator action; not an application bug.
- Blocked: named prerequisite is missing.
- Optional: useful, but not required for the core dashboard.

## Work ledger

| Area | Status | Reopen condition |
|---|---|---|
| Phone dashboard ingress and telemetry history | Complete / maintain | Reopen only on a reproducible regression in the supported phone path. |
| Recipe data model, scaling and snapshots | Complete / maintain | Reopen only on a failing preservation or scaling test. |
| Assistant response contract repairs | Complete / source-tested | Reopen only for a reproducible contract failure or a required user workflow gap. Live model qualification remains separate. |
| Sensor operating intent | Complete / source-tested | Reopen only on a failing `stored`, `preparing`, or `brewing` policy test. |
| Camera structural observation | Intentionally disabled when policy is off or no brew is active | Reopen only when structural monitoring is explicitly enabled or a required observation is missing. No dummy brew. |
| Physical iSpindel power | Operator-deferred | Operator chooses `preparing` and powers the device using its installed hardware. Expected-interval settings are not a power command. |
| Water reference and calibration activation | Operator-deferred | Operator supplies and reviews a stable water reference before explicit activation. |
| Recipe staged AI workflow | Complete / source-tested | Reopen on a failing review → approval → rewrite → diff → apply → save → reload contract or browser journey. |
| Archive review/compare/fork workflow | Complete / source-tested | Reopen on a failing frozen-evidence, append-only annotation, comparison, or new-draft lineage test. |
| Local research evidence QC | Complete / source-tested | Reopen if fresh retrieval, versioned evidence, truthful empty/unavailable status, or second-request local reuse regresses. Live broker/model qualification remains separate. |
| Phone health and off-host backup/restore | Source-complete / qualification deferred | Reopen source on a failing evidence, paired-backup, restore, FTS, or backup-health test. Live phone installation, off-host promotion and paired Docker rehearsal remain explicit qualification gates. |
| Integrated qualification and candidate preparation | Source-complete / live qualification pending | Offline qualification tooling is source-tested. `tests/live/test_assistant_pipeline_live.py` measures domain writes and carries the research flag; `tests/live/test_supported_origin_live.py` is read-only and credential-file gated. G1 read-only HTTPS smoke, G2 isolated live-model/research qualification, and G4 clean-tree build/activation are not executed. |
| Production release activation | Blocked by explicit authorization | Only after G4 produces a frozen candidate, the live gates pass, and a separate impact-aware activation instruction. Activation is not performed. |

## Non-negotiable boundaries

- `ispindel.db` telemetry schema remains unchanged by brewing workflow work.
- Historical brew snapshots and terminal events are immutable.
- Model output never creates trusted operator provenance or deterministic findings.
- Empty/unavailable research is reported honestly; HTTP 200 alone is not success.
- Camera scope is structural safety only: container presence/uprightness/displacement, visible spill/overflow/damage and view quality. It does not infer liquid, fermentation, gravity, pH, ABV or food safety.
- A stored sensor and a camera that is not required are accepted operating states, not stale work items.
- No commit, candidate build, descriptor mutation, phone deployment, service restart, live broker/model call, or physical device action is implied by source implementation work. P4 qualification tooling is source-complete; live gates G1–G4 are not executed.

## Candidate hygiene

Keep at most one isolated candidate when live qualification is explicitly authorized. Candidate databases and camera/configuration paths must be isolated. Obsolete candidate processes are cleaned up only by an explicitly authorized, exact-identity operation; they are not additional deliverables.
