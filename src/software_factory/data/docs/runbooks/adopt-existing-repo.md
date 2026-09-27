# Adopt an existing project

Follow [setup](factory-setup.md). Run `software-factory inspect --root /path/to/project` for read-only stack/check suggestions, then initialize the repository root with an explicit profile.

The package installs .factory runtime/configuration assets and selected native exports. It does not replace the product README, root dependency manifest, code, CI or editor preferences. AGENTS.md, CLAUDE.md and Copilot instructions use owned sections; Codex configuration preserves unrelated TOML keys. Name collisions or edited owned content stop preflight before writes.

