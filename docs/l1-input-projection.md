# L1 input projection

`L1_INPUT_PROJECTION=auto|json|lines` selects the presentation of the action and
generated rich intention in the hybrid selective L1 assessment. Default: `auto`.
See [stage separation](evaluation-stage-separation.md) for ownership and safety.

| Backend/model | Auto projection |
| --- | --- |
| OpenAI-compatible GPT-OSS (including provider-qualified names) | `lines` |
| Jev | `lines` |
| Other chat models | `json` |

Explicit `json` and `lines` override this mapping for both chat and Jev. Jev
retains its typed-choice API: the projected text is carried in its `prompt`
state alongside selective options. The `auto` mapping follows the
measurements in `evaluation-redesign-2026-10.md`: Jev was benchmarked with
`lines` in the full matched run, and the first study showed the JSON
rendering hurt GPT-OSS. Other chat models keep `json` until measured.

This setting applies to the hybrid selective L1 boundary, including its
summary-only fallback. It does not migrate legacy/default sessions. Judge
receives a separate JSON packet with original evidence; the intention cannot
filter that evidence. Extraction artifacts remain unchanged.

Lines put the proposed tool and complete effective arguments first, followed by
the generated goal/constraints and relevant controller observations/tool hints.
Raw sources, conversation tails, approval/proposal pairs, skills and past
actions are not L1 inputs. Nested arguments are flattened, not discarded.
Missing effects and scope remain unknown. Generated restrictions remain
distinguished by status and reference. Controls in quoted text are escaped to
prevent multiline text from forging labeled records. Output references use
readable enum labels; validated answers are translated back to controller
references before policy evaluation.

GPT-OSS on the direct `api.groq.com` endpoint requests `strict: true` for L1
JSON-schema output, independently of the projection override. This is not
applied to Judge, Jev or other providers/models. The existing unsupported-format
fallback remains; `stages[].output_format` records the actual successful
transport format and whether it was strict. Shape constraints do not guarantee
correct classifications.

Evaluation metadata records the requested/resolved projection and its version.
`context_input.l1_projection` is null when L1 did not run, including static and
retained-review paths. Each L1 stage records user/system character counts and
projection diagnostics. Digests and acquisition metadata stay in audit records,
not in the lines evidence payload.

## Offline size comparison

```sh
uv run --extra dev --with tiktoken python -m tools.l1_projection_sizes \
  --output /tmp/intaris-l1-projection-sizes.json
```

This uses the exact pinned `c7c60f6bd5dd76451584353769e0bbef86d0f543`
classification assembly and renderer through isolated module-local imports,
independently of subsequent adapter or renderer changes in the integration tree.
Both formats receive the same synthetic selected artifacts based on versioned
benchmark fixtures, not idealized model responses. Results include complete
system/user messages and separate character/token counts using local
`tiktoken/o200k_harmony` encoding (GPT-OSS vocabulary). Chat framing, provider
serialization and Jev's typed question overhead are excluded. No inference
calls are made. The model benchmark remains the correctness/latency gate.
