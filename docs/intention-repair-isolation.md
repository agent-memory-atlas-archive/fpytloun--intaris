# Intention repair isolation

Internal profile-validation feedback is part of the generation instructions,
not a new user message. A bounded repair keeps the original user/source payload
unchanged so the latest source message remains the latest actual task.

If the resulting profile is unavailable, Intaris preserves the previous title,
intention and intention source, and clears the derived profile using the existing
state-revision and user-message freshness guards. New raw instructions remain
available independently; preserving a summary does not preserve stale grants.
Successful extraction can still update the intention normally.

Parent alignment is unchanged. This prevents a failed JSON repair from replacing
a valid child assignment with a task such as “Regenerate JSON.” It does not
retroactively rewrite already-corrupted sessions or approve their blocked calls.
Those sessions need source-based regeneration and normal alignment checking.

Regression coverage includes unchanged source payloads, successful repair,
failed repair, new raw restrictions with old grants cleared, and a concurrent
generation whose newer profile must not be cleared.
