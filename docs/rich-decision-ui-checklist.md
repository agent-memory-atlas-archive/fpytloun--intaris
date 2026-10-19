# Rich decision UI acceptance

Verified against the task-owned localhost static preview using synthetic REST
responses, vendored Alpine and rebuilt CSS. The preview did not start the Intaris
runtime or invoke a model. Automated API and service tests cover real persistence
and decision boundaries separately.

- [x] Sessions: exact quotes, all six kind badges, status, current marker, scoped
  approval and dropped-item count/code render beneath the summary.
- [x] Sessions: active profile, compiled rules and advisory allow/deny clauses
  render side by side; raw policy is available in a disclosure.
- [x] 390px iframe: new policy columns stack into one 316px column and quotes
  wrap. Existing Console/Events button row has an 8px horizontal overflow;
  this pre-existing row is unchanged.
- [x] Script-looking quote renders literally, with no script nodes in blockquotes.
- [x] Null/legacy profile hides the new rich panel while preserving the summary.
- [x] Approvals: structured `refuse` / `operator_policy` headline and exact cited
  user text render; Jev abstentions list only the uncertain fields.
- [x] Audit: disclosure witness, verb heuristic and actual L1 projection tags
  render in expanded details.
- [x] No editing controls were introduced for intention items or operator clauses.
- [x] `node --test tests/ui_decision.test.js`: structured-stage selection, unavailable
  historical cites, abstentions, witness tags, six kind badges, stale session
  refresh protection, review notifications and audit refresh.

The implementing review retained screenshots of Sessions (desktop and a 390px
iframe), Approvals, and Audit. Those review artifacts are not public downloads;
this checklist does not expose their private locators or claim they are published
release assets. The observations above refer to that historical synthetic preview.

CSS build: the existing `make css` command invokes `npx tailwindcss`, which does
not expose the v4 CLI in a fresh environment. Built using pinned task-local
`tailwindcss@4.1.13` and `@tailwindcss/cli@4.1.13`, without adding dependencies:

```sh
npm install --prefix /tmp/intaris-rich-jev-tailwind --no-save \
  --package-lock=false tailwindcss@4.1.13 @tailwindcss/cli@4.1.13
ln -s /tmp/intaris-rich-jev-tailwind/node_modules node_modules
./node_modules/.bin/tailwindcss -i intaris/ui/src/input.css \
  -o intaris/ui/static/css/app.css --minify
unlink node_modules
```
