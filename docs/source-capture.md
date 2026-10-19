# Bounded authority source capture

`intaris/source_capture.py` owns the shared source limits used by intention
profile generation, profile validation, task-context capture, and Judge citation
admission. `INTARIS_SOURCE_MAX_CHARS` defaults to **16000 characters**. Set it
before process startup; accepted values are 1–24000. Invalid configuration fails
startup rather than silently changing the evidence boundary.

The total captured source-body budget is **48000 characters**. Reserve the newest
user source and its latest-message alias first, up to the per-source cap. Allocate
the remainder newest-first; truncate older bodies rather than replacing them with
isolated saved quotes. Duplicate aliases count toward the total. Original
chronological presentation and source identifiers are preserved. Prompt metadata,
schemas and bounded previous selectors are additional overhead.

Generation presents exactly the bounded text later used for quote validation.
A quote must be a verbatim substring of that text. Truncated sources remain marked
truncated: a complete statement in the captured head may support a profile item,
but text beyond the cap or cut off at the boundary cannot. Judge citation admission
remains stricter: only complete, non-truncated user sources are citable. Increasing
the limit does not convert non-user sources into user authority.

This changes capture only, not inference scheduling: profile generation and
recovery remain asynchronous and evaluation adapts the persisted profile without
an additional inference call. Existing profile integrity and source comparisons
remain enforced; changed captured text can require normal profile regeneration.

Delegation provenance is unchanged. A client-submitted user-role assignment is
still a client claim, not independent proof that the owner authored it.
