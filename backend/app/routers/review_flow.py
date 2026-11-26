"""审单转派/证据读取/确认回执的语义化 HTTP 别名。

通用 ``/api/match_review/{id}/actions`` 已能覆盖主流程；这里提供更贴近验收脚本
和页面调用的 REST 路径，并允许通过 query、header 或 JSON body 传递复核员身份。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.services import service_for

router = APIRouter(prefix="/api", tags=["审单转派与确认回执"])


def _review_service():
    return service_for("match_review")


def _match_service():
    return service_for("sku_match")


def _respond(outcome: dict[str, Any]) -> JSONResponse:
    status_code = int(outcome.get("http_status") or 200)
    return JSONResponse(status_code=status_code, content=outcome)


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _identity(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    query = dict(request.query_params)
    merged: dict[str, Any] = {}
    merged.update(query)
    merged.update(body)
    for key in ("reviewer", "user", "operator", "actor", "current_user", "as"):
        value = body.get(key) or query.get(key) or request.headers.get(f"x-{key}")
        if value:
            merged[key] = value
    reviewer = (
        body.get("reviewer") or body.get("user") or body.get("current_user")
        or query.get("reviewer") or query.get("user") or query.get("as")
        or request.headers.get("x-reviewer") or request.headers.get("x-user")
    )
    if reviewer:
        merged["reviewer"] = reviewer
    key = (
        body.get("idempotency_key") or body.get("request_key") or body.get("requestId")
        or query.get("idempotency_key") or query.get("request_key")
        or request.headers.get("idempotency-key") or request.headers.get("x-idempotency-key")
        or request.headers.get("x-request-id")
    )
    if key:
        merged["idempotency_key"] = key
    return merged


@router.post("/match_review/scenario/m72")
@router.post("/match_review/setup-m72")
def setup_m72() -> dict[str, Any]:
    service = _review_service()
    entry = service.ensure_seed_scenario()
    return {"ok": True, "message": "M72 / 600g 转派审单场景已就绪", "entry": entry}


@router.post("/match_review/scenario/reset")
def reset_scenario() -> dict[str, Any]:
    entry = _review_service().reset_scenario()
    return {"ok": True, "message": "M72 演练数据已重置为仅甲认领", "entry": entry}


@router.get("/match_review/todo/{user}")
def todo(user: str) -> dict[str, Any]:
    return _review_service().my_todo(user)


@router.get("/match_review/{review_ref}/context")
@router.get("/match_review/{review_ref}/task")
def context(review_ref: str, request: Request) -> dict[str, Any]:
    params = dict(request.query_params)
    reviewer = (
        params.get("reviewer") or params.get("user") or params.get("viewer")
        or params.get("as") or params.get("current_user")
        or request.headers.get("x-reviewer") or request.headers.get("x-user")
    )
    return _review_service().task_context(review_ref, reviewer, **params)


@router.get("/match_review/{review_ref}/sample")
@router.get("/match_review/{review_ref}/sample_detail")
@router.get("/match_review/{review_ref}/original-evidence")
@router.get("/match_review/{review_ref}/evidence")
def sample(review_ref: str, request: Request) -> JSONResponse:
    params = dict(request.query_params)
    reviewer = params.pop("reviewer", None) or params.pop("user", None) or request.headers.get("x-reviewer")
    return _respond(_review_service().read_sample(review_ref, reviewer, **params))


@router.get("/match_review/{review_ref}/receipt")
def receipt(review_ref: str, request: Request) -> dict[str, Any]:
    key = request.query_params.get("request_key") or request.headers.get("idempotency-key")
    return _review_service().get_receipt(review_ref, key)


@router.post("/match_review/{review_ref}/claim")
async def claim(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    reviewer = params.pop("reviewer", None) or params.pop("user", None)
    return _respond(_review_service().claim_task(review_ref, reviewer, **params))


@router.post("/match_review/{review_ref}/accept")
@router.post("/match_review/{review_ref}/takeover")
async def accept(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    reviewer = params.pop("reviewer", None) or params.pop("user", None)
    return _respond(_review_service().accept_task(review_ref, reviewer, **params))


@router.post("/match_review/{review_ref}/transfer")
@router.post("/match_review/{review_ref}/transfer_task")
@router.post("/match_review/{review_ref}/transfer-task")
@router.post("/match_review/{review_ref}/assign")
@router.post("/match_review/{review_ref}/reassign")
@router.post("/match_review/{review_ref}/handoff")
async def transfer(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    target = params.pop("target_reviewer", None) or params.pop("to_reviewer", None) or params.pop("to", None) or params.pop("assignee", None)
    return _respond(_review_service().transfer_task(review_ref, target_reviewer=target, **params))


@router.post("/match_review/{review_ref}/submit")
@router.post("/match_review/{review_ref}/complete")
@router.post("/match_review/{review_ref}/confirm")
@router.post("/match_review/{review_ref}/conclusion")
@router.post("/match_review/{review_ref}/approve")
async def submit(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    reviewer = params.pop("reviewer", None) or params.pop("user", None)
    conclusion = params.pop("conclusion", None) or params.pop("复核结论", None)
    return _respond(_review_service().approve(review_ref, reviewer, conclusion=conclusion, **params))


@router.post("/match_review/{review_ref}/reject")
async def reject(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    reviewer = params.pop("reviewer", None) or params.pop("user", None)
    conclusion = params.pop("conclusion", None) or params.pop("复核结论", None)
    return _respond(_review_service().reject(review_ref, reviewer, conclusion=conclusion, **params))


@router.post("/match_review/{review_ref}/candidate-evidence")
async def update_evidence(review_ref: str, request: Request) -> JSONResponse:
    body = await _body(request)
    params = _identity(request, body)
    reviewer = params.pop("reviewer", None) or params.pop("user", None)
    return _respond(_review_service().update_candidate_evidence(review_ref, reviewer, **params))


@router.post("/sku_match/apply-confirmed-review")
async def apply_by_body(request: Request) -> dict[str, Any]:
    body = await _body(request)
    return _match_service().apply_confirmed_review(**body)


@router.post("/sku_match/{match_ref}/apply-confirmed-review")
@router.post("/sku_match/{match_ref}/apply-review")
async def apply_by_ref(match_ref: str, request: Request) -> dict[str, Any]:
    body = await _body(request)
    body.setdefault("match_id", match_ref)
    body.setdefault("review_ref", body.get("review_no") or match_ref)
    return _match_service().apply_confirmed_review(**body)


@router.get("/sku_match/confirmed-result/{review_ref}")
def confirmed_result(review_ref: str) -> dict[str, Any]:
    result = _match_service().apply_confirmed_review(review_ref=review_ref)
    return result
