---
id: GFM-005
title: 区分局部采集 partial 与整轮监控失败
status: Done
assignee:
  - '@codex'
created_date: '2026-10-13 02:57'
updated_date: '2026-10-13 03:14'
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
- [x] #1 单轮partial保留覆盖缺口且不触发整项红色失败，持续partial仍由健康检查升级
- [x] #2 failed、blocked、发送uncertain或retry仍返回失败；回归测试通过
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Direct：保留partial与覆盖缺证原件，不改变successful live心跳含义；CLI单轮partial不立即OnFailure，现有36小时success哨兵继续升级持续partial；failed/blocked/degraded及发送未知仍失败。补正反CLI测试，运行独立tests/cubi，提交后核对Cubi现役路径。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
265项独立tests/cubi通过（退出0，11.57秒）；CLI实际run/scan分级、持续partial不刷新成功心跳、平台failed/blocked及nested/flat发送uncertain/retry均覆盖。git diff --check通过。Cubi现役wrapper直接引用本canonical源码，下一次原timer执行即采用；未为验收访问闲鱼、AI或发送TG。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
定时run的单轮partial不再触发整项失败，仍保留缺口与36小时完整成功哨兵。显式scan及真实失败/阻断/投递异常继续非零。265项回归通过。
<!-- SECTION:FINAL_SUMMARY:END -->
