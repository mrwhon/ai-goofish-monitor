---
id: GFM-005
title: 区分局部采集 partial 与整轮监控失败
status: Ready
assignee:
  - '@codex'
created_date: '2026-10-13 02:57'
labels: []
dependencies: []
type: bug
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
CUB-063 用户已GO授权：2026-10-13 pc-workstation完成搜索分析但一个关注商品availability未知使partial退出1，触发整项OnFailure。GFM-004已有解析修复；本次仅分级及持续partial可见性，不放宽账号/收藏/发送边界。
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 单轮partial保留覆盖缺口且不触发整项红色失败，持续partial仍由健康检查升级
- [ ] #2 failed、blocked、发送uncertain或retry仍返回失败；回归测试通过
<!-- AC:END -->
