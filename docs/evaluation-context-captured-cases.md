# Read-only capture acceptance evidence

Source: authenticated tenant/session-scoped production audit and event reads,
without replay, model calls or production mutations. The deployed Intaris base
was RC6 `806ce0b3b275151901a29dc15d7c4f0f2e8307cb`. Investigation and
production record identifiers are retained privately.

Both evaluations belong to the same captured session.
The event read reported a contiguous history without a gap.

| Evaluation | Captured facts | Missing evidence |
| --- | --- | --- |
| Artifact publication case | `artifact_publish`; executor/document source; owner-scoped storage description; declared read-only/read capability; L1 write/mutating with external=true; Judge low-risk outside-task defer. The call, evaluation and result were consecutive events. | No selected-operation annotations in the old model view. No final view digest or exact Judge input size. |
| Shell transmission case | `bash`; executor/shell source; declared write capability. Proposed Python monitoring-management script with lock handling and ID recording. L1 write/transmit but external=false; Judge low-risk routine-within-task defer. The call, evaluation and result were consecutive events. | Not a process cleanup case. No earlier structured shell IDs recorded. Judge trimmed agent reason and conversation tail; exact byte loss unavailable. |

Both audit records say `not_dispatched`; paired results failed. Neither action
is evidenced as executed. The captured task artifact had four originals,
including a duplicate latest-message representation and a generated summary,
with partial coverage. The sole user event preceded both evaluations. Private task bodies,
command text, paths, output and credentials are intentionally not copied here.

These captures establish the declaration/rendering gaps, not the result of a
new model replay. Offline regressions characterize generic operation contracts,
raw-source coverage and real structured result joins. A successful own-process
cleanup fixture is synthetic because the stated captures do not contain a real
structured start result. Paid replay remains an integration-owner follow-up.
