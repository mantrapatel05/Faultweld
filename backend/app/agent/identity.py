"""Identity of the simulated environment, defined once for agent-side records.

The scenario fixtures are literal-typed for the real (kind) environment. The simulation has
its own stable identity so that evidence, proposals and, from commit 4 on, policy decisions
agree on what they describe without reading evaluator-only fixture labels.
"""

from __future__ import annotations

ENVIRONMENT_ID = "sim"
CLUSTER_ID = "sim-faultweld"
NAMESPACE = "faultweld-demo"
DEPLOYMENT_KIND = "Deployment"
DEPLOYMENT_NAME = "faultweld-sample"
DEPLOYMENT_UID = "sim-deployment-faultweld-sample"
