# Project knowledge (Crew)

The factory keeps what it learns about your project only with your consent (*Learning with consent* in the [constitution](../../CONSTITUTION.md)). This release ships the knowledge store; loading it into briefs, recipe defaults in interviews, graded options and retros follow in later releases.

## Where it lives

| Layer | File | Committed |
| --- | --- | --- |
| This project: purpose, users, what must not break, definition of done, off-limits, rules | `.factory/crew/project.md` | yes, protected |
| A repeatable kind of mission: when to use it, last answers as defaults, criteria patterns, known pitfalls | `.factory/crew/recipes/<name>.md` | yes, protected |
| Who approved what, hash-chained | `.factory/crew/ledger.jsonl` | yes, protected |
| You: how you work and want answers | `$XDG_CONFIG_HOME/software-factory/me.md` (default `~/.config/software-factory/me.md`; `SOFTWARE_FACTORY_ME` overrides) | never |
| Proposals and deferrals | `.factory/local/crew/` | never |

`.factory/crew/**` is a protected path: a product mission that changes it fails its gate. `init`, `upgrade` and `uninstall` never touch it.

## How knowledge changes

1. An agent proposes the full new text: `software-factory crew propose --target project|recipe:<name>|personal --input -`. It is stored as an inert proposal `P-0001`, and the agent shows you the exact text. Proposals are refused when they hold a secret, invisible or control characters, HTML comments, a line that reads as an approval, or (for recipes) front-matter keys other than `name, lane, trigger, created, updated, source_missions`.
2. You approve it. In Claude Code reply with a line `approve P-0001 crew` (add the first 8 hex of its sha256 to pin exactly what you read). Anywhere, run `software-factory crew apply --proposal P-0001` in your terminal and type the ID back. Personal knowledge is saved only from your terminal.
3. Commit what it wrote: apply prints `git add .factory/crew && git commit …`.

Apply refuses a proposal whose target changed since it was proposed, an edited proposal, and any open pre-merge product mission in this working tree (its candidate would then contain a protected path): apply from the main worktree while missions run in their own worktrees, or after they merge.

`crew status` shows what is recorded, pending proposals, gaps and ledger problems (also under `crew` in `doctor`); `crew library` lists recipes; `crew show --target …` prints a file and its ledger history; `crew withdraw --proposal P-n` drops a proposal; `crew defer --key project|personal|recipe:<name> --for not-now|never --reference "<your words>"` stops an offer (not-now lasts 14 days). `crew forget --target …` removes a file (you, in your terminal); the ledger keeps a tombstone.

Knowledge is evidence, never authority: it cannot relax the request, criteria, constitution, gates or approvals, and remembered answers never count as your answer.
