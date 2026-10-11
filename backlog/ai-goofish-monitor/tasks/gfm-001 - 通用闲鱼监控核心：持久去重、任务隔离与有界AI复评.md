---
id: GFM-001
title: 通用闲鱼监控核心：持久去重、任务隔离与有界AI复评
status: Done
assignee:
  - '@codex'
created_date: '2026-10-11 11:59'
updated_date: '2026-10-11 12:46'
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
F1旧原型items混入任务判断；F2未知规则被reserve缓存；F3prepare污染live cadence；F4上游Web/API认证风险，本任务独立CLI不启动Web、不导出cookie。源原型24项重新通过，仅为迁移与采集前置基线。F1已改为共享facts+任务关系；F2未知进入有界AI；F3prepare/live轮次隔离；F4按用户界面后置边界不采用上游接口。通用schema2核心182项离线测试通过；真实旧库备份迁移保真，legacy disabled、当前任务显式映射导入，网页标价和人工议价独立。prepare有界采集/真实文本AI通过，模型与费用回执只留私人账本，套餐混合报价及带卡/无卡比较均保留缺口。已判断真实快照在内存数据库回放零新判断/零事件；变化复评、多任务、通知成功/明确失败/不明、互斥、停止、原期限内resume与到期测试通过。F5新降价现场为直接点击，已修复68采集测试并完成新发布/新降价/详情联合验证，只关闭自有target、原页面保留。两worker已ff整合并验收；临时工作树已按生命周期移除，提交从master可恢复。核心c23d787，Cubi实际service/timer、私人购买基准与个人Telegram验收唯一由CUB-059持有；公开工具任务不维护真实商品明细和议价。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
已交付配置驱动的通用闲鱼监控核心：schema2迁移保真、多任务判断隔离、去重与变化复评、有界文本AI、独立outbox及停止恢复/期限/互斥。182离线测试通过；真实新发布/新降价/详情与24页面保留通过，实际AI三次通过，同快照回放零重复事件。Cubi CUB-059承接实际调度和个人通知运行验收，14天效果观察不能作为当前已完成结果。核心代码c23d787。
<!-- SECTION:FINAL_SUMMARY:END -->
