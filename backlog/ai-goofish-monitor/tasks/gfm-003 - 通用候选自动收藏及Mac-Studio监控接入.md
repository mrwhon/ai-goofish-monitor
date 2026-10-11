---
id: GFM-003
title: 通用候选自动收藏及Mac Studio监控接入
status: Done
assignee: []
created_date: '2026-10-11 15:55'
updated_date: '2026-10-11 16:19'
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
- [x] #1 通用收藏仅在显式启用的active live任务中对当前有效candidate执行，prepare/reserve/rejected/到期/旧判定不执行
- [x] #2 收藏前核对当前账号和精确商品，只添加不取消；去重、有界执行且不确定结果先读状态而非重复点击
- [x] #3 M5 Ultra 256GB及合适旧收藏接入现有每日新上架/降价/去重，按可信同配置参照判断价值，旧PC历史与截止不变
- [x] #4 边界负例和巡检集成测试通过，真实登录态读取/收藏结果有证据，提交仅到自己的fork
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 调查发现均有处置依据，剩余事项已明确承接
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. 复用现有Browser/Reader、candidate判定与SQLite meta，核对平台登录账号及收藏控件；不新增DDL或上游Web/API写入。2. 收藏作为显式任务collection_policy：默认关闭，最多一条/轮，对已判断candidate补收藏并验证，保留intent/uncertain记录及同账号同商品去重；新任务复用。3. 私人Mac条件基于官方同配置价格及交易证据；保留独立旧M3参考，更新PC整体价值条件。4. 聚焦自动收藏正负边界、配置和巡检mock测试，首轮真实读取及有合适候选时真实收藏核验；历史保持、runtime health、diff/status后提交push fork。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
发现与处置：F1 首轮77项包含异芯片和96GB/预售，不能以标价低视为256GB候选；私有条件将便宜初筛与同配置评估分开。F2 收藏有平台账号写入边界，已从当前登录页正常loginuser响应确认账号（仅私有配置记录）；核心必须实时核对该响应、精确详情ID和收藏按钮，只添不删。F3 不增加DDL：既有items/monitor_items关系和global meta可表达单平台账号-item动作回执；各任务判定独立，收藏效果同账号同商品共享，历史不回填。F4 官网只读工具页面省略动态价格；在官方配置页选择配置读取实际价格，未进购物车或提交订单。

F4已解决：2026-10-12官网配置器实选256GB/2TB，30/64核80749元、36/80核90499元（无AC/额外软件）；支持规格页遗漏低核256GB，但实际配置器允许，私有条件已纠正，不能判卖家低核配置虚假。F5实际收藏控制已核验：已收藏商品返回already_saved，零点击，登录user响应匹配私人expected_account_id；人工不制造不合适商品收藏用于测试。收藏写入正例由隔离UI测试覆盖，真实新收藏只在出现合适candidate时执行。所有目前报价仍是待核实声明；候选收藏为核实队列。

验收与处置：F1明确非目标内存/芯片和求购租赁已廉价排除，未知保留；live11成功，已见80项。F2新增33条测试覆盖live/prepare、reserve/rejected、停用/到期、跨任务去重、漂移、账号变化、控件歧义、取消/不确定结果，真实已收藏控制匹配身份零点击。F3旧1159观察、453评估、2outbox、8run逐行保持，旧activation/expiry不变，integrity/FK通过。F4官网动态报价及来源差异已落私人criteria。F5无新candidate，真实新收藏0；真实登录态已有收藏验证，新增正例由隔离UI覆盖，仅出现合适候选时添加。完整tests/cubi235 passed(57.38s)，diff-check通过；actual live10/11均ok且未重发通知，Cubi registered xianyu-monitor PASS、health2 active、next2026-10-12 10:30。private receipt/live文件留存且配置/DB备份保留。全部发现有处置，无剩余实现项；硬件实际质量/是否购买需另行核验。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
新增默认关闭、显式账户绑定的通用candidate收藏：active live、当前判定与事实、预算、账号和UI检查，动作前intent、不确定只读对账、同账号跨任务去重、加入后跟踪降价。私人配置已启用PC/Mac两任务，M5 Ultra256GB和旧M3 Ultra256GB独立对照，官方两核心配置按各自价格评估；不把书签当购买保证。235项测试、实际live10/11、既有收藏控制账号核验、registered runtime及历史逐行保持通过。本轮新增收藏0，因为无通过判定的候选；真实新增仅候选出现时执行。实现56ce6b9已推送origin/master(mrwhon fork)，上游未修改。
<!-- SECTION:FINAL_SUMMARY:END -->
