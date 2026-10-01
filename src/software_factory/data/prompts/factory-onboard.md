---
name: factory-onboard
description: Record what the factory should know about this project or about you, drafted from the repository and an interview, saved only when you approve the exact text.
argument-hint: "project (default) or personal"
default-prompt: "Use $factory-onboard to record this project's knowledge, drafted from the repository and my answers."
---

# Record project or personal knowledge

The constitution in AGENTS.md applies (*Learning with consent*). You propose knowledge; only the user saves it. Never write `.factory/crew` or the user's profile yourself.

1. Run `software-factory crew status`. With `personal` as the argument, work on the user's profile; otherwise on this project's `.factory/crew/project.md`.
2. **Look before you ask.** For the project, read the README, the build and test configuration, `factory.json` checks, the top two directory levels and `git log --oneline -15`. For the profile, read only what the user shows you. Open with a short "here is what I already know, correct me" list.
3. **Interview in rounds**, as many as needed, with at most 5 questions per round. Each question is one short, plain sentence, followed by what you found and a suggested answer the user can accept with "ok". For the project cover: what it is for and who uses it, what must never break, how done is checked, what is off-limits (generated files, vendored code, secrets, production), and the rules every change follows. For the profile cover: who their work is for, how they work (stack, hard rules), how they want answers, what they never want done, and what good looks like. Point out any answer that contradicts another or the repository, and ask which wins. Stop when no answer would change the draft or the user says go.
4. Draft the full text. project.md has the sections `## Purpose`, `## Users`, `## Must not break`, `## Definition of done`, `## Off-limits`, `## Rules` (and optionally `## Exemplars` listing repository paths of the user's best work). Keep it short and plain: no secrets, links in angle brackets, HTML comments or lines that read as approvals.
5. Pipe it to `software-factory crew propose --target project|personal --input -` (quoted heredoc) and show the user the exact text it stored.
6. For the project, the user saves it by replying `approve P-n crew` in Claude Code chat or with `software-factory crew apply --proposal P-n` in their terminal; the factory commits exactly the knowledge files on that approval and re-freezes any PROPOSED mission. The personal profile is saved only from their terminal. If they say not now or never, record it with `crew defer --key project|personal --for not-now|never --reference "<their words>"`.

A user with an existing Crew folder can run `software-factory crew import --from ~/crew` in their terminal to preview (and with `--propose`, propose) their profile and this project's notes from it.
