import threading
from concurrent.futures import ThreadPoolExecutor

from app.seed import SEED_ROWS
from app.services.match_review import MatchReviewService
from app.services.sku_match import SkuMatchService
from app.store import store


def reset():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    return MatchReviewService(), SkuMatchService()


def test_transfer_wins_and_b_completes():
    review, match = reset()
    before_a = review.task_context("M72", "甲")
    assert before_a["can_submit"] is True, before_a
    stranger = review.read_sample("M72", "路人丙")
    assert stranger["ok"] is False and stranger["authorized"] is False

    transfer = review.transfer_task("M72", supervisor="主管", target_reviewer="乙",
                                    idempotency_key="transfer-1")
    assert transfer["ok"] is True, transfer
    assert "移交范围" in transfer and transfer["移交范围"], transfer
    assert any("EV-M72-600G" in item for item in transfer["移交范围"])

    after_transfer_a = review.task_context("M72", "甲", owner_token="stale")
    assert after_transfer_a["completed"] is False
    assert after_transfer_a["can_submit"] is False
    assert after_transfer_a["code"] in {"reviewer_forbidden", "owner_token_invalid"}
    a_sample = review.read_sample("M72", "甲")
    assert a_sample["ok"] is False, a_sample

    b_todo = review.my_todo("乙")
    assert "M72" in b_todo["records"]
    b_ctx = review.task_context("M72", "乙")
    assert b_ctx["completed"] is False and b_ctx["can_submit"] is True

    # 甲的迟到提交/超时重投：转派已先成交，因此不能确认。
    a_late = review.approve("M72", "甲", conclusion="600g", idempotency_key="A-600G-1",
                            expected_version=1)
    assert a_late["ok"] is False, a_late
    assert a_late["confirmed"] is False

    # 模拟甲回执超时后用同一幂等键重提：仍能辨别自己未确认。
    a_retry = review.approve("M72", "甲", conclusion="600g", idempotency_key="A-600G-1",
                             expected_version=1)
    assert a_retry["ok"] is False and a_retry["confirmed"] is False

    b_sample = review.read_sample("M72", "乙")
    assert b_sample["ok"] is True and "600g" in str(b_sample["sample_detail"])

    b_submit = review.approve("M72", "乙", conclusion="核准 600g",
                              evidence_id="EV-M72-600G", evidence_version=1,
                              idempotency_key="B-600G-1")
    assert b_submit["ok"] is True, b_submit
    receipt = b_submit["receipt"]
    assert receipt["effective_reviewer"] == "乙"
    assert receipt["approved_candidate_spec"] == "600g"

    # 乙超时重提返回同一回执，不产生第二个结果。
    b_retry = review.approve("M72", "乙", conclusion="核准 600g",
                             evidence_id="EV-M72-600G", evidence_version=1,
                             idempotency_key="B-600G-1")
    assert b_retry["duplicate"] is True
    assert b_retry["receipt"]["receipt_id"] == receipt["receipt_id"]

    after_b_a = review.task_context("M72", "甲")
    assert after_b_a["completed"] is True and after_b_a["can_submit"] is False
    assert after_b_a["effective_reviewer"] == "乙"
    after_b_b = review.task_context("M72", "乙")
    assert after_b_b["completed"] is True and after_b_b["can_submit"] is False

    # 候选证据随后更新到 v2，不能污染已经确认的 v1。
    updated = match.update_candidate_evidence if hasattr(match, "update_candidate_evidence") else None
    sku_row = store.find("sku_match", int(review.resolve_entry("M72")["match_id"]))
    sku_row["候选规格"] = "650g"
    sku_row["candidate_evidence_version"] = 2

    applied = match.apply_confirmed_review(review_ref="M72")
    assert applied["ok"] is True, applied
    result = applied["match_result"]
    assert result["candidate_spec"] == "600g"
    assert result["evidence_version"] == 1
    assert result["effective_reviewer"] == "乙"
    assert sku_row["候选规格"] == "600g"
    assert sku_row["actual_owner"] == "乙"

    replay = match.apply_confirmed_review(review_ref="M72")
    assert replay.get("duplicate") is True
    print("PASS transfer-wins / ACL / idempotent retry / matcher snapshot")


def test_submit_wins():
    review, match = reset()
    a = review.approve("M72", "甲", conclusion="600g", idempotency_key="A-WIN")
    assert a["ok"] is True
    transfer = review.transfer_task("M72", supervisor="主管", target_reviewer="乙")
    assert transfer["ok"] is False and transfer["already_completed"] is True
    retry = review.approve("M72", "甲", conclusion="600g", idempotency_key="A-WIN")
    assert retry["duplicate"] is True and retry["receipt"]["effective_reviewer"] == "甲"
    applied = match.apply_confirmed_review(review_ref="M72")
    assert applied["match_result"]["effective_reviewer"] == "甲"
    print("PASS submit-wins / transfer rejected / receipt replay")


def test_parallel_only_one_winner():
    for i in range(20):
        review, _ = reset()
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return review.approve("M72", "甲", conclusion="600g", idempotency_key=f"P-{i}")

        def transfer():
            barrier.wait()
            return review.transfer_task("M72", supervisor="主管", target_reviewer="乙")

        with ThreadPoolExecutor(max_workers=2) as pool:
            fs = [pool.submit(submit), pool.submit(transfer)]
            a, t = [f.result() for f in fs]
        wins = int(bool(a.get("ok"))) + int(bool(t.get("ok")))
        assert wins == 1, (a, t)
        entry = review.resolve_entry("M72")
        if a.get("ok"):
            assert entry["status"] == "已通过" and entry["effective_reviewer"] == "甲"
            assert not t["ok"] and t["already_completed"]
        else:
            assert t["ok"] and entry["assignee"] == "乙" and entry["status"] == "待复核"
            assert a["code"] in {"reviewer_forbidden", "version_conflict", "owner_token_invalid"}
    print("PASS 20 parallel submit/transfer races have exactly one winner")


if __name__ == "__main__":
    test_transfer_wins_and_b_completes()
    test_submit_wins()
    test_parallel_only_one_winner()
