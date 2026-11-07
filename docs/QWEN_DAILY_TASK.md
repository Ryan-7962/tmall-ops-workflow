# 取数 Agent 每日增量任务书

从现在起，你只负责取数并交付真实数据行，不负责月度、同比、ROI、占比计算，不重建历史快照，不修改 HTML。

## 已经入库的数据不要重复抓

稳定历史已经迁移。只有源端修订或下游明确指出某个日期缺失、对账失败时，才补该日期。

## 每天交付

统一写入 `data/incremental/`。文件必须包含实际数据行，不得只给 schema、行数或探测摘要。

### 1. 店铺日事实：只取 N-1

文件：`store_day_<END>.csv`

```text
date,scope,uv,pv,payment_amount,payment_buyers,product_visitors,cart_users,
rival_uv,rival_payment_amount,rival_payment_buyers
```

固定已验收的 `scope`。核心指标缺失就保留空并报告，不得填 0。若改用其他终端或来源，必须另标独立 scope。

### 2. 万相台：N-1 加近 14 天归因回补

文件：`wxt_day_<END>.csv`

```text
date,channel,attribution_scope,ad_spend,attributed_payment_amount,
clicks,impressions,cart_adds,total_orders
```

近 14 天重取只用于归因回补。数据库会覆盖同一主键并保存修订，不累加。

### 3. 一级类目：只取 N-1

文件：`lingyang_category_day_<END>.csv`

```text
date,cate_level1_name,uv,payment_amount,successful_refund_amount,payment_buyers,cart_users
```

只交一级类目日事实，不交多层级混合汇总。

### 4. SKU：只取 N-1 全量实际行

文件：`lingyang-sku-<END>.json`，字段见 `DATA_CONTRACT.md`。

商品近 7 天聚合、成交商品数和标题补全由下游固定脚本处理。

### 5. 商品与流量日事实

- 商品：日期、商品 ID/名称、支付、退款、商品访客、买家、件数、PV、加购、新老客、搜索、类目和图片。
- 流量：固定单一归属原则，保留渠道 ID/名称、父渠道、层级、商品 ID、UV、PV、支付、买家、加购和新客。
- TOP 榜必须标部分覆盖，不得冒充全量。
- 禁止跨归属原则求和。

### 6. 过渡期窗口原始块

在完整商品和流量日事实尚未验收前，仍交付：

```text
window_<1d|3d|7d|14d|30d>_<END>.json
```

保留 `cur/prev/ly` 与 `overview/channels/items/promoItems`，不计算月度，不改字段。收到下游切换通知后再停止。

## 禁止事项

- 不每天重抓整年或全部历史。
- 不重复交付已通过校验且没有修订的日期。
- 不计算 monthly、payRate、ROI、share 或商品近 7 天聚合。
- 不把窗口汇总均摊成日事实，不跨终端、来源或归因口径拼接。
- 不修改页面，不创建或发布线上节点。

每天只汇报：END、各文件实际行数、回补日期、失败或空数据项。
