"""Explicit settlement for a frozen wave member whose source generation has
since been superseded -- see docs/indexing-clustering-target-state.md step 5
("Handle supersession without stranding an older wave") and
processing_generation.py.

A frozen wave captures one member's (photoId, processingRevision) pair at
freeze time (upload_waves.py's member row). If that photo is later forced-
reprocessed or its model contract moves on, the row's live generation no
longer matches what the wave captured. The wave must not silently retarget
itself to the NEW output (the frozen manifest is immutable by design -- see
upload_waves.py's "Frozen revision is immutable" comment), and it must not
block forever waiting for processing that is itself now blocked behind this
same older wave (the new revision belongs to a successor wave, not this
one). This module gives that situation an explicit, named outcome instead
of leaving it to either silently pass or silently hang.

No real wave-completion evaluator calls this yet -- see upload_wave_
execution.py's and wave_publication.py's module docstrings on the
equivalent scope limits for steps 4 and 6. This is a standalone, tested
primitive for that future evaluator to use.
"""
from processing_generation import is_superseded


def member_outcome(member, row, *, is_video):
    """Returns one of:

    - 'valid': the row's live generation still matches what this member
      captured. Its original immutable output (artifact manifest, face
      snapshots) is still the right evidence for this wave.
    - 'missing': the row no longer exists (hard-deleted) or carries no
      source identity at all. There is nothing this wave can settle with;
      an operator must resolve it explicitly (see processing_cleanup.py's
      caveat on fencing active writers during deletion).
    - 'superseded': the row's generation has moved on (forced reprocess, or
      a live model/policy contract bump) since this wave froze. This
      member's captured revision is stale; this wave must settle with its
      ORIGINAL output (if still durably retained) or record an explicit
      supersession outcome -- it must never adopt the new generation's
      output as if it were a continuation of the same member, and it must
      not block waiting for the new generation's processing, which belongs
      to a successor wave, not this one.
    """
    if row is None:
        return 'missing'
    if row.get('processingRevision') != member.get('processingRevision'):
        return 'superseded'
    if is_superseded(row, is_video=is_video):
        return 'superseded'
    return 'valid'


def settlement_requires_original_output(outcome):
    """True when `outcome` needs the member's ORIGINAL immutable artifacts
    (not a fresh capture) to settle -- i.e. 'valid'. A 'superseded' outcome
    needs either that same original output (if still retained) or an
    explicit recorded supersession/deletion outcome; it is a release
    decision which of those a given deployment requires, not something
    this primitive can decide on its own."""
    return outcome == 'valid'
