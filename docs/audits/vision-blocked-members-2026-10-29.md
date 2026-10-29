# Vision blocked members and transient cleanup

Wave `a24451515ecd4fdb98096f981b4c8de5` admitted 984 members. Vision processed and settled 968 with no source errors in approximately 843 seconds of productive stage wall time. Sixteen members remained pending without a source attempt. Subsequent passes repeatedly waited at the barrier.

At 23:45:53 UTC on October 28, evaluation raised `ArtifactCleanupBusy` from the policy guard. That transient wait consumed a dispatch attempt; subsequent coordination conflicts exhausted the dispatch budget at 23:53:17 UTC. Vision later scaled to zero. Sleeping was a consequence of the stalled release, not its original cause.

At inspection time on October 29, all sixteen pending filenames referred to different photo/revision identities, each with durable replacement admissions. These later replacements do not establish why the original members were first skipped. The original telemetry did not record the silent skip reason.

## Change

When selection cannot qualify a source and returns no source row, Vision now performs a fresh authoritative check. Durable deletion/replacement proof retains its existing explicit settlement behavior. Otherwise, it records a bounded reason, identity fingerprint, check count, and first observation time on the pending member. Missing metadata, unqualified identity changes, isolation, and obsolete contracts must persist for at least three checks and five minutes before receiving an explicit `failed_exhausted` settlement proof. A changed fingerprint starts a fresh grace period.

Infrastructure read errors remain errors, not evidence of a missing source. A live source owner prevents failure settlement. Source recovery, replacement proof, and ownership are checked again after coordination writes. The member is never marked successfully processed, and no replacement source is processed on behalf of the old member. Existing verified settlements are respected and frozen inputs remain immutable.

`vision_member_blocked` telemetry includes the wave, bounded reason, check count, and age; normal image scope supplies the hashed member identity.

Transient artifact cleanup now refunds the current dispatch attempt and records a durable 30-second delayed continuation using the existing role queue. `wave_cleanup_wait` reports the stage and delay. This applies to Vision, clustering, and indexing consumers. Destructive library cleanup and ordinary coordination failures retain their existing fences and failure budgets.

The existing wave evaluator can freeze with verified failed members, and clustering ignores their unavailable face outputs. No successful output is invented to cross the barrier.

## Rollout

Deploy the build to the actual stage worker containers, including `index-maintenance`. The already exhausted Vision dispatch requires a fresh normal WorkStore request and wake after deployment; this code does not silently reset exhausted work. No live retry or deployment was performed while developing this fix.
