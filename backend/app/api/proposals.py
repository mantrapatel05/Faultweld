from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from app.api.dependencies import get_runtime
from app.api.errors import (
    CONFLICT_PROBLEM_TYPE,
    FORBIDDEN_PROBLEM_TYPE,
    NOT_FOUND_PROBLEM_TYPE,
    UNAVAILABLE_PROBLEM_TYPE,
    ApiProblem,
    ProblemDetails,
)
from app.api.runtime import ApiRuntime
from app.api.views import (
    DecisionRequest,
    DecisionResponse,
    ExecuteResponse,
    ProposalDetailResponse,
    proposal_detail,
)
from app.domain.schemas import ApprovalDecision, IncidentStatus
from app.policy.principals import LOCAL_DEMO_USER_ID

router = APIRouter(prefix="/api/proposals", tags=["proposals"])


@router.get(
    "/{proposal_id}",
    response_model=ProposalDetailResponse,
    responses={
        404: {"model": ProblemDetails},
        503: {"model": ProblemDetails},
    },
)
def get_proposal(
    proposal_id: Annotated[str, Path(min_length=1, max_length=128)],
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
) -> ProposalDetailResponse:
    with runtime.open_store() as store:
        stored = store.get_proposal(proposal_id)
        if stored is None:
            raise ApiProblem(
                status=404,
                problem_type=NOT_FOUND_PROBLEM_TYPE,
                title="Proposal not found",
                detail="The requested proposal does not exist.",
                code="proposal_not_found",
            )
        decision = store.get_decision(proposal_id)
        try:
            snapshot = runtime.cluster.source.snapshot()
        except Exception as error:
            raise ApiProblem(
                status=503,
                problem_type=UNAVAILABLE_PROBLEM_TYPE,
                title="Live state unavailable",
                detail="The current simulator state could not be read.",
                code="state_unavailable",
            ) from error
        return proposal_detail(
            stored,
            current_snapshot=snapshot,
            policy_decision=decision.decision.decision if decision is not None else None,
            policy_reason_codes=(decision.decision.reason_codes if decision else ()),
            policy_config=runtime.policy_config,
            now=runtime.clock(),
        )


@router.post(
    "/{proposal_id}/decision",
    response_model=DecisionResponse,
    responses={
        403: {"model": ProblemDetails},
        404: {"model": ProblemDetails},
        409: {"model": ProblemDetails},
        422: {"model": ProblemDetails},
    },
)
def decide_proposal(
    proposal_id: Annotated[str, Path(min_length=1, max_length=128)],
    payload: DecisionRequest,
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
) -> DecisionResponse:
    """Record a human decision; the approver identity is resolved server-side."""
    with runtime.open_store() as store:
        if payload.decision is ApprovalDecision.APPROVE:
            outcome = store.approve(
                proposal_id,
                payload.expected_version,
                LOCAL_DEMO_USER_ID,
            )
        else:
            outcome = store.reject(
                proposal_id,
                payload.expected_version,
                LOCAL_DEMO_USER_ID,
                reason=payload.reason or "",
            )
            store.update_incident_status(
                outcome.approval.incident_id,
                IncidentStatus.ESCALATED,
                terminal_reason="approval_rejected",
            )
        current = store.get_proposal(proposal_id)
        if current is None:
            raise ApiProblem(
                status=404,
                problem_type=NOT_FOUND_PROBLEM_TYPE,
                title="Proposal not found",
                detail="The proposal disappeared after the decision was recorded.",
                code="proposal_not_found",
            )
        approval = outcome.approval.approval
        return DecisionResponse(
            proposal_id=proposal_id,
            approval_id=approval.approval_id,
            decision=approval.decision,
            approver_principal_id=approval.approver_principal_id,
            approver_role=approval.approver_role,
            expires_at=approval.expires_at,
            proposal_version=current.version,
            already_decided=outcome.already_decided,
        )


@router.post(
    "/{proposal_id}/execute",
    response_model=ExecuteResponse,
    responses={
        202: {"model": ExecuteResponse},
        403: {"model": ProblemDetails},
        404: {"model": ProblemDetails},
        409: {"model": ProblemDetails},
        503: {"model": ProblemDetails},
    },
)
def execute_proposal(
    proposal_id: Annotated[str, Path(min_length=1, max_length=128)],
    runtime: Annotated[ApiRuntime, Depends(get_runtime)],
    response: Response,
) -> ExecuteResponse:
    outcome = runtime.execute_proposal(proposal_id)
    successful = outcome.reason_code in {"executed", "replayed", "reconciled"}
    if not successful and outcome.reason_code != "execution_unknown":
        status_code = (
            404
            if outcome.reason_code in {"proposal_not_found", "approval_not_found"}
            else 409
        )
        problem_type = NOT_FOUND_PROBLEM_TYPE if status_code == 404 else CONFLICT_PROBLEM_TYPE
        title = "Execution resource not found" if status_code == 404 else "Execution refused"
        if outcome.reason_code in {"principal_mismatch", "role_not_permitted"}:
            status_code, problem_type, title = 403, FORBIDDEN_PROBLEM_TYPE, "Execution forbidden"
        if outcome.reason_code == "state_unavailable":
            status_code = 503
            problem_type = UNAVAILABLE_PROBLEM_TYPE
            title = "Live state unavailable"
        raise ApiProblem(
            status=status_code,
            problem_type=problem_type,
            title=title,
            detail=f"The gateway did not execute this proposal: {outcome.reason_code}.",
            code=outcome.reason_code,
        )

    if outcome.reason_code == "execution_unknown":
        response.status_code = status.HTTP_202_ACCEPTED

    return ExecuteResponse(
        proposal_id=proposal_id,
        reason_code=outcome.reason_code,
        executed=outcome.executed,
        attempt_id=outcome.attempt_id,
        attempt_outcome=outcome.attempt_outcome,
        proposal_state=outcome.proposal_state,
        verification_status=outcome.verification_status,
        writer_operations=outcome.writer_operations,
    )
