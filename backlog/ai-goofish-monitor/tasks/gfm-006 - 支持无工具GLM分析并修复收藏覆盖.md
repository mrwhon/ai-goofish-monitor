---
id: GFM-006
title: 支持无工具GLM分析并修复收藏覆盖
status: Ready
assignee:
  - codex
created_date: '2026-10-14 12:42'
labels: []
dependencies: []
type: bug
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Cubi CUB-070已授权切换闲鱼两组分析到既有GLM并修复收藏漏读。核心现仅支持chat-completions且收藏只读取初始DOM，缺失详情额度过低造成持续partial。复用宿主Claude GLM launcher，保留其他宿主chat兼容；不联系卖家或买卖，不绕过平台边界。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 GLM CLI单轮无工具解析与模型用量回执可验证，失败保持pending，无隐式API回退
- [ ] #2 收藏加载有时间与滚动上限，覆盖已收藏关注对象；明确未收藏不等同未采集
- [ ] #3 缺失关注详情分配满足有界巡检，不稀释登录风控及已售状态边界
- [ ] #4 核心离线回归与运行合同同步通过
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 调查发现均有处置依据，剩余事项已明确承接
<!-- DOD:END -->
