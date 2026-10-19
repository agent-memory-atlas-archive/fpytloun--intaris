# Optional rich context and recent action continuity

Balanced and bounded autonomy use captured raw sources when rich extraction is
missing or empty. The raw view retains session intention, original assignment,
verbatim user messages, chronology, restrictions and provenance. Generated
summaries are not explicit grants. No permission is granted merely by choosing
this view. Strict retains unavailable-rich behavior. The existing mixed view
remains in use when no rich user evidence is expected.

Hybrid L1 previously replaced the legacy prompt with a rich projection and lost
recent tool activity. A shared owner now captures at most fifteen prior attempted
actions, with tool and redacted target/command only, within 2000 JSON characters.
This frozen view reaches both L1 and Judge; it contains no outputs, permission or
proof that a tool executed. It is not added to user citation sources or source
freshness watermarks. Queries are tenant/session scoped.

The managed `routes/intaris_judge` route now starts with JSON object output,
matching observed provider behavior. Other routes retain strict schema. Local
strict Pydantic validation gets one repair, followed by existing citation,
operator-policy and external-effect checks. Exhausted validation/transport
failure leaves the action unresolved with an assessment-failure explanation,
not an adverse model judgment or internal policy label.

## Extraction and diagnostics

The previous nineteen unreadable stored profiles were NULL, not corrupted
integrity hashes or changed intentions. A generated title/intention can update
successfully while the rich envelope is rejected and stored as NULL. Historical
raw completions and envelope validation subtypes were not retained, so attributing
those nineteen to specific fields would be speculation.

The profile prompt now explicitly names the exact item keys, one-based source
numbers, finite statuses, original-language quotes and limits. Its schema is
derived from the validator. The previous 89 `invalid_item_schema` diagnostics did
not identify the failed field and therefore do not prove a particular schema
mismatch. Safe field/type diagnostics now do, without logging model values or
unknown keys. Managed object-mode generation gets one local repair on its first
barrier attempt; repair failures do not multiply transport retries.

Quote matching requires one unique NFC-normalized occurrence, collapses
whitespace and maps typographic quotes, dashes and ellipses to their plain forms.
It retains punctuation, case, word order, negation and token boundaries; it never
accepts semantic paraphrase. Stored quotes and offsets still refer to the exact
original text. Complete-clause, truncation and embedded-data checks remain.

## Replay

`tools/paid_write_replay.py enrich-history --source … --output …` reads only the
original 300 call IDs. Its read-only production query selects the prior fifteen
same-tenant/session tool calls strictly before each target timestamp. Only
redacted targets/command summaries are exported, never outputs. A new mode-600
input is created without replacing the original. Local history rows do not
represent human decisions or successful execution. Missing profiles no longer
abort relaxed replay before evaluation.

Acceptance requires full PostgreSQL tests, Ruff, independent review and a single
authorized full replay. The canary gate is at least 90% legacy-approval agreement
and every remaining non-approval justified by external/irreversible effects,
real restrictions or genuine task mismatch. Agreement alone is not safety proof.
