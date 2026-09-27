# Software Factory 0.2.8: persistent TypeSafe authentication

The previous runtime read only its own `TYPESAFE_API_KEY` environment variable. A key set in a different terminal or agent process was therefore unavailable. Version 0.2.8 adds one user credential store shared by model routing, semantic check, semantic verify-claims and triage. The environment remains an explicit override; there is no per-command key handoff.

## Install and set up once

```sh
uv tool install --reinstall /home/reshif/build/software-factory/dist/software_factory-0.2.8-py3-none-any.whl
software-factory auth login
software-factory auth status
```

Login reads hidden input. Status checks local availability without contacting TypeSafe. Freshly initialized projects use this release's pinned runtime. Existing projects need an explicit upgrade or the user's planned reinitialization before they can read saved credentials. The global installation and consuming project were left untouched during this build.

[Authentication guide and colored Mermaid diagram](../src/software_factory/data/docs/runbooks/authentication.md) explain lookup precedence, storage, rotation, logout, CI and agent access. The Linux store is an owner-only plaintext file outside repositories; Windows uses user-bound DPAPI, which still requires real Windows validation.

## Verification

- Linux full suite: **393 tests and 379 subtests passed** on both Python 3.14.4 and 3.11.16.
- Locked dependency sync, Ruff, source-distribution build and isolated wheel installation passed.
- Fresh-process credential lookup, all four JEV consumers, nested product-check exclusion, preservation across project lifecycle operations and logout passed using synthetic keys and mocked providers.
- Independent review's rotation stress passed 1,200 writes and 6,000 concurrent reads; regression tests cover both stat and descriptor races and reject multiple hard links.
- Wheel bytes match the source; archive inspection found no private credential files, development environments or Node runtime.

No live provider inference, real-key authentication, native-client execution, macOS/Windows execution or publication is claimed. [Exact source identity and verification record](authentication-release-0.2.8.json) and [artifact checksums](../dist/SHA256SUMS) are saved separately from the archives.
