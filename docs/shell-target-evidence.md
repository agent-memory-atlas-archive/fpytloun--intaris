# Shell target context

`shell_target` carries bounded target-specific executor context for `bash_kill`.
Normalization binds it to the evaluated user, session, shell ID and, when
specified, target executor. Invalid bindings become unknown. Output tails and
unsupported fields are removed; credentials are redacted before truncation.

The same normalized target reaches policy assessment and independent Judge
review even when the shell-start event has aged out of recent history. Command
and purpose strings are contextual data, never instructions or authorization.
The evaluator does not auto-approve a known target or auto-deny an unknown one.

This context is supplied by the authenticated caller, not independently attested
by Intaris. Cognis obtains it from its selected executor and validates ownership.
Clients without target context retain the previous behavior.
