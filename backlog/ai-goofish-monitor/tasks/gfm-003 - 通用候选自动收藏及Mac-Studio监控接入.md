---
id: GFM-003
title: 通用候选自动收藏及Mac Studio监控接入
status: Ready
assignee: []
created_date: '2026-10-11 15:55'
labels: []
dependencies: []
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
用户要求遇到合适的新候选自动加入闲鱼收藏，并新增M5 Ultra 256GB监控；现有核心只有只读收藏。收藏是继续核实的书签，不是购买、联系卖家或付款授权。产品偏好和真实商品数据留在private配置，默认关闭新增能力，账户不匹配时禁止写入。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 通用收藏仅在显式启用的active live任务中对当前有效candidate执行，prepare/reserve/rejected/到期/旧判定不执行
- [ ] #2 收藏前核对当前账号和精确商品，只添加不取消；去重、有界执行且不确定结果先读状态而非重复点击
- [ ] #3 M5 Ultra 256GB及合适旧收藏接入现有每日新上架/降价/去重，按可信同配置参照判断价值，旧PC历史与截止不变
- [ ] #4 边界负例和巡检集成测试通过，真实登录态读取/收藏结果有证据，提交仅到自己的fork
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 调查发现均有处置依据，剩余事项已明确承接
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 复用现有Browser/Reader、candidate判定与SQLite meta，核对平台登录账号及收藏控件；不新增DDL或上游Web/API写入。2. 收藏作为显式任务collection_policy：默认关闭，最多一条/轮，对已判断candidate补收藏并验证，保留intent/uncertain记录及同账号同商品去重；新任务复用。3. 私人Mac条件基于官方同配置价格及交易证据；保留独立旧M3参考，更新PC整体价值条件。4. 聚焦自动收藏正负边界、配置和巡检mock测试，首轮真实读取及有合适候选时真实收藏核验；历史保持、runtime health、diff/status后提交push fork。
<!-- SECTION:PLAN:END -->
