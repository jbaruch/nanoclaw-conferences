# Initial calibration — 2026-10-20

The operator's `cfp-labeling-2026-10-16.json` supplies 17 non-geographic
yes/no labels: ten yes and seven no. Geo-excluded, unlabelled and
`unknown_to_baruch` rows were excluded. The evaluation used the current
speaker profile, relevance criteria and conference metadata, including
bounded website descriptions. No state decisions changed during calibration.

The final evaluation refreshed Sessionize metadata through the current
normalizer before fetching website descriptions and recorded each full
distribution. It automatically kept FOSDEM and DevFest Berlin, dismissed
Dev Days Students Madrid, and routed fourteen to Sonnet. All three automatic
decisions matched the operator's
labels. Every response in this final pass supplied a usable distribution.

The executable routing contract and calibration accounting procedure are
referenced in `skills/check-cfps/references/jev-relevance.md`.

This small set selected conservative routing boundaries; it is not an
independent accuracy estimate. Retain the middle band until a larger
labelled evaluation supports narrowing it. Re-evaluate when the model,
questions, profile or metadata projection changes.


# Model renewal — 2026-11-05

The same 17 non-geographic yes/no cases were replayed against `jev-1.13.0`
and `jev-1.14.0`, using the current speaker profile, criteria and candidate
metadata. Public website descriptions were fetched once; two were unavailable.
Each pair received the same frozen state and questions, without labels or prior
scores. Private input bundle SHA-256: `972e331c270ecc9913d2953451c552223d832202313585f862c08047e485a503`.

Both models produced usable distributions for all 17 cases. With the existing
bands, both kept FOSDEM and DevFest Berlin, dismissed Dev Days Students Madrid,
and sent fourteen cases to the reasoning model. All three automatic decisions
matched the operator's labels; no case changed route. The pin moves to 1.14
without changing the bands.

This replay checks this cohort's routing, not independent accuracy on future
conferences. The private run artifact retains each packet digest, probability
distribution, metadata warning and the co-shipped evaluator's summary. No CFP
state or owner decision changed during the comparison.
