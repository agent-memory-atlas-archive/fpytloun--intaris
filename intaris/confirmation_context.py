"""Shared interpretation of user confirmations, never a deterministic grant."""

CONFIRMATION_INSTRUCTIONS = """
Interpret chronological user confirmations in context, including 'Odešli',
'yes, send it' and 'ok'. A short confirmation need not repeat the recipient,
content or tool arguments. When the captured conversation identifies one concrete
requested/proposed action and the current action matches it, the user's explicit
confirmation authorizes that action. Cite the user confirmation and the user
request establishing its scope. An earlier 'show me the draft before sending'
condition is satisfied by a later unambiguous confirmation of that same draft;
do not ask the identical approval question again merely because the reply is short.
An assistant proposal can disambiguate the referent but is not itself permission.
Never derive the referent solely from the current tool call or agent justification.
If the proposal/scope is missing, ambiguous, changed, contradicted or revoked,
keep permission unresolved. A bare 'ok' acknowledges only what was actually
proposed: it does not authorize another recipient, modified content, extra effects
or future actions. Confirmation never overrides operator prohibitions or ceilings.
"""
