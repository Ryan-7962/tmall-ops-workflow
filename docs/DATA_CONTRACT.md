# 数据契约

## 1. 总体原则

- 数据库是唯一事实源，页面和日报不再各自计算一套数字。
- 原始文件按 SHA-256 去重，事实按业务主键幂等写入。
- 缺失保持缺失，不填 0，不跨口径拼接。
- 稳定历史只迁移一次；日常只补 N-1 和明确需要修订的日期。

## 2. 输入目录

```text
data/
  snapshot.js                     # 首次历史基线，可选后续兼容输入
  lingyang.js                     # 首次商品聚合元数据基线
  lingyang-sku-<END>.json         # SKU 实际数据行
  incremental/
    store_day_<END>.csv
    wxt_day_<END>.csv
    window_<1d|3d|7d|14d|30d>_<END>.json
    lingyang_category_day_<END>.csv
    lingyang-sku-<END>.json
  tmall-workbench/
    manifest.json
    lingyang_category_day/
    product_day/
    product_master/
    promotion_channel_day/
    shop_flow_day/
```

只有包含实际数据行的文件才属于可导入数据。schema、行数说明或探测摘要不能冒充事实文件。

## 3. 店铺日事实

`store_day_<END>.csv`：

```text
date,scope,uv,pv,payment_amount,payment_buyers,product_visitors,cart_users,
rival_uv,rival_payment_amount,rival_payment_buyers
```

- 主键：`date + scope`
- 核心指标缺失时导入失败，不得用 0 补齐。
- 不同终端或来源必须使用不同 `scope`，不得相互覆盖。

## 4. 万相台日事实

`wxt_day_<END>.csv`：

```text
date,channel,attribution_scope,ad_spend,attributed_payment_amount,
clicks,impressions,cart_adds,total_orders
```

- 主键：`date + channel + attribution_scope`
- 日常取 N-1 与近 14 天，用于归因回补。
- 回补更新同一行，并写入修订历史；不得把新旧值相加。

## 5. 一级类目

`lingyang_category_day_<END>.csv`：

```text
date,cate_level1_name,uv,payment_amount,successful_refund_amount,payment_buyers,cart_users
```

- 主键：`date + cate_level1_name`
- 只接一级类目，不与 LEAF 或多层级混合求和。
- 页面需要最近 7 个连续日期的完整覆盖。

## 6. SKU 日事实

`lingyang-sku-<END>.json`：

```json
{
  "meta": {"source": "source-name", "window": "<START>~<END>"},
  "rows": [{
    "date": "YYYY-MM-DD",
    "itemId": "product-id",
    "itemName": "product-title",
    "skuId": "sku-id",
    "skuName": "sku-name",
    "payAmt": 0,
    "payByr": 0,
    "payItm": 0,
    "cart": 0,
    "crtAmt": 0
  }]
}
```

- 主键：`date + item_id + sku_id + sku_name`
- 商品聚合由固定脚本完成，不在取数端重复计算。
- 示例中的 0 只表示合法零值字段结构；真实缺失不能用 0 代替。

## 7. 窗口原始块

过渡期内保留 `window_<WINDOW>_<END>.json`，包含原有：

```text
cur / prev / ly
overview / channels / items / promoItems
```

窗口块是预设口径快照，不能均摊成日事实。商品和流量完整日事实通过验收后，再切换到本地窗口聚合。

## 8. 标准数据包

`manifest.json` 仅接收 `status=passed` 条目。文件路径不得越界，SHA-256 必须一致，业务键不得重复。

当前支持的标准域：

- `store_day`
- `product_day`
- `product_master`
- `promotion_channel_day`
- `promotion_product_day`
- `promotion_campaign_day`
- `promotion_unit_day`
- `shop_flow_day`

TOP 榜文件必须保留部分覆盖说明，不得标记成全量。

## 9. 首次基线与页面模板

首次完整 `run` 需要：

1. 当年及去年店铺日事实基线；
2. 万相台日事实基线；
3. 最近 7 天一级类目和 SKU 实际行；
4. 五个窗口基线（按配置）；
5. 本地看板模板：

```text
dashboard/
  index.html
  app.js
  dashboard-online.html   # 含可复用的内联 ECharts 基线
  data/
```

`index.html` 必须唯一引用：

```text
data/snapshot.js
data/lingyang.js
app.js
```

公开仓库不包含任何店铺私有模板或真实历史数据。

## 10. 核心事实表

- `source_files`：文件哈希和来源留痕
- `store_daily_facts`：店铺日事实
- `wxt_daily_facts`：万相台当前事实
- `wxt_daily_revisions`：万相台归因修订
- `standard_facts`：标准数据包事实
- `category_daily_facts`：一级类目日事实
- `sku_daily_facts`：SKU 日事实
- `window_snapshots`：窗口原始快照
- `product_master`：商品 ID 与标题、图片的稳定维度
