# 运行与故障处理

## 每日推荐节奏

| 时间 | 动作 |
|---|---|
| 10:00 | 取数 Agent 交付 N-1 增量文件与必要的万相台回补 |
| 10:10 | 幂等入库、质量门禁、生成 HTML、可选固定页发布 |
| 10:20 | 从数据库导出生成经营日报并抽查关键数字 |
| 10:30 | 按需推送日报或异常告警 |

时间只是示例，可按数据平台就绪时间调整。

## 常用命令

```bash
# 只入库
python3 src/daily_pipeline.py ingest --config pipeline_config.json

# 查看事实表行数和日期范围
python3 src/daily_pipeline.py status --config pipeline_config.json

# 入库、校验、生成页面
python3 src/daily_pipeline.py run --config pipeline_config.json

# 同时更新既有固定页面
python3 src/daily_pipeline.py run --config pipeline_config.json --publish

# 仅从既有数据库重新导出
python3 src/daily_pipeline.py export --config pipeline_config.json

# 生成日报
PIPELINE_CONFIG=pipeline_config.json python3 src/make_report.py
```

## 发布前准备

1. 已经存在一个 WorkBuddy Library Page，不使用创建新节点的方式。
2. 本地 `pipeline_config.json` 配置该节点的 `page_node_id`。
3. 设置 `WORKBUDDY_LIBRARY_SKILL_DIR` 为本机 WorkBuddy library 技能目录。
4. 先执行发布演练：

```bash
python3 src/publish_dashboard.py --config pipeline_config.json --dry-run
```

## 发布保护

- 每次发布重新获取当次线上基线。
- 只替换 `window.__DASH__` 和 `window.__LY__` 两段业务数据。
- 页面壳或功能脚本出现范围外变化即停止。
- 忽略管线生成时间后，业务内容无变化则跳过发布。
- 提交后重新发布并回读，核对数据截止日和关键功能标记。
- 任一步失败，线上保留上一版；不自动新建同名页面。

## 校验报告

默认写入：

```text
state/daily-pipeline/validation-<YYYY-MM-DD>.json
```

常见失败：

| 错误 | 处理方式 |
|---|---|
| 数据截止日不是昨天 | 检查取数任务是否完成，不修改日期伪装新鲜 |
| 万相台与店铺截止日不一致 | 补齐同一 END，不跨来源拼接 |
| 一级类目或 SKU 近 7 天不完整 | 补真实缺失日期，不填 0 |
| 类目与商品成交总额不一致 | 检查是否混入 LEAF、重复层级或重复 SKU |
| 窗口未更新 | 补对应窗口原始块，不改写窗口日期 |
| manifest 哈希不一致 | 重新交付文件，不手工绕过校验 |
| 页面壳差异异常 | 停止发布，重新核对线上基线 |

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试覆盖：

- 相同文件重复导入不会增加事实；
- 万相台归因回补只更新同一主键并保留修订；
- 增量文件扩展日期而不重写历史；
- 去年未结束月只统计相同天数；
- 商品主数据只补缺失标题和图片，不覆盖已有值。

## Git 提交前检查

- 不上传 `pipeline_config.json`。
- 不上传数据库、状态目录、真实快照和线上节点。
- 不上传账号、Cookie、Token 或个人绝对路径。
- 运行单元测试并检查 `.gitignore` 是否生效。
