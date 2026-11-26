"""规格匹配器领域服务：把站点SKU匹配到商品档案的归一规格，负责匹配置信度判定与人工复核分流。

数据链路位置：规格对齐层，消费规格归一器推送的候选规格与商品档案中心的主数据，
低置信度结果转人工复核队列，高置信度结果回填商品档案。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SkuMatchService(BaseModuleService):
    MODULE = "sku_match"
    ENTITY = "匹配记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["匹配批次", "商品档案", "候选规格", "匹配置信度", "匹配状态"]
    REQUIRED_FIELDS = ["匹配批次", "商品档案", "候选规格"]
    STATUS_ORDER = ["待匹配", "已匹配", "人工复核", "匹配失败"]
    ACTION_RULES = {"执行匹配": "已匹配", "转人工复核": "人工复核"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：规格匹配与置信度分流 ----

    def run_match(self, entry_id: int, confidence: float) -> dict[str, Any]:
        """执行规格匹配，按置信度分流到已匹配或人工复核。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        goods_name = entry.get("商品档案", "")
        goods = next(
            (g for g in store.rows("goods_registry") if g.get("商品标识") == goods_name), None
        )
        if goods is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"商品档案 {goods_name} 不存在"}
        if confidence >= 0.95:
            entry["status"] = "已匹配"
            entry["pending"] = False
        else:
            entry["status"] = "人工复核"
            entry["pending"] = True
        entry["匹配置信度"] = f"{confidence:.2f}"
        return {
            "ok": True,
            "message": "匹配执行完成",
            "result_status": entry.get("status"),
            "confidence": entry.get("匹配置信度"),
        }

    def escalate_review(self, entry_id: int) -> dict[str, Any]:
        """把低置信度匹配记录升级到人工复核队列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "人工复核":
            return {"ok": False, "message": "记录不存在或不处于人工复核状态"}
        rows = store.rows("match_review")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        application_no = str(entry.get("申请单号") or f"APP-M{entry_id}")
        evidence_id = entry.get("candidate_evidence_id") or f"EV-M{entry_id}-{rid}"
        evidence_version = int(entry.get("candidate_evidence_version") or 1)
        evidence = {
            "evidence_id": evidence_id,
            "id": evidence_id,
            "evidence_version": evidence_version,
            "version": entry.get("candidate_version") or f"v{evidence_version}",
            "application_no": application_no,
            "candidate_spec": entry.get("候选规格", ""),
            "sample_id": entry.get("sample_id"),
            "sample_fingerprint": entry.get("candidate_fingerprint"),
            "traceable": True,
        }
        rows.append({
            "id": rid,
            "记录编号": f"SM-{entry_id}-{rid}",
            "匹配记录": str(entry.get("id", "")),
            "复核结论": f"置信度 {entry.get('匹配置信度', '')}",
            "复核角色": "规格审核员",
            "复核时间": datetime.now(timezone.utc).isoformat(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
            "申请单号": application_no,
            "match_id": entry.get("id"),
            "review_no": f"SM-{entry_id}-{rid}",
            "evidence_scope": [{
                "evidence_id": evidence_id,
                "evidence_version": evidence_version,
                "candidate_spec": entry.get("候选规格", ""),
            }],
            "evidence_versions": [evidence],
        })
        return {"ok": True, "message": "已升级人工复核", "review_id": rid, "evidence": evidence}

    def apply_review_result(self, entry_id: int, approved: bool) -> dict[str, Any]:
        """应用复核结论：通过则回填档案，驳回则进失败隔离。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        confirmed = self.apply_confirmed_review(match_id=entry_id)
        if confirmed.get("ok") and confirmed.get("applied"):
            return confirmed
        if approved:
            entry["status"] = "已匹配"
            entry["pending"] = False
            return {"ok": True, "message": "复核通过，匹配已确认"}
        return self._mark_rejected(entry, "人工复核驳回")

    def _find_review(self, review_ref: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        review_id = kwargs.get("review_id") or kwargs.get("review_no") or kwargs.get("ref") or review_ref
        if review_id is not None:
            text = str(review_id)
            if text.isdigit():
                review = store.find("match_review", int(text))
                if review is not None:
                    return review
            review = next((r for r in store.rows("match_review") if str(r.get("记录编号")) == text), None)
            if review is not None:
                return review
        match_id = kwargs.get("match_id") or kwargs.get("entry_id")
        if match_id is not None:
            text = str(match_id)
            if text.isdigit():
                review = next((r for r in store.rows("match_review") if str(r.get("匹配记录")) == text), None)
                if review is not None:
                    return review
        return None

    def apply_confirmed_review(
        self,
        review_ref: Any = None,
        receipt: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """只读取已确认的复核归属，并按实际核准的候选证据版本写匹配结果。"""
        review = kwargs.pop("review", None) or self._find_review(review_ref, **kwargs)
        if review is None and review_ref is not None and kwargs.get("match_id") is None:
            # 兼容第一个位置参数直接传 sku_match id 的调用。
            maybe_match = store.find(self.MODULE, int(review_ref)) if str(review_ref).isdigit() else None
            if maybe_match is not None:
                review = self._find_review(match_id=maybe_match.get("id"))
                kwargs["match_id"] = maybe_match.get("id")
        if review is None:
            review = self._find_review(**kwargs)
        if review is None:
            return {"ok": False, "code": "review_not_found", "message": "找不到已确认的复核归属数据"}

        receipt = receipt or review.get("confirmed_receipt") or review.get("confirmation")
        if not isinstance(receipt, dict) or not receipt.get("confirmed"):
            return {
                "ok": False, "code": "review_not_confirmed", "not_confirmed": True,
                "message": "规格匹配器只读取已确认回执；该复核尚无生效确认",
                "review_no": review.get("记录编号"),
            }
        if receipt.get("approved") is False or receipt.get("decision") == "已驳回":
            return self.reject_confirmed_review(review_ref=review.get("记录编号"), receipt=receipt, **kwargs)

        match_id = (
            kwargs.get("match_id") or kwargs.get("entry_id")
            or receipt.get("match_id") or review.get("match_id") or review.get("匹配记录")
        )
        match: dict[str, Any] | None = None
        if match_id not in (None, "") and str(match_id).isdigit():
            match = store.find(self.MODULE, int(match_id))
        if match is None:
            review_no = review.get("记录编号")
            match = next((m for m in store.rows(self.MODULE) if m.get("review_no") == review_no), None)
        if match is None:
            return {"ok": False, "code": "match_not_found", "message": "复核回执关联的规格匹配记录不存在"}

        receipt_id = str(receipt.get("receipt_id") or receipt.get("confirmation_id") or "")
        same_receipt = match.get("applied_receipt_id") == receipt_id and match.get("status") == "已匹配"
        if match.get("applied_receipt_id") and not same_receipt and not kwargs.get("force"):
            return {
                "ok": False, "code": "receipt_conflict", "conflict": True,
                "message": "匹配结果已采用另一版确认回执，不能混用",
                "applied_receipt_id": match.get("applied_receipt_id"), "entry": match,
            }

        snapshot = receipt.get("evidence_snapshot") or receipt.get("approved_evidence") or review.get("approved_evidence") or {}
        spec = (
            receipt.get("approved_candidate_spec")
            or snapshot.get("candidate_spec")
            or receipt.get("candidate_spec")
            or review.get("approved_candidate_spec")
        )
        evidence_id = receipt.get("evidence_id") or snapshot.get("evidence_id") or snapshot.get("id")
        evidence_version = int(receipt.get("evidence_version") or snapshot.get("evidence_version") or 1)
        reviewer = receipt.get("effective_reviewer") or receipt.get("reviewer") or review.get("effective_reviewer") or review.get("复核角色")
        result = {
            "match_id": match.get("id"),
            "review_id": review.get("id"),
            "review_no": review.get("记录编号"),
            "receipt_id": receipt_id,
            "source": "confirmed_match_review",
            "status": "已匹配",
            "candidate_spec": spec,
            "approved_candidate_spec": spec,
            "candidate_evidence_id": evidence_id,
            "evidence_id": evidence_id,
            "evidence_version": evidence_version,
            "version": receipt.get("version") or snapshot.get("version") or f"v{evidence_version}",
            "effective_reviewer": reviewer,
            "actual_owner": reviewer,
            "decided_by": receipt.get("decided_by") or reviewer,
            "confirmed_at": receipt.get("confirmed_at"),
            "application_no": receipt.get("application_no") or review.get("申请单号"),
        }

        current_spec = match.get("候选规格")
        if current_spec and spec and str(current_spec) != str(spec):
            result["latest_candidate_spec_before_apply"] = current_spec
            match["latest_candidate_spec"] = current_spec
            match["candidate_drift"] = True

        match.update({
            "status": "已匹配",
            "pending": False,
            "abnormal": False,
            "匹配状态": "复核核准",
            "候选规格": spec,
            "approved_candidate_spec": spec,
            "approved_evidence_id": evidence_id,
            "approved_evidence_version": evidence_version,
            "applied_evidence_version": evidence_version,
            "applied_receipt_id": receipt_id,
            "review_id": review.get("id"),
            "review_no": review.get("记录编号"),
            "effective_reviewer": reviewer,
            "actual_owner": reviewer,
            "review_confirmed_by": reviewer,
            "match_result": result,
            "商品匹配结果": result,
        })
        if same_receipt:
            return {"ok": True, "applied": False, "duplicate": True, "idempotent_replay": True,
                    "message": "该确认回执已写入匹配结果；已按确认快照校正被后续候选更新扰动的字段",
                    "entry": match, "match_result": result}
        return {"ok": True, "applied": True, "message": "规格匹配结果已按实际核准的候选证据版本写入", "entry": match, "match_result": result}

    def reject_confirmed_review(
        self,
        review_ref: Any = None,
        receipt: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        review = kwargs.pop("review", None) or self._find_review(review_ref, **kwargs)
        receipt = receipt or (review or {}).get("confirmed_receipt")
        match_id = kwargs.get("match_id") or (receipt or {}).get("match_id") or (review or {}).get("匹配记录")
        match = store.find(self.MODULE, int(match_id)) if str(match_id).isdigit() else None
        if match is None and review is not None:
            match = next((m for m in store.rows(self.MODULE) if m.get("review_no") == review.get("记录编号")), None)
        if match is None:
            return {"ok": False, "message": "驳回回执关联的匹配记录不存在"}
        reason = f"复核驳回: {(receipt or {}).get('conclusion', '')}"
        return self._mark_rejected(match, reason)

    def _mark_rejected(self, entry: dict[str, Any], reason: str) -> dict[str, Any]:
        entry["status"] = "匹配失败"
        entry["pending"] = False
        entry["abnormal"] = True
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"match-{entry.get('id')}",
            "失败阶段": "匹配",
            "失败原因": reason or "人工复核驳回",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "0",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "applied": True, "message": "复核驳回，样本已隔离", "quarantine_id": fid, "entry": entry}

    def read_confirmed_assignment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.apply_confirmed_review(*args, **kwargs)

    def apply_review_ownership(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.apply_confirmed_review(*args, **kwargs)

    def write_match_result_from_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.apply_confirmed_review(*args, **kwargs)

    def sync_confirmed_review(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.apply_confirmed_review(*args, **kwargs)

    def consume_normalized(self, entry_id: int) -> dict[str, Any]:
        """消费规格归一器推送的候选规格完成对齐。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "记录不存在"}
        spec = entry.get("候选规格", "")
        matched_rule = next(
            (r for r in store.rows("spec_normalizer")
             if r.get("status") == "已启用" and r.get("归一规格") == spec), None
        )
        if matched_rule is None:
            entry["status"] = "匹配失败"
            entry["abnormal"] = True
            return {"ok": False, "message": f"候选规格 {spec} 没有启用中的归一规则"}
        entry["匹配状态"] = "归一对齐"
        return {
            "ok": True,
            "message": "候选规格已对齐归一规则",
            "rule": matched_rule.get("规则名称", ""),
        }

    def link_goods_baseline(self, entry_id: int) -> dict[str, Any]:
        """检查匹配成功后商品是否具备基线价格。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已匹配":
            return {"ok": False, "message": "记录不存在或未匹配"}
        goods_name = entry.get("商品档案", "")
        has_baseline = any(
            b.get("商品标识") == goods_name and b.get("status") == "已生效"
            for b in store.rows("price_baseline")
        )
        return {
            "ok": True,
            "message": "基线关联检查完成",
            "goods": goods_name,
            "has_baseline": has_baseline,
        }

    def batch_summary(self, batch: str) -> dict[str, Any]:
        """汇总匹配批次内各状态数量并上报指标。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("匹配批次") == batch]
        if not rows:
            return {"ok": False, "message": f"批次 {batch} 不存在"}
        matched = sum(1 for r in rows if r.get("status") == "已匹配")
        review = sum(1 for r in rows if r.get("status") == "人工复核")
        return {
            "ok": True,
            "message": "批次汇总完成",
            "total": len(rows),
            "matched": matched,
            "need_review": review,
            "failed": len(rows) - matched - review,
        }
