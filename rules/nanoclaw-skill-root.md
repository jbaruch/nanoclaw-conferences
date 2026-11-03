---
alwaysApply: false
applyTo: "skills/**/SKILL.md, skills/**/references/*.md — when writing or reviewing a command block that runs a co-shipped script"
---

# NanoClaw Skill Root

## Lookup

- NanoClaw installs each of this plugin's skills at `/home/node/.claude/skills/tessl__<skill>/` inside the agent container
- That directory is the only skill root the container mounts; no `.tessl/plugins/` tree exists there
- Command blocks invoke a co-shipped script as `python3 /home/node/.claude/skills/tessl__<skill>/scripts/<file>`
- `<skill>` is one of this plugin's skills: `check-cfps`, `nightly-cfp-sync`, `conference-followups`
- `<file>` is a script under `skills/<skill>/scripts/` in this repository
- Reference reads name the complete repository-relative source and its explicit corresponding `/home/node/.claude/skills/tessl__<skill>/references/<file>.md` path.
- No other root, fallback or computed lookup is used

## Scope

- Authored scope: `skills/check-cfps/SKILL.md`, `skills/check-cfps/references/*.md`, `skills/nightly-cfp-sync/SKILL.md`, `skills/nightly-cfp-sync/references/*.md`, `skills/conference-followups/SKILL.md`, `skills/conference-followups/references/*.md`
- Co-shipped targets: every file under `skills/check-cfps/scripts/` `skills/nightly-cfp-sync/scripts/` and `skills/conference-followups/scripts/`
- Input and output contracts: each script's own docstring; this rule adds none

## Authority

- This rule names a path; it grants no runtime authority
- It waives no check a script or skill applies after the lookup
- Regression coverage: `tests/test_skill_script_paths.py` resolves every such command path in the authored scope to an existing co-shipped script
