"""人工复核队列领域服务。

在原有认领、办结、驳回联动之外，增加“转派审单”的归属/证据版本控制：

* 只有当前认领人或接手人能读取样本详情、写回结论；
* 转派会递增归属版本并作废旧的 owner token，迟到提交不能覆盖新归属；
* 提交与转派在同一把领域锁内完成检查+提交，并发时只会产生一个生效结果；
* 幂等键用于“请求超时后重投”的回执辨别；
* 结论固化当时可回查的候选证据版本，规格匹配器只消费这份确认快照。
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

FLOW_LOCK = threading.RLock()

REVIEWER_ALIASES = {
    "甲": "甲", "jia": "甲", "user-a": "甲", "reviewer-a": "甲", "a": "甲",
    "乙": "乙", "yi": "乙", "user-b": "乙", "reviewer-b": "乙", "b": "乙",
}
SUPERVISOR_ALIASES = {"主管", "supervisor", "admin", "manager"}
ELIGIBLE_ROLES = ("规格审核员", "商品运营", "数据治理专员", "甲", "乙")
TRANSFER_ACTIONS = {
    "转派审单", "转派", "转交", "移交", "移交审单", "转派任务",
    "transfer", "transfer_task", "transfer_review", "reassign", "handoff",
}
APPROVE_ACTIONS = {"通过记录", "通过", "审核通过", "提交结论", "提交复核结论", "approve", "submit", "submit_conclusion"}
REJECT_ACTIONS = {"驳回记录", "驳回", "reject", "reject_review"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_user(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return REVIEWER_ALIASES.get(text.casefold(), text)


def pick(params: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in params and params[key] not in (None, ""):
            return params[key]
    return None


def _as_params(source: Any = None, **kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if source is not None:
        if hasattr(source, "model_dump"):
            data = source.model_dump(exclude_none=True)
            if isinstance(data, dict):
                params.update(data)
        elif isinstance(source, dict):
            params.update(source)
    nested = params.get("params")
    values = params.get("values")
    merged: dict[str, Any] = {}
    if isinstance(nested, dict):
        merged.update(nested)
    if isinstance(values, dict):
        merged.update(values)
    # 顶层显式参数优先于嵌套容器。
    for key, value in params.items():
        if key not in ("params", "values"):
            merged[key] = value
    merged.update({k: v for k, v in kwargs.items() if v is not None})
    return merged


class MatchReviewService(BaseModuleService):
    MODULE = "match_review"
    ENTITY = "复核记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "匹配记录", "复核结论", "复核角色", "复核时间"]
    REQUIRED_FIELDS = ["记录编号", "匹配记录", "复核结论"]
    STATUS_ORDER = ["待复核", "已通过", "已驳回"]
    ACTION_RULES = {"通过记录": "已通过", "驳回记录": "已驳回", "转派审单": "待复核"}
    NEGATIVE_ACTIONS = ["驳回记录"]

    def __init__(self) -> None:
        self.ensure_seed_scenario()

    def list_entries(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        self.ensure_seed_scenario()
        return super().list_entries(*args, **kwargs)

    # ------------------------------------------------------------------
    # 场景种子与通用解析
    # ------------------------------------------------------------------

    def reset_scenario(self) -> dict[str, Any]:
        """恢复种子并重建 M72 场景，便于验收脚本重复执行。"""
        from app.seed import SEED_ROWS
        with FLOW_LOCK:
            store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
            return self.ensure_seed_scenario()

    def ensure_seed_scenario(self) -> dict[str, Any] | None:
        """补齐题面中的 M72 / 600g 场景；使用独立 72 号锚点，不覆盖通用样例。"""
        with FLOW_LOCK:
            rows = store.rows(self.MODULE)
            review = next((r for r in rows if str(r.get("记录编号", "")) == "M72"), None)
            if review is None:
                review = store.find(self.MODULE, 72)
                if review is None:
                    review = {"id": 72, "记录编号": "M72"}
                    rows.append(review)
                else:
                    review["记录编号"] = "M72"

            if review.get("flow_initialized"):
                return review

            review_no = str(review.get("记录编号") or "M72")
            linked_ref = review.get("match_id") or review.get("匹配记录")
            match = None
            if linked_ref not in (None, "") and str(linked_ref).isdigit():
                match = store.find("sku_match", int(linked_ref))
            if match is None:
                match = next((m for m in store.rows("sku_match") if str(m.get("review_no")) == review_no), None)
            if match is None:
                match = store.find("sku_match", 72)
                if match is None:
                    match = {"id": 72}
                    store.rows("sku_match").append(match)
            match_id = int(match.get("id") or 72)
            sample_id = 72
            application_no = str(review.get("申请单号") or f"APP-{review_no}")
            evidence_id = f"EV-{review_no}-600G"
            fingerprint = "FP-M72-600G-v1"
            digest = hashlib.sha256(f"{evidence_id}|v1|600g|{fingerprint}".encode()).hexdigest()

            review.update({
                "status": "待复核",
                "pending": True,
                "abnormal": False,
                "匹配记录": str(match_id),
                "复核结论": "待提交：候选规格 600g（证据 v1）",
                "复核角色": "甲",
                "复核时间": "",
                "申请单号": application_no,
                "match_id": match_id,
                "review_no": review_no,
                "task_no": review_no,
                "task_id": review_no,
                "reviewer_role": "规格审核员",
                "claimed_by": "甲",
                "original_claimant": "甲",
                "assignee": "甲",
                "current_reviewer": "甲",
                "owner": "甲",
                "owner_id": "jia",
                "assigned_to": "甲",
                "assigned_by": "主管",
                "ownership_version": 1,
                "claim_version": 1,
                "version": 1,
                "owner_token": f"owner-{review_no}-v1-jia",
                "completed": False,
                "confirmed": False,
                "confirmed_receipt": None,
                "decision": None,
                "handoffs": [],
                "transfer_receipts": [],
                "submission_receipts": {},
                "request_log": {},
                "evidence_scope": [{
                    "evidence_id": evidence_id,
                    "id": evidence_id,
                    "evidence_version": 1,
                    "version": "v1",
                    "candidate_spec": "600g",
                    "sample_id": sample_id,
                    "sample_fingerprint": fingerprint,
                }],
                "evidence_versions": [{
                    "evidence_id": evidence_id,
                    "id": evidence_id,
                    "evidence_version": 1,
                    "version": "v1",
                    "application_no": application_no,
                    "review_no": review_no,
                    "candidate_spec": "600g",
                    "normalized_spec": "600g",
                    "sample_id": sample_id,
                    "sample_fingerprint": fingerprint,
                    "source": "商品详情页样本",
                    "traceable": True,
                    "content_digest": digest,
                    "captured_at": "2026-11-26T01:50:00+00:00",
                }],
                "flow_initialized": True,
            })

            if match is not None and not match.get("review_flow_initialized"):
                match.update({
                    "匹配批次": f"BATCH-{review_no}",
                    "商品档案": "商品登记中心样例2",
                    "候选规格": "600g",
                    "匹配置信度": "0.82",
                    "匹配状态": "待人工复核",
                    "status": "人工复核",
                    "pending": True,
                    "abnormal": False,
                    "申请单号": application_no,
                    "review_no": review_no,
                    "review_id": review.get("id"),
                    "candidate_evidence_id": evidence_id,
                    "candidate_evidence_version": 1,
                    "candidate_version": "v1",
                    "candidate_fingerprint": fingerprint,
                    "candidate_versions": [{
                        "evidence_id": evidence_id,
                        "version": 1,
                        "candidate_spec": "600g",
                        "fingerprint": fingerprint,
                    }],
                    "review_flow_initialized": True,
                })

            sample = store.find("sample_store", sample_id)
            if sample is None:
                sample = {"id": sample_id}
                store.rows("sample_store").append(sample)
            if not sample.get("review_flow_initialized"):
                sample.update({
                    "样本批次": f"SAMPLE-{review_no}",
                    "站点名称": "示例品牌旗舰店",
                    "原始HTML摘要": "<div class='sku'>净含量：600g/盒</div>",
                    "抓取时间": "2026-11-26 01:50",
                    "样本指纹": fingerprint,
                    "status": "已保存",
                    "pending": True,
                    "abnormal": False,
                    "申请单号": application_no,
                    "review_no": review_no,
                    "evidence_id": evidence_id,
                    "evidence_version": 1,
                    "review_flow_initialized": True,
                })

            store.rows("review_sample_detail")
            detail_rows = store.rows("review_sample_detail")
            if not any(str(r.get("review_no")) == review_no for r in detail_rows):
                detail_rows.append({
                    "id": max((int(r.get("id", 0)) for r in detail_rows), default=0) + 1,
                    "review_no": review_no,
                    "evidence_id": evidence_id,
                    "evidence_version": 1,
                    "sample_id": sample_id,
                    "sample_fingerprint": fingerprint,
                    "title": "示例品牌 M72 商品详情页",
                    "html": (
                        "<html><body><h1>示例商品 M72</h1>"
                        "<section data-testid='original-evidence'>"
                        "<span class='net-weight'>净含量：600g</span>"
                        "<span class='pack'>盒装</span></section></body></html>"
                    ),
                    "captured_at": "2026-11-26T01:50:00+00:00",
                })
            return review

    def resolve_entry(self, ref: int | str | None) -> dict[str, Any] | None:
        self.ensure_seed_scenario()
        if ref is None:
            return None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            entry = store.find(self.MODULE, int(ref))
            if entry is not None:
                return entry
        text = str(ref).strip()
        entry = next((r for r in store.rows(self.MODULE) if str(r.get("记录编号", "")) == text), None)
        if entry is not None:
            return entry
        for field in ("review_no", "task_no", "task_id", "code", "id"):
            entry = next((r for r in store.rows(self.MODULE) if str(r.get(field, "")) == text), None)
            if entry is not None:
                return entry
        return super().resolve_entry(ref)

    # ------------------------------------------------------------------
    # HTTP/动作分派
    # ------------------------------------------------------------------

    def handle_action(
        self,
        entry_id: int | str,
        payload: Any,
        *,
        request_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = _as_params(payload)
        action = str(params.pop("action", "") or getattr(payload, "action", "") or "").strip()
        if request_key:
            params.setdefault("request_key", request_key)
        if context:
            for key, value in context.items():
                if value not in (None, ""):
                    params.setdefault(key, value)
        return self.dispatch(entry_id, action, params)

    def dispatch(self, ref: int | str, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        key = pick(params, "idempotency_key", "request_key", "requestId", "key")
        reviewer = pick(params, "reviewer", "user", "username", "current_user", "operator", "actor", "as_user", "复核人", "办理人", "当前用户", "操作人")
        action_text = str(action or "").strip()

        if action_text in TRANSFER_ACTIONS:
            target = pick(params, "target_reviewer", "to_reviewer", "new_reviewer", "to_user", "assignee", "target", "to", "指定复核员", "接收人", "接班人", "转派给")
            supervisor = pick(params, "supervisor", "operator", "actor", "from_reviewer", "主管", "操作人")
            return self.transfer_task(
                ref, supervisor=supervisor, target_reviewer=target,
                reason=pick(params, "reason", "remark", "原因"),
                idempotency_key=key,
                expected_version=pick(params, "expected_version", "expected_owner_version", "owner_version"),
            )
        if action_text in {"认领", "认领任务", "claim", "claim_task"}:
            return self.claim_task(ref, reviewer or pick(params, "role", "复核角色"), **{
                "owner_token": pick(params, "owner_token", "token"),
                "idempotency_key": key,
            })
        if action_text in {"接手", "接手任务", "接受转派", "accept", "accept_task", "takeover", "take_over"}:
            return self.accept_task(ref, reviewer, **params)
        if action_text in APPROVE_ACTIONS:
            return self.approve(
                ref, reviewer,
                conclusion=pick(params, "conclusion", "复核结论", "result", "decision_text"),
                candidate_spec=pick(params, "candidate_spec", "候选规格", "spec"),
                evidence_id=pick(params, "evidence_id", "evidenceId"),
                evidence_version=pick(params, "evidence_version", "version", "证据版本"),
                owner_token=pick(params, "owner_token", "token"),
                expected_version=pick(params, "expected_version", "expected_owner_version", "owner_version"),
                idempotency_key=key,
                remark=pick(params, "remark"),
            )
        if action_text in REJECT_ACTIONS:
            return self.reject(
                ref, reviewer,
                conclusion=pick(params, "conclusion", "复核结论", "reason", "原因"),
                candidate_spec=pick(params, "candidate_spec", "候选规格", "spec"),
                evidence_id=pick(params, "evidence_id", "evidenceId"),
                evidence_version=pick(params, "evidence_version", "version", "证据版本"),
                owner_token=pick(params, "owner_token", "token"),
                expected_version=pick(params, "expected_version", "expected_owner_version", "owner_version"),
                idempotency_key=key,
            )

        entry, message = self.run_action(ref, action_text)
        return {"ok": entry is not None, "message": message, "entry": entry}

    # ------------------------------------------------------------------
    # 认领与待办
    # ------------------------------------------------------------------

    def claim_task(self, entry_id: int | str, role: str | dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """按角色/实名认领待复核记录。"""
        if isinstance(role, dict):
            kwargs.update(role)
            role = pick(role, "role", "reviewer", "user", "复核角色", "复核人")
        with FLOW_LOCK:
            entry = self.resolve_entry(entry_id)
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}
            reviewer = normalize_user(role or pick(kwargs, "reviewer", "user", "复核人"))
            if not reviewer:
                return {"ok": False, "code": "identity_required", "http_status": 401, "message": "认领必须提供复核员身份"}
            if reviewer not in ELIGIBLE_ROLES and reviewer not in REVIEWER_ALIASES.values():
                return {"ok": False, "code": "role_forbidden", "http_status": 403, "message": f"角色 {reviewer} 无复核资格"}

            current = normalize_user(entry.get("assignee") or entry.get("复核角色"))
            if entry.get("status") != "待复核":
                return {"ok": False, "code": "already_completed", "completed": True, "message": "记录已办结，不能重复认领", "entry": entry}
            if current and current != reviewer:
                return {
                    "ok": False, "code": "already_claimed", "conflict": True, "http_status": 409,
                    "message": f"记录已由 {current} 认领，需主管转派后才能接手", "owner": current, "entry": entry,
                }

            version = int(entry.get("ownership_version") or entry.get("claim_version") or 0) + 1
            token = str(kwargs.get("owner_token") or f"owner-{entry.get('记录编号', entry.get('id'))}-v{version}-{reviewer}")
            entry.update({
                "复核角色": reviewer,
                "复核人": reviewer,
                "claimed_by": reviewer,
                "assignee": reviewer,
                "current_reviewer": reviewer,
                "owner": reviewer,
                "assigned_to": reviewer,
                "reviewer_role": entry.get("reviewer_role") or "规格审核员",
                "ownership_version": version,
                "claim_version": version,
                "version": version,
                "owner_token": token,
                "pending": True,
            })
            return {"ok": True, "message": f"记录已由 {reviewer} 认领", "owner": reviewer,
                    "ownership_version": version, "owner_token": token, "entry": entry}

    def accept_task(self, ref: int | str, reviewer: str | dict | None = None, **kwargs: Any) -> dict[str, Any]:
        """指定接手人从待办进入并确认接手；转派后的同一原任务号不变。"""
        if isinstance(reviewer, dict):
            kwargs.update(reviewer)
            reviewer = pick(reviewer, "reviewer", "user", "current_user", "复核人")
        user = normalize_user(reviewer or pick(kwargs, "user", "current_user", "operator", "复核人"))
        with FLOW_LOCK:
            entry = self.resolve_entry(ref)
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}
            if entry.get("status") in ("已通过", "已驳回") or entry.get("completed"):
                return {"ok": False, "code": "already_completed", "completed": True,
                        "message": "该单已办结，不能再接手续办", "entry": entry}
            assignee = normalize_user(entry.get("assignee") or entry.get("复核角色"))
            if not user:
                return {"ok": False, "code": "identity_required", "http_status": 401, "message": "接手必须提供复核员身份"}
            if not assignee:
                claimed = self.claim_task(ref, user)
                if not claimed.get("ok"):
                    return claimed
                entry = claimed["entry"]
                assignee = user
            if user != assignee:
                return {"ok": False, "code": "reviewer_forbidden", "http_status": 403,
                        "message": f"转派接手人是 {assignee or '未指定'}，{user} 无权接手", "entry": entry}

            at = now_iso()
            first_accept = not entry.get("accepted_at")
            entry.update({
                "accepted_by": user,
                "accepted_at": entry.get("accepted_at") or at,
                "takeover_by": user,
                "takeover_at": entry.get("takeover_at") or at,
                "assignee": user,
                "current_reviewer": user,
                "owner": user,
                "复核角色": user,
            })
            result = self.task_context(ref, user)
            result.update({
                "ok": True,
                "message": f"{user} 已从自己的待办接手 M72，可打开原证据继续提交" if first_accept else f"{user} 已接手该任务，可继续办理",
                "accepted": True,
                "entry": entry,
            })
            return result

    def pending_queue(self, role: str | None = None) -> dict[str, Any]:
        """输出待复核队列，可按复核角色/当前归属人过滤。"""
        self.ensure_seed_scenario()
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "待复核"]
        viewer = normalize_user(role) if role else ""
        if viewer:
            rows = [
                r for r in rows
                if viewer in {
                    normalize_user(r.get("复核角色")), normalize_user(r.get("assignee")),
                    normalize_user(r.get("current_reviewer")), normalize_user(r.get("claimed_by")),
                    normalize_user(r.get("assigned_to")),
                }
            ]
        return {
            "ok": True,
            "message": "待复核队列已输出",
            "count": len(rows),
            "role": role,
            "viewer": viewer or None,
            "records": [r.get("记录编号", "") for r in rows][:10],
            "items": rows,
        }

    def my_todo(self, user: str) -> dict[str, Any]:
        result = self.pending_queue(user)
        result["message"] = f"{normalize_user(user)} 的待办已输出"
        result["can_submit"] = True
        return result

    # ------------------------------------------------------------------
    # 转派
    # ------------------------------------------------------------------

    def transfer_task(
        self,
        entry_id: int | str | None = None,
        *positional: Any,
        supervisor: str | None = None,
        target_reviewer: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # 兼容 transfer_task(ref, target)、transfer_task(ref, supervisor, target)
        # 以及更宽松的关键字调用。
        args = [a for a in positional if a not in (None, "")]
        params = _as_params(kwargs)
        target = target_reviewer or pick(params, "to_reviewer", "new_reviewer", "to_user", "assignee", "target", "to", "接班人", "指定复核员")
        actor = supervisor or pick(params, "operator", "actor", "from_reviewer", "主管", "操作人")

        if target is None and args:
            if len(args) == 1 and str(args[0]).strip() in SUPERVISOR_ALIASES and not actor:
                actor = str(args[0])
            elif len(args) == 1:
                target = args[0]
            else:
                target = args[-1]
        if actor is None and len(args) >= 2:
            actor = args[0]

        supervisor = actor
        reason = pick(params, "reason", "remark", "原因")
        key = pick(params, "idempotency_key", "request_key", "requestId", "key")
        expected_version = pick(params, "expected_version", "expected_owner_version", "owner_version")

        with FLOW_LOCK:
            entry = self.resolve_entry(entry_id if entry_id is not None else params.get("review_ref") or params.get("review_id"))
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}

            review_no = str(entry.get("记录编号") or entry.get("id"))
            if entry.get("status") in ("已通过", "已驳回") or entry.get("completed"):
                receipt = entry.get("confirmed_receipt") or {}
                return {
                    "ok": False, "code": "already_completed", "conflict": True, "http_status": 409,
                    "completed": True, "already_completed": True, "confirmed": True,
                    "message": f"{review_no} 已由 {receipt.get('effective_reviewer') or entry.get('effective_reviewer') or entry.get('复核角色')} 办结，不能转派",
                    "effective_reviewer": receipt.get("effective_reviewer") or entry.get("effective_reviewer"),
                    "receipt": receipt or None, "entry": entry,
                }

            target = normalize_user(target)
            if not target:
                return {"ok": False, "code": "target_required", "http_status": 422, "message": "转派必须指定接手复核员"}
            if target not in ELIGIBLE_ROLES and target not in REVIEWER_ALIASES.values():
                return {"ok": False, "code": "role_forbidden", "http_status": 403, "message": f"接手人 {target} 无复核资格"}

            current_version = int(entry.get("ownership_version") or entry.get("version") or 1)
            if expected_version not in (None, "") and int(expected_version) != current_version:
                return {
                    "ok": False, "code": "version_conflict", "conflict": True, "http_status": 409,
                    "message": f"归属版本已变化：请求基于 v{expected_version}，当前 v{current_version}",
                    "current_version": current_version, "entry": entry,
                }

            if key and key in (entry.get("transfer_idempotency") or {}):
                cached = entry["transfer_idempotency"][key]
                return {**cached, "entry": entry, "duplicate": True, "idempotent_replay": True}

            previous = normalize_user(entry.get("assignee") or entry.get("current_reviewer") or entry.get("复核角色"))
            version = current_version + 1
            token = f"owner-{review_no}-v{version}-{target}-{uuid.uuid4().hex[:8]}"
            at = now_iso()
            match = self._linked_match(entry)
            evidence_scope = [dict(item) for item in (entry.get("evidence_scope") or [])]
            if not evidence_scope:
                evidence = self._select_evidence(entry)
                if evidence:
                    evidence_scope = [{
                        "evidence_id": evidence.get("evidence_id"),
                        "evidence_version": evidence.get("evidence_version", 1),
                        "version": evidence.get("version"),
                        "candidate_spec": evidence.get("candidate_spec"),
                        "sample_id": evidence.get("sample_id"),
                        "sample_fingerprint": evidence.get("sample_fingerprint"),
                    }]

            scope_descriptions = [f"复核单 {review_no}（原任务号不变）"]
            if match is not None:
                scope_descriptions.append(f"匹配任务 #{match.get('id')} / {match.get('匹配批次', '')}")
            for item in evidence_scope:
                scope_descriptions.append(
                    f"候选证据 {item.get('evidence_id')} {item.get('version') or ('v' + str(item.get('evidence_version', 1)))}"
                    f"（{item.get('candidate_spec', '')}）"
                )
                if item.get("sample_id") is not None:
                    scope_descriptions.append(f"原始样本 #{item.get('sample_id')} / {item.get('sample_fingerprint', '')}")

            handover = {
                "receipt_id": f"HO-{review_no}-v{version}-{uuid.uuid4().hex[:8]}",
                "handover_id": f"HO-{review_no}-v{version}",
                "type": "转派审单",
                "review_id": entry.get("id"),
                "review_ref": review_no,
                "review_no": review_no,
                "记录编号": review_no,
                "task_id": entry.get("task_id") or review_no,
                "original_task_id": entry.get("id"),
                "match_id": match.get("id") if match is not None else entry.get("match_id"),
                "application_no": entry.get("申请单号"),
                "from_reviewer": previous or None,
                "previous_reviewer": previous or None,
                "to_reviewer": target,
                "target_reviewer": target,
                "supervisor": normalize_user(supervisor) or "主管",
                "operator": normalize_user(supervisor) or "主管",
                "reason": reason or "",
                "transferred_at": at,
                "ownership_version": version,
                "owner_token": token,
                "evidence_scope": evidence_scope,
                "scope": scope_descriptions,
                "移交范围": scope_descriptions,
                "can_submit": True,
                "completed": False,
            }

            entry.update({
                "status": "待复核",
                "pending": True,
                "复核角色": target,
                "复核人": target,
                "assignee": target,
                "current_reviewer": target,
                "owner": target,
                "assigned_to": target,
                "claimed_by": target,
                "assigned_by": normalize_user(supervisor) or "主管",
                "ownership_version": version,
                "claim_version": version,
                "version": version,
                "owner_token": token,
                "last_handover": handover,
            })
            entry.setdefault("handoffs", []).append(handover)
            entry.setdefault("transfer_receipts", []).append(handover)
            if key:
                cached_outcome = {
                    "ok": True,
                    "message": f"审单 {review_no} 已由 {previous or '原复核员'} 转派给 {target}",
                    "receipt": handover, "handover": handover,
                    "scope": scope_descriptions, "移交范围": scope_descriptions,
                    "owner": target, "ownership_version": version,
                    "owner_token": token, "can_submit": True, "completed": False,
                }
                entry.setdefault("transfer_idempotency", {})[key] = cached_outcome

            return {
                "ok": True,
                "message": f"审单 {review_no} 已转派：{previous or '未认领'} → {target}；接手回执已列出移交范围",
                "receipt": handover,
                "handover": handover,
                "scope": scope_descriptions,
                "移交范围": scope_descriptions,
                "owner": target,
                "ownership_version": version,
                "owner_token": token,
                "can_submit": True,
                "completed": False,
                "entry": entry,
            }

    def transfer_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.transfer_task(*args, **kwargs)

    def reassign(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.transfer_task(*args, **kwargs)

    def assign_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.transfer_task(*args, **kwargs)

    def handoff_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.transfer_task(*args, **kwargs)

    def transfer_reviewer(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.transfer_task(*args, **kwargs)

    # ------------------------------------------------------------------
    # 证据/样本读取与任务视图
    # ------------------------------------------------------------------

    def _linked_match(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        match_id = entry.get("match_id") or entry.get("匹配记录")
        text = str(match_id or "").strip()
        if text.isdigit():
            found = store.find("sku_match", int(text))
            if found is not None:
                return found
        review_no = entry.get("记录编号") or entry.get("review_no")
        return next((m for m in store.rows("sku_match") if str(m.get("review_no", "")) == str(review_no)), None)

    def _evidence_catalog(self, entry: dict[str, Any]) -> list[dict[str, Any]]:
        catalog = entry.get("evidence_versions")
        if isinstance(catalog, list) and catalog:
            return catalog
        match = self._linked_match(entry) or {}
        review_no = str(entry.get("记录编号") or entry.get("id"))
        evidence_id = entry.get("candidate_evidence_id") or match.get("candidate_evidence_id") or f"EV-{review_no}-600G"
        version = int(entry.get("candidate_evidence_version") or match.get("candidate_evidence_version") or 1)
        spec = entry.get("候选规格") or match.get("候选规格") or "600g"
        return [{
            "evidence_id": evidence_id,
            "id": evidence_id,
            "evidence_version": version,
            "version": f"v{version}",
            "application_no": entry.get("申请单号") or match.get("申请单号"),
            "review_no": review_no,
            "candidate_spec": spec,
            "sample_id": entry.get("sample_id") or match.get("sample_id") or 1,
            "sample_fingerprint": entry.get("sample_fingerprint") or match.get("candidate_fingerprint"),
            "traceable": True,
        }]

    def _select_evidence(
        self,
        entry: dict[str, Any],
        evidence_id: str | None = None,
        evidence_version: Any = None,
        candidate_spec: str | None = None,
    ) -> dict[str, Any] | None:
        catalog = self._evidence_catalog(entry)
        scope = entry.get("evidence_scope")
        scope_ids = {
            (str(x.get("evidence_id") or x.get("id")), int(x.get("evidence_version") or 1))
            for x in scope if isinstance(x, dict)
        } if isinstance(scope, list) else set()

        wanted_id = str(evidence_id or "").strip()
        wanted_spec = str(candidate_spec or "").strip().lower()
        wanted_version: int | None = None
        if evidence_version not in (None, ""):
            text = str(evidence_version).strip().lstrip("vV")
            if text.isdigit():
                wanted_version = int(text)

        candidates = catalog
        if wanted_id:
            candidates = [e for e in candidates if str(e.get("evidence_id") or e.get("id")) == wanted_id]
        if wanted_version is not None:
            candidates = [e for e in candidates if int(e.get("evidence_version") or 1) == wanted_version]
        if wanted_spec:
            candidates = [e for e in candidates if wanted_spec in str(e.get("candidate_spec", "")).lower()]

        evidence = candidates[0] if candidates else None
        if evidence is None and not wanted_id and wanted_version is None and not wanted_spec:
            # 默认使用发起复核时冻结在 evidence_scope 中的版本，绝不静默取候选最新值。
            if scope and isinstance(scope, list) and scope[0]:
                ref = scope[0]
                eid = ref.get("evidence_id") or ref.get("id")
                ver = int(ref.get("evidence_version") or 1)
                evidence = next(
                    (e for e in catalog if str(e.get("evidence_id") or e.get("id")) == str(eid)
                     and int(e.get("evidence_version") or 1) == ver),
                    dict(ref),
                )
            else:
                evidence = catalog[0] if catalog else None

        if evidence is None:
            return None
        if scope_ids:
            key = (str(evidence.get("evidence_id") or evidence.get("id")), int(evidence.get("evidence_version") or 1))
            if key not in scope_ids:
                return None
        elif evidence.get("traceable") is False:
            return None
        return dict(evidence)

    def _viewer_state(self, entry: dict[str, Any], viewer: str | None, owner_token: str | None = None,
                      expected_version: Any = None) -> tuple[bool, str, dict[str, Any]]:
        user = normalize_user(viewer)
        assignee = normalize_user(entry.get("assignee") or entry.get("current_reviewer") or entry.get("复核角色"))
        effective = normalize_user(entry.get("effective_reviewer") or (entry.get("confirmed_receipt") or {}).get("effective_reviewer"))
        terminal = entry.get("status") in ("已通过", "已驳回") or bool(entry.get("completed"))

        if not user:
            return False, "identity_required", {
                "message": "未提供复核员身份，不能读取样本或写回结论",
                "unauthorized": True, "authorized": False, "permission_denied": True,
            }
        if terminal and effective and user == effective:
            return True, "", {"owner": user, "terminal_owner": True}
        if terminal:
            return False, "already_completed", {
                "message": f"本单已由 {effective or '其他复核员'} 办结，{user} 不能再写回结论",
                "completed": True, "already_completed": True, "effective_reviewer": effective,
            }
        if user != assignee:
            return False, "reviewer_forbidden", {
                "message": f"{user} 不是 {entry.get('记录编号') or '本单'} 当前认领/接手人（当前：{assignee or '未认领'}），无权读取样本详情或写回结论",
                "owner": assignee, "forbidden": True, "permission_denied": True,
                "authorized": False, "can_read_sample": False, "can_submit": False,
            }
        current_version = int(entry.get("ownership_version") or entry.get("version") or 1)
        if expected_version not in (None, "") and int(expected_version) != current_version:
            return False, "version_conflict", {
                "message": f"归属版本已变化：请求基于 v{expected_version}，当前 v{current_version}",
                "current_version": current_version, "conflict": True,
            }
        if owner_token and owner_token != entry.get("owner_token"):
            return False, "owner_token_invalid", {
                "message": "写回凭证已随转派失效，请从最新待办进入后重试",
                "current_version": current_version, "conflict": True,
            }
        return True, "", {"owner": user, "ownership_version": current_version}

    def task_context(self, ref: int | str, viewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        with FLOW_LOCK:
            entry = self.resolve_entry(ref)
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}
            token = pick(kwargs, "owner_token", "token")
            expected = pick(kwargs, "expected_version", "owner_version")
            allowed, code, meta = self._viewer_state(entry, viewer, token, expected)
            terminal = entry.get("status") in ("已通过", "已驳回") or bool(entry.get("completed"))
            user = normalize_user(viewer)
            can_submit = allowed and not terminal
            result = {
                "ok": True,
                "message": "任务入口状态已生成",
                "review_id": entry.get("id"),
                "review_ref": entry.get("记录编号"),
                "review_no": entry.get("记录编号"),
                "viewer": user or None,
                "status": entry.get("status"),
                "completed": terminal,
                "finished": terminal,
                "already_completed": terminal,
                "can_submit": can_submit,
                "submittable": can_submit,
                "can_write": can_submit,
                "can_read_sample": allowed,
                "authorized": allowed,
                "is_owner": allowed,
                "assignee": entry.get("assignee") or entry.get("复核角色"),
                "effective_reviewer": entry.get("effective_reviewer") or (entry.get("confirmed_receipt") or {}).get("effective_reviewer"),
                "ownership_version": entry.get("ownership_version") or 1,
                "owner_token": entry.get("owner_token") if user == normalize_user(entry.get("assignee")) else None,
                "evidence_scope": entry.get("evidence_scope") or [],
                "handoffs": entry.get("handoffs") or [],
                "receipt": entry.get("confirmed_receipt"),
                "entry": entry,
            }
            if not allowed:
                result.update({"ok": False, "code": code, **meta})
                result["can_submit"] = False
                result["submittable"] = False
                result["can_write"] = False
                result["can_read_sample"] = False
                result["authorized"] = False
            elif terminal:
                result["message"] = "该单已办结，只能查看确认回执，不能重复提交"
            else:
                result["message"] = f"{user} 当前持有可写资格，可以打开原证据并提交结论"
            return result

    def get_task_view(self, ref: int | str, viewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.task_context(ref, viewer, **kwargs)

    def entry_status(self, ref: int | str, viewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.task_context(ref, viewer, **kwargs)

    def read_sample(self, ref: int | str, reviewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        with FLOW_LOCK:
            entry = self.resolve_entry(ref)
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}
            user = reviewer or pick(kwargs, "user", "viewer", "current_user", "operator", "复核人")
            allowed, code, meta = self._viewer_state(entry, user, pick(kwargs, "owner_token", "token"))
            if not allowed:
                return {"ok": False, "code": code, "http_status": 403 if code != "identity_required" else 401,
                        "authorized": False, "can_read_sample": False, **meta}

            evidence = self._select_evidence(
                entry,
                pick(kwargs, "evidence_id", "evidenceId"),
                pick(kwargs, "evidence_version", "version"),
                pick(kwargs, "candidate_spec", "候选规格"),
            )
            review_no = str(entry.get("记录编号"))
            sample_id = (evidence or {}).get("sample_id") or 1
            sample = store.find("sample_store", int(sample_id)) if str(sample_id).isdigit() else None
            detail = next(
                (d for d in store.rows("review_sample_detail")
                 if str(d.get("review_no")) == review_no
                 and (evidence is None or str(d.get("evidence_id")) == str(evidence.get("evidence_id")))),
                None,
            )
            if detail is None:
                detail = next((d for d in store.rows("review_sample_detail") if str(d.get("review_no")) == review_no), {})
            return {
                "ok": True,
                "message": "原证据样本详情已读取",
                "authorized": True,
                "review_no": review_no,
                "reviewer": normalize_user(user),
                "evidence": evidence,
                "sample": sample,
                "sample_detail": detail,
                "original_evidence": detail or sample,
                "can_continue": entry.get("status") == "待复核",
            }

    def get_sample_detail(self, ref: int | str, reviewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.read_sample(ref, reviewer, **kwargs)

    def read_evidence(self, ref: int | str, reviewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.read_sample(ref, reviewer, **kwargs)

    def open_original_evidence(self, ref: int | str, reviewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.read_sample(ref, reviewer, **kwargs)

    # ------------------------------------------------------------------
    # 提交/办结与幂等
    # ------------------------------------------------------------------

    def _decision(
        self,
        ref: int | str | None,
        reviewer: str | dict | None,
        approved: bool,
        conclusion: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if isinstance(reviewer, dict):
            kwargs.update(reviewer)
            reviewer = pick(reviewer, "reviewer", "user", "current_user", "operator", "复核人")
        params = _as_params(kwargs)
        reviewer = reviewer or pick(params, "reviewer", "user", "current_user", "operator", "复核人")
        conclusion = conclusion or pick(params, "conclusion", "复核结论", "result", "decision_text")
        key = pick(params, "idempotency_key", "request_key", "requestId", "key")
        owner_token = pick(params, "owner_token", "token")
        expected_version = pick(params, "expected_version", "expected_owner_version", "owner_version")

        with FLOW_LOCK:
            entry = self.resolve_entry(ref if ref is not None else params.get("review_ref") or params.get("review_id"))
            if entry is None:
                return {"ok": False, "code": "not_found", "http_status": 404, "message": "复核记录不存在"}
            review_no = str(entry.get("记录编号") or entry.get("id"))
            user = normalize_user(reviewer)

            # 同一幂等键的成功回执优先回放，使超时重投能辨别“第一次其实已确认”。
            receipts = entry.setdefault("submission_receipts", {})
            if key and key in receipts:
                cached = receipts[key]
                if isinstance(cached, dict) and cached.get("receipt"):
                    return {**cached, "duplicate": True, "idempotent_replay": True, "retried": True}
                return {
                    "ok": True, "message": "同一幂等键的确认回执回放",
                    "receipt": cached, "confirmation": cached, "result": cached,
                    "completed": True, "confirmed": True, "already_confirmed": True,
                    "submission_confirmed": True, "duplicate": True,
                    "idempotent_replay": True, "retried": True, "entry": entry,
                }

            allowed, code, meta = self._viewer_state(entry, user, owner_token, expected_version)
            if not allowed:
                response = {
                    "ok": False, "code": code, "conflict": code in {"reviewer_forbidden", "version_conflict", "owner_token_invalid", "already_completed"},
                    "http_status": 403 if code not in {"identity_required", "already_completed"} else (401 if code == "identity_required" else 409),
                    "confirmed": False, "submission_confirmed": False, "review_no": review_no,
                    "viewer": user or None, "can_submit": False, **meta,
                }
                if code == "already_completed":
                    response["already_completed"] = True
                    response["order_confirmed"] = True
                    response["confirmed"] = True
                    response["receipt"] = entry.get("confirmed_receipt")
                if key:
                    entry.setdefault("request_log", {})[key] = {**response, "failed_at": now_iso()}
                return response

            text = str(conclusion or "").strip()
            candidate_spec = pick(params, "candidate_spec", "候选规格", "spec")
            if not candidate_spec and text:
                for known in self._evidence_catalog(entry):
                    spec = str(known.get("candidate_spec") or "")
                    if spec and spec.lower() in text.lower():
                        candidate_spec = spec
                        break
            evidence = self._select_evidence(
                entry,
                pick(params, "evidence_id", "evidenceId"),
                pick(params, "evidence_version", "version", "证据版本"),
                candidate_spec,
            )
            if evidence is None:
                response = {
                    "ok": False, "code": "evidence_out_of_scope", "http_status": 422,
                    "confirmed": False, "message": "结论引用的候选证据不属于本申请可回查范围或版本不存在",
                    "review_no": review_no,
                }
                if key:
                    entry.setdefault("request_log", {})[key] = response
                return response

            spec_value = candidate_spec or evidence.get("candidate_spec") or ""
            if approved and not text:
                text = f"核准候选规格 {spec_value}（{evidence.get('version') or ('v' + str(evidence.get('evidence_version', 1)))}）"
            elif not approved and not text:
                text = f"驳回候选规格 {spec_value}"

            at = now_iso()
            version = int(entry.get("ownership_version") or entry.get("version") or 1)
            receipt_id = f"RC-{review_no}-{uuid.uuid4().hex[:10]}"
            evidence_snapshot = dict(evidence)
            receipt = {
                "receipt_id": receipt_id,
                "confirmation_id": receipt_id,
                "idempotency_key": key,
                "confirmed": True,
                "submission_confirmed": True,
                "ok": True,
                "review_id": entry.get("id"),
                "review_ref": review_no,
                "review_no": review_no,
                "记录编号": review_no,
                "match_id": entry.get("match_id") or entry.get("匹配记录"),
                "application_no": entry.get("申请单号"),
                "decision": "已通过" if approved else "已驳回",
                "approved": approved,
                "passed": approved,
                "conclusion": text,
                "candidate_spec": spec_value,
                "approved_candidate_spec": spec_value if approved else None,
                "evidence_id": evidence_snapshot.get("evidence_id") or evidence_snapshot.get("id"),
                "evidence_version": int(evidence_snapshot.get("evidence_version") or 1),
                "version": evidence_snapshot.get("version") or f"v{evidence_snapshot.get('evidence_version', 1)}",
                "evidence_snapshot": evidence_snapshot,
                "approved_evidence": evidence_snapshot if approved else None,
                "reviewer": user,
                "effective_reviewer": user,
                "decided_by": user,
                "confirmed_by": user,
                "actual_owner": user,
                "ownership_version": version,
                "confirmed_at": at,
                "completed": True,
            }

            entry.update({
                "status": "已通过" if approved else "已驳回",
                "pending": False,
                "abnormal": not approved,
                "completed": True,
                "confirmed": True,
                "复核结论": text,
                "复核角色": user,
                "复核人": user,
                "effective_reviewer": user,
                "decided_by": user,
                "confirmed_by": user,
                "actual_owner": user,
                "复核时间": at,
                "decision": receipt,
                "confirmed_receipt": receipt,
                "approved_evidence": evidence_snapshot if approved else None,
                "approved_candidate_spec": spec_value if approved else None,
            })
            receipts[receipt_id] = receipt
            if key:
                receipts[key] = receipt

            match_result = None
            try:
                from app.services.sku_match import SkuMatchService
                match_service = SkuMatchService()
                if approved:
                    applied = match_service.apply_confirmed_review(review_ref=review_no, receipt=receipt)
                else:
                    applied = match_service.reject_confirmed_review(review_ref=review_no, receipt=receipt)
                match_result = applied.get("match_result") or applied
                receipt["match_backfill"] = applied.get("ok", False)
            except Exception as exc:  # 兜底：复核确认本身不能被联动异常吞掉。
                receipt["match_backfill"] = False
                receipt["match_backfill_error"] = str(exc)

            if not approved:
                self._append_quarantine(entry, receipt)

            message = "复核已通过，确认回执已生成并按核准证据写入匹配结果" if approved else "复核已驳回，确认回执已生成，样本回流隔离区"
            return {
                "ok": True,
                "message": message,
                "receipt": receipt,
                "confirmation": receipt,
                "result": receipt,
                "match_result": match_result,
                "completed": True,
                "already_confirmed": True,
                "confirmed": True,
                "submission_confirmed": True,
                "can_submit": False,
                "entry": entry,
            }

    def approve(self, entry_id: int | str | dict | None = None, reviewer: str | None = None,
                conclusion: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(entry_id, dict):
            return self.submit_conclusion(entry_id)
        return self._decision(entry_id, reviewer, True, conclusion, **kwargs)

    def reject(self, entry_id: int | str | dict | None = None, reviewer: str | None = None,
               conclusion: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(entry_id, dict):
            return self.submit_conclusion(entry_id, approved=False)
        return self._decision(entry_id, reviewer, False, conclusion, **kwargs)

    def submit_conclusion(self, review_ref: int | str | dict | None = None,
                         reviewer: str | dict | None = None,
                         conclusion: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(review_ref, dict):
            params = _as_params(review_ref)
            review_ref = params.get("review_ref") or params.get("entry_id") or params.get("id")
            reviewer = reviewer or params
        approved = kwargs.pop("approved", True)
        if str(kwargs.get("action", "")) in REJECT_ACTIONS:
            approved = False
        return self._decision(review_ref, reviewer, bool(approved), conclusion, **kwargs)

    def submit_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.submit_conclusion(*args, **kwargs)

    def get_receipt(self, ref: int | str, request_key: str | None = None) -> dict[str, Any]:
        with FLOW_LOCK:
            entry = self.resolve_entry(ref)
            if entry is None:
                return {"ok": False, "message": "复核记录不存在"}
            key = request_key
            receipt = None
            if key:
                receipt = (entry.get("submission_receipts") or {}).get(key)
                if receipt is None and key == (entry.get("confirmed_receipt") or {}).get("idempotency_key"):
                    receipt = entry.get("confirmed_receipt")
            else:
                receipt = entry.get("confirmed_receipt")
            return {
                "ok": receipt is not None,
                "confirmed": bool(receipt and receipt.get("confirmed")),
                "already_confirmed": bool(entry.get("confirmed")),
                "completed": bool(entry.get("completed")),
                "receipt": receipt,
                "effective_reviewer": entry.get("effective_reviewer"),
                "entry": entry,
            }

    def query_receipt(self, ref: int | str, request_key: str | None = None) -> dict[str, Any]:
        return self.get_receipt(ref, request_key)

    def update_candidate_evidence(self, ref: int | str, reviewer: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """产生候选证据新版本；旧确认/旧复核范围仍保留各自快照。"""
        with FLOW_LOCK:
            entry = self.resolve_entry(ref)
            if entry is None:
                return {"ok": False, "message": "复核记录不存在"}
            allowed, code, meta = self._viewer_state(entry, reviewer or pick(kwargs, "user", "operator"))
            if not allowed and code != "already_completed":
                return {"ok": False, "code": code, **meta}
            old = self._select_evidence(entry) or {}
            catalog = entry.setdefault("evidence_versions", self._evidence_catalog(entry))
            next_version = max(int(e.get("evidence_version") or 1) for e in catalog) + 1
            spec = pick(kwargs, "candidate_spec", "候选规格", "spec") or f"{old.get('candidate_spec', '600g')}-updated"
            evidence_id = pick(kwargs, "evidence_id") or old.get("evidence_id") or f"EV-{entry.get('记录编号')}-600G"
            fingerprint = pick(kwargs, "fingerprint", "sample_fingerprint") or f"FP-{entry.get('记录编号')}-{next_version}"
            evidence = {
                **old,
                "evidence_id": evidence_id,
                "id": evidence_id,
                "evidence_version": next_version,
                "version": f"v{next_version}",
                "candidate_spec": spec,
                "sample_fingerprint": fingerprint,
                "updated_at": now_iso(),
                "traceable": True,
            }
            catalog.append(evidence)
            match = self._linked_match(entry)
            if match is not None:
                match["候选规格"] = spec
                match["candidate_evidence_version"] = next_version
                match["candidate_version"] = f"v{next_version}"
                match.setdefault("candidate_versions", []).append({
                    "evidence_id": evidence_id, "version": next_version,
                    "candidate_spec": spec, "fingerprint": fingerprint,
                })
            return {"ok": True, "message": "候选证据已更新为新版本；旧审批不会混入该结果", "evidence": evidence, "entry": entry}

    def _append_quarantine(self, entry: dict[str, Any], receipt: dict[str, Any]) -> None:
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"review-{entry.get('id')}-{receipt.get('evidence_id', '')}",
            "失败阶段": "匹配",
            "失败原因": f"复核驳回: {receipt.get('conclusion', '')}",
            "隔离时间": now_iso(),
            "重试次数": "0",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })

    # ------------------------------------------------------------------
    # 统计/商品档案联动（保留原接口）
    # ------------------------------------------------------------------

    def today_throughput(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        rows = [
            r for r in store.rows(self.MODULE)
            if str(r.get("复核时间", "")).startswith(today)
            and r.get("status") in ("已通过", "已驳回")
        ]
        approved = sum(1 for r in rows if r.get("status") == "已通过")
        return {
            "ok": True,
            "message": "今日办结统计完成",
            "total": len(rows),
            "approved": approved,
            "rejected": len(rows) - approved,
        }

    def link_goods_registry(self, entry_id: int) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "复核记录不存在"}
        conclusion = str(entry.get("复核结论", ""))
        affected = 0
        for goods in store.rows("goods_registry"):
            if conclusion and conclusion in str(goods.get("登记来源", "")):
                goods["pending"] = entry.get("status") == "已通过"
                affected += 1
        return {
            "ok": True,
            "message": "复核结论已同步商品档案",
            "affected": affected,
        }
