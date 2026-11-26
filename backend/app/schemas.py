"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    # 专项流程会回传 receipt/handover/task_view/match_result 等字段，不能被响应模型丢弃。
    model_config = ConfigDict(extra="allow")

    ok: bool
    message: str = ""
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 允许动作接口携带复核人、转派对象、证据版本、幂等键等顶层业务参数。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    request_key: str | None = None
    requestId: str | None = None
    idempotency_key: str | None = None
    params: dict[str, Any] | None = None
    values: dict[str, Any] | None = None
