---
name: factory-retro
description: Look back at a finished mission and turn what it taught into lessons you approve one by one, as project rules or a reusable recipe.
argument-hint: "Mission ID, for example M-0007"
default-prompt: "Use $factory-retro on the mission I name, and show me the lessons with their evidence."
---

# Retro: export the win

The constitution in AGENTS.md applies (*Learning with consent*). You propose lessons; only the user saves them.

1. The mission must be at READY_PR or later (a cancelled mission counts). Run `software-factory crew retro-signals --mission ID` and tell the user what it found.
2. `software-factory mission brief --mission ID --kind retro` → give the brief to a planner. It works from the mission's records only and returns retro.md and a lessons JSON. Record retro.md with `mission record-doc --mission ID --doc retro --input -`.
3. Pipe the lessons JSON to `software-factory crew propose --mission ID --input -`. It refuses lessons without evidence from the record, defaults that do not quote the user's own answer, and any lesson from a contributor or anonymous request.
4. Show the user every proposal with its numbered items and evidence, the preferences meant for their own me.md, and the maintenance requests (changes to checks, hooks, roles, CI or policy, which are never product work). Do not save anything yourself.
5. After the mission merges, the user approves items by number: `approve P-n crew 1,3` in Claude Code chat, or `software-factory crew apply --proposal P-n --items 1,3` in their terminal, then commits `.factory/crew`. There is no approve-all for lessons.
