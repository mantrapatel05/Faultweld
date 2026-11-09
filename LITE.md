# Faultweld Lite status

Faultweld Lite is the locally runnable simulator workflow. It requires no Docker,
Kubernetes cluster, or live model provider.

| Area | Status |
|---|---|
| Simulated fault scenarios | Implemented |
| Typed investigation and policy-gated proposals | Implemented |
| SQLite approvals and audit history | Implemented |
| Gateway execution and independent verification | Implemented |
| Scripted safety evaluations | Implemented; report records scope and limitations |
| Incident API and operator approval workflows | Implemented |
| Browser approval and verification console | Implemented |
| Real-cluster writes and live-model evaluation | Out of scope |

The browser console presents the server's proposal, policy, approval, execution,
and verification state. Approval does not execute an action. `recovered`,
`not_recovered`, and `unknown` remain separate outcomes, and the normal API app
does not mount the demo-only simulator mutation route.

See [README.md](README.md) for setup, demonstration commands, evaluation results,
and limitations.
