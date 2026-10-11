---
id: GFM-001
title: 通用闲鱼监控核心：持久去重、任务隔离与有界AI复评
status: Ready
assignee:
  - '@codex'
created_date: '2026-10-11 11:59'
updated_date: '2026-10-11 12:39'
labels: []
dependencies: []
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
现有原型把判断存到商品上，未知配置靠正则结束，无法复用于其它商品；用户已批准并行实现。核心归本工具仓，Cubi CUB-059 负责运行接入与主机14天真实试运行。管理界面后置。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 共享商品事实而任务判断独立；两个品类配置与两个任务同商品测试通过
- [x] #2 schema1历史数据、议价、观察、决定与通知状态迁移保真并失败回滚；变化复评和队列预算公平
- [x] #3 采集区分空结果、截断、登录风控和解析失败，只关闭自有页面
- [x] #4 AI适配器有界、保留证据缺口、失败可重试；通知outbox独立且确认送达才标sent
- [x] #5 CLI支持prepare/live隔离、不可延长期限、停止到期、互斥、健康检查和运行恢复；定向测试与文档通过
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 调查发现均有处置依据，剩余事项已明确承接
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
协调者先固定contracts/config公共接口；A独占browser/parsers及test_browser，B独占policy/analysis及test_analysis，各自隔离worktree离线实现提交；协调者独占ledger迁移、CLI、通知和联合验收。schema2身份矩阵：items全局(platform,id)一对多observations；monitors以monitor_id拥有期限配置；monitor_items关联monitor/item独立watch/quote/判断；assessments不可变历史，outbox引用assessment和monitor，runs归monitor+mode。单用户单站无需租户/site伪维度。迁移保留schema1全部稳定ID、行数、payload、送达状态、议价和期限，disabled legacy任务承接混合PC/Mac历史；显式映射导入PC关系。旧原型与上游按链接跳过旧ID/全局AI副作用不复用，复用纯JSON parser和后台Reader。测试多任务不同结论/标准变化、事务回滚与孤儿FK；无双写authority，旧格式只为迁移输入。focused tests: PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/cubi --confcutdir=tests/cubi -q -o addopts=--tb=short；新增失败后仅按影响面扩验。真实DB/browser/AI/通知由协调者在CUB-059统一执行。独立复审默认关闭，直接迁移保真与边界测试验收。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
F1旧原型items混入任务判断；F2未知规则被reserve缓存；F3prepare污染live cadence；F4上游Web/API认证风险，本任务独立CLI不启动Web、不导出cookie。全部范围内实施。源原型24项在2026-10-11重新通过，仅作为迁移输入和采集前置基线，不代表通用核心已完成。

已验收通用schema2核心：182项离线测试通过；真实schema1库已备份并迁移，legacy任务disabled保留历史，pc-workstation显式导入两基准，10500议价与11388网页价独立。真实prepare完成2页59条、4详情、3次DeepSeek文本AI，实际served_model=deepseek-flash，预留费用累计US$0.0235374/上限3；混合套餐价与带卡/无卡口径均保留缺口。15条已判断真实快照在内存数据库回放，0新判断/0新outbox；变化复评与多任务合成测试通过。F1改为共享facts+任务关系；F2未知进入有界AI；F3prepare/live轮次隔离；F4不启动上游Web/API，认证风险接口未采用；F5新降价真实为直接点击，已修复68采集测试并完成现场新发布2页60条/新降价1页30条/详情11388，全部24用户页面原样保留，只关闭自有target。通知成功/明确失败/不明与互斥、停用、原期限内resume、到期均有测试。两worker已ff整合；源代码验收候选可部署，Cubi实际service/timer与个人Telegram送达由CUB-059持有。
<!-- SECTION:NOTES:END -->
