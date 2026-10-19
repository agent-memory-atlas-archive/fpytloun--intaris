# Like-for-like static replay

`tools/static_comparison.py` reconstructs exactly the call IDs from the existing
private export. The baseline worker imports classifier and working-directory
resolution from a clean, detached worktree pinned to
`de115a5036966514c3ff5b0c13681b565ada5537`; it never imports the RC3 classifier.
Legacy READ decisions use the original static branch and request floor.
Every non-static call is counted as fake escalation, not a model approval.
The same redacted arguments, current session policy and context feed RC3.

The comparison deliberately does not reconstruct past path-approval caches or
human receipts, and the audit has no historical image/policy snapshot. It is a
classifier/fast-path comparison, not an HTTP replay. Current-policy snapshot
drift is held equal across both columns. Ten sample IDs per tool/reason and every
per-call result are retained privately; storage locators are omitted here.

de115a5 did not consume read-only hints from evaluate context. Its MCP preferences
were a separate evaluator argument, not stored in these rows. Named web tools,
conversation reads and the listed third-party MCP reads were not static in this
baseline; promoting arbitrary readOnlyHint/client metadata would be a new trust
contract, not a compatibility fix. Those tools remain unchanged.

## Canonical fix

`observe_hybrid` misinterpreted `_check_path_policy` returning `None`: this means
no override, including a path explicitly covered by `allow_paths`. Restore the
static read witness in that case, while preserving configured deny checks and
sensitive-path exclusion. The explicit observational metadata operation
`knowledgebase_list_artifacts` and inert shell observation commands also gain
effect proof; this does not promote mutable MCP infrastructure exemptions.

## Retained differences

Each retained difference is individually assigned a reason in the comparison:

- Existing Git helpers no longer constitute an exception. The accepted model
  is defined once in [Static Git trust boundary](git-static-trust.md).
- `unresolved_target_expansion`: glob/pathspec expansion cannot be bound safely
  to exact denied paths without executing against the target filesystem.
- `unverified_git_options_or_subcommand`: an unrecognized option/subcommand is
  not proof of absent writes/helpers (notably output flag abbreviations).
- `unresolved_shell_syntax`: substitutions, redirects or unsupported shell
  structure are not verified read-only.
- `script_filter_or_wrapper_requires_assessment`: awk/sed/jq/find/sort, shell
  wrappers and kubectl need option/program-aware proof, not a command-name grant.
- `disclosure_protection`: sensitive reads stay outside the static grant.
- `process_environment_or_unverified_ps_format`: environment-output and custom
  process formats are excluded; only explicitly supported `ps` argument forms
  receive static proof.
- `applicable_advisory_restriction`: a relevant configured restriction still
  requires assessment; unrelated prose does not.
- `explicit_human_checkpoint`: required confirmation is not waived by reads.

## Prepared paid replay — explicit approval required

Preparation:

```sh
uv run --extra dev --extra postgresql python tools/paid_write_replay.py prepare
```

Only production READ ONLY queries run. A deterministic stratified selection
chooses 300 legacy `evaluation_path=llm` approvals, round-robin across bash,
file-write tools and explicitly named MCP mutations. Small strata are exhausted
and their remaining allocation redistributed. The private file retains original
audit-redacted args, the audit intention when present, and user messages at or
before the call timestamp. Secrets removed by the original audit cannot be
recovered. No paid client is constructed by preparation.

After separate authorization, with `COGNIS_INFERENCE_URL` and
`COGNIS_INFERENCE_TOKEN_FILE` configured for the managed gateway:

```sh
uv run --extra dev --extra postgresql python tools/paid_write_replay.py run \
  --output /tmp/intaris-rc3-replay/paid --approve-paid
```

This runs RC3 balanced in an isolated temporary SQLite database using
`routes/intaris_evaluation`, `routes/intaris_analysis` and
`routes/intaris_judge`. It does not dispatch tools or mutate production sessions.
The runner refuses execution if `EVALUATION_POLICY_FORCE` is set.
Per-call outcomes and agreement counts are private local outputs. Provider errors
are reported separately, never counted as agreement. The runner is exercised only
with mocked clients in tests; the real 300-call run has **not** been executed.

`paid-estimate.json` uses the measured L20 action cost ($0.000412, including
Judge), plus 120B profile-generation estimates at assumed $0.15/M input and
$0.60/M output. Actual sample text is estimated at four characters/token, with
mean/p50/p90/max reported, not represented as exact tokenizer counts.
One profile per original session and the current isolated runner's one profile
per sample are priced separately. The conditional upper envelope reserves four
stages, twice estimated input, schema overhead, maximum outputs and three
attempts; it is not a billing cap if managed routes/rates change. Use the
`estimate` subcommand to recalculate locally with no production read or inference.
All input/report files are mode 0600 under a 0700 directory, never committed.
