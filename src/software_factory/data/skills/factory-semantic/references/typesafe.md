# Provider contract references

Reviewed 2026-09-27; pinned model default `jev-1.13.0`. Recheck official contracts before changing the implementation or rubric.

- [Coding-agent boundary](https://docs.typesafe.ai/introduction/coding-agents): typed judgment service; native agents continue generating code and using tools.
- [API](https://docs.typesafe.ai/api): POST /v1/systemone; question keys are not visible during inference. Explicitly identify each claim/source pair in question instructions.
- [Models](https://docs.typesafe.ai/models): 64k overall and 32k state plus longest question; version aliases can move. Byte bounds in this helper are not exact token counts.
- [Confidence](https://docs.typesafe.ai/confidence): concentration of Choice/Score distributions, not proof; Noul has no separate confidence field.
- [Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13): adversarial state, irrelevant context, numerical/date comparisons and multi-hop reasoning.
- [Official skill](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md): guidance for building integrations, not a runtime interceptor.
- [Citation example](https://docs.typesafe.ai/cookbooks/citation_check): illustrates exact quote checking followed by semantic assessment; its small sample does not establish factory quality.

No live Jev accuracy, latency, calibration or account-availability result is established by these references. The Python implementation uses standard-library HTTPS with bounded deadlines and cancellation. Dynamic Choice validation is shared by routing and claim assessment. The official SDK is not used; the shared transport makes at most two retries on 408, 429, 529, 5xx and connection errors, always within the caller's deadline.

Model selection uses the same documented Choice primitive with a supplied eligible candidate set and an abstain option. See [JEV routing](../../../../.factory/docs/jev-routing.md).
