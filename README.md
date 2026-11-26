# 商品价格监控采集平台

面向多区域站点的商品价格抓取、规格归一、比价计算与变价告警的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8020/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5200/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8020`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集站点目录 | `shop_site` | 站点配置 | 站点名称、站点域名、采集区域 |
| 价格爬取任务 | `price_crawler` | 爬取任务 | 任务名称、目标站点、商品关键词 |
| 页面解析模板 | `page_parser` | 解析模板 | 模板名称、适用站点、选择器路径 |
| 规格归一器 | `spec_normalizer` | 规格归一规则 | 规则名称、源规格文本、归一规格 |
| 货币换算器 | `currency_conv` | 汇率快照 | 快照日期、源货币、目标货币 |
| 价格基线库 | `price_baseline` | 价格基线 | 商品标识、归一规格、基线价格 |
| 比价计算器 | `price_compare` | 比价结果 | 比价批次、商品标识、最低价站点 |
| 变价告警规则 | `price_alert` | 告警规则 | 规则名称、监控商品、价差阈值 |
| 商品登记中心 | `goods_registry` | 商品档案 | 商品标识、商品名称、品牌 |
| 规格匹配器 | `sku_match` | 匹配记录 | 匹配批次、商品档案、候选规格 |
| 页面样本库 | `sample_store` | 页面样本 | 样本批次、站点名称、原始HTML摘要 |
| 解析质量审计 | `parse_audit` | 审计明细 | 审计时间、解析模板、样本总数 |
| 采集调度计划 | `crawl_schedule` | 调度计划 | 计划名称、关联任务、调度周期 |
| 抓取频控 | `rate_limit` | 频控规则 | 规则名称、目标站点、每分钟上限 |
| 代理线路池 | `proxy_route` | 代理线路 | 线路标识、线路区域、可用性评分 |
| 失败隔离区 | `failure_quarantine` | 隔离记录 | 样本指纹、失败阶段、失败原因 |
| 比价导出通道 | `price_export` | 导出任务 | 任务名称、商品范围、导出格式 |
| 变价历史流水 | `change_history` | 变价记录 | 商品标识、旧价格、新价格 |
| 人工复核队列 | `match_review` | 复核记录 | 记录编号、匹配记录、复核结论 |
| 采集量指标 | `collect_metrics` | 指标记录 | 统计时间、站点名称、抓取量 |

## 转派审单与 M72 演练

人工复核队列内置 `M72 / 600g` 场景：初始只有甲认领，关联可回查证据 `EV-M72-600G v1`
和独立样本 `#72`。页面进入「人工复核队列」即可使用甲入口、主管转派和乙的待办入口；
「规格匹配器」页只展示已确认回执实际核准的证据版本与负责人。

常用接口：

```bash
# 重置为“只有甲已认领”的初始场景
curl -X POST http://127.0.0.1:8020/api/match_review/scenario/reset

# 两个入口的办结/可提交状态
curl 'http://127.0.0.1:8020/api/match_review/M72/context?reviewer=甲'
curl 'http://127.0.0.1:8020/api/match_review/M72/context?reviewer=乙'

# 未认领/未接手人读取样本会被拒绝；当前归属人可读取
curl 'http://127.0.0.1:8020/api/match_review/M72/sample?reviewer=乙'

# 主管转派给乙，回执列出移交范围并递增 ownership_version
curl -X POST http://127.0.0.1:8020/api/match_review/M72/transfer \
  -H 'Content-Type: application/json' \
  -d '{"supervisor":"主管","target_reviewer":"乙"}'

# 乙用同一原任务提交；甲的旧身份/旧版本会被拒绝
curl -X POST http://127.0.0.1:8020/api/match_review/M72/submit \
  -H 'Content-Type: application/json' \
  -d '{"reviewer":"乙","conclusion":"核准 600g","idempotency_key":"B-M72-600G-1"}'

# 规格匹配器读取已确认归属，并按确认快照写入匹配结果
curl -X POST http://127.0.0.1:8020/api/sku_match/apply-confirmed-review \
  -H 'Content-Type: application/json' -d '{"review_ref":"M72"}'
```

后端可用 `backend/.venv/bin/python backend/verify_review_handoff.py` 复跑权限、
幂等重投、证据版本快照和 20 组并行提交/转派竞争校验。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
