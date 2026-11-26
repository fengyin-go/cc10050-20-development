"""路由汇总：专项语义接口先注册，通用资源路由后注册。"""
from __future__ import annotations

from app.routers import resource, review_flow

ROUTERS = [review_flow, resource]
