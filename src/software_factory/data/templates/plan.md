# Mission plan

This template guides planning; the actual task records live in `mission.json`.

## Approach

Explain the chosen implementation and material tradeoffs using current repository evidence. Match detail to the task's uncertainty.

## Task contracts

For each task record its ID, inspectable outcome, dependencies, permitted paths, assigned role, acceptance criteria, required check IDs and expected result. Add tasks through the state tool so dependency and scope rules apply.

## Integration and verification

State the integration order, who owns the active workspace and which work can safely run independently. Map acceptance criteria to actual checks or explicit manual validation. Include final verification and independent review.

## Risks

List the material risks of this change: compatibility, data, security, performance or rollout. For each, state its likelihood, impact and mitigation or detection. Write "None identified" with a reason when that is true. The PR packet copies this section and refuses a missing or unedited one.

## Replanning record

Record material changes to the approach, the evidence that caused them and effects on existing tasks. Preserve completed work and decision history.
