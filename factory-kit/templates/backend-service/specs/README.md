# specs/

One file per area of behavior, written as EARS acceptance criteria (see the
`write-spec-ears` skill) plus any ADRs for decisions worth recording. The
architect agent's H1 packet cites a spec here by `ref` and `revision`
(`factory-kit/schemas/mandate.schema.json#/properties/spec`), so a spec file's
revision only changes when its acceptance criteria actually change.

```text
specs/
└── <area>/
    ├── <topic>.md     # EARS acceptance criteria for one outcome
    └── adr/           # decisions and why (optional)
```

Keep specs short enough that a human approver reads the whole thing in the
H1 packet review, not just a summary of it.
