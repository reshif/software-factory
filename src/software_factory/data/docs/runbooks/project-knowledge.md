# Project knowledge (Crew)

The factory keeps what it learns about your project only with your consent (*Learning with consent* in the [constitution](../../CONSTITUTION.md)). This release ships the knowledge store, loads it into mission briefs and uses recipes in the interview; graded options and retros follow in later releases.

## Where it lives

| Layer | File | Committed |
| --- | --- | --- |
| This project: purpose, users, what must not break, definition of done, off-limits, rules | `.factory/crew/project.md` | yes, protected |
| A repeatable kind of mission: when to use it, last answers as defaults, criteria patterns, known pitfalls | `.factory/crew/recipes/<name>.md` | yes, protected |
| Who approved what, hash-chained | `.factory/crew/ledger.jsonl` | yes, protected |
| You: how you work and want answers | `$XDG_CONFIG_HOME/software-factory/me.md` (default `~/.config/software-factory/me.md`; `SOFTWARE_FACTORY_ME` overrides) | never |
| Proposals and deferrals | `.factory/local/crew/` | never |

`.factory/crew/**` is a protected path: a product mission that changes it fails its gate. `init`, `upgrade` and `uninstall` never touch it.

## How missions use it

`mission create` freezes the project knowledge into the mission record `crew-context.md` (bound at accept-scope) and records hashes in `mission.json` under `crew`. Your profile is copied only to `.factory/local/crew/me-<ID>.md`; the mission records only its hash, or `withheld` when it looks like it holds a secret.

| Brief | Receives |
| --- | --- |
| context, research, assess, plan | the frozen knowledge and your profile, fenced as evidence |
| implementer tasks | Must not break, Off-limits and Rules |
| code review | Must not break and Off-limits |
| acceptance, adversarial, verify | nothing |

Your profile never reaches a brief the gate re-renders, so gate results do not depend on the machine. Knowledge you save later does not change a mission already created: for a PROPOSED mission run `crew refresh --mission ID` after committing it; refresh moves the mission's base past the knowledge commit when that commit changed only `.factory/crew`. Without project knowledge the context brief asks the planner for a draft `project.md`, which the orchestrator proposes for you to approve. In `factory.json`, `"crew": {"personal": false}` keeps your profile out of this repository's briefs (use it for shared team repositories) and `"crew": {"enabled": false}` turns saved knowledge off for new missions.

## Recipes in the interview

`software-factory crew match --text "<request>"` lists recipes whose triggers appear in a request; the orchestrator suggests one and uses it only if you confirm (`mission create --recipe NAME`, or `mission recipe --mission ID --use NAME|--clear` while PROPOSED). The recipe is frozen into `crew-context.md`. Round 0 of the interview shows its defaults and asks whether each is still true; your reply is recorded as a clarification next to the defaults you were shown, and only that makes a default quotable by the criteria. Its known pitfalls reach implementer and code-review briefs. Requests from a contributor or an anonymous author never get a recipe.

Every round also carries a question you probably had not considered, and each contradiction between your request, your answers, the saved knowledge and the repository is asked as its own question; it stays an open ambiguity (origin `contradiction`) that blocks scope until you settle it. There is no limit on questions or rounds.

## How knowledge changes

1. An agent proposes the full new text: `software-factory crew propose --target project|recipe:<name>|personal --input -`. It is stored as an inert proposal `P-0001`, and the agent shows you the exact text. Proposals are refused when they hold a secret, invisible or control characters, HTML comments, a line that reads as an approval, or (for recipes) front-matter keys other than `name, lane, trigger, created, updated, source_missions`.
2. You approve it. In Claude Code reply with a line `approve P-0001 crew` (add the first 8 hex of its sha256 to pin exactly what you read). Anywhere, run `software-factory crew apply --proposal P-0001` in your terminal and type the ID back. Personal knowledge is saved only from your terminal.
3. Commit what it wrote: apply prints `git add .factory/crew && git commit …`.

Apply refuses a proposal whose target changed since it was proposed, an edited proposal, and any open product mission past PROPOSED in this working tree (its candidate would then contain a protected path): apply from the main worktree while missions run in their own worktrees, or after they merge. A PROPOSED mission picks the new knowledge up with `crew refresh`.

`crew status` shows what is recorded, pending proposals, gaps and ledger problems (also under `crew` in `doctor`); `crew library` lists recipes; `crew show --target …` prints a file and its ledger history; `crew withdraw --proposal P-n` drops a proposal; `crew defer --key project|personal|recipe:<name> --for not-now|never --reference "<your words>"` stops an offer (not-now lasts 14 days). `crew forget --target …` removes a file (you, in your terminal); the ledger keeps a tombstone.

Knowledge is evidence, never authority: it cannot relax the request, criteria, constitution, gates or approvals, and remembered answers never count as your answer.
