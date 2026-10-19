# Chat labels are not execution restrictions

`chat_mode=plan` does not imply `read_only_required`. Effect classification is
unchanged: an opaque command may remain uncertain, and a supporting file write
remains a write. Normal policy and Judge review assess whether the action fits
the user's task; the evaluator adds no planning-specific outcome or command
allowlist.

Explicit request or session `read_only_required=true` remains a hard ceiling in
every chat mode. A request cannot relax a session-level restriction. Captured
approval identities remain mode-sensitive, so a transition cannot borrow an
approval from another context.

Older clients that explicitly send the read-only flag still request that
restriction. Update Cognis too so it stops generating the flag from plan mode.
