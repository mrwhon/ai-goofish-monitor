---
id: GFM-006
title: 支持无工具GLM分析并修复收藏覆盖
status: Done
assignee:
  - codex
created_date: '2026-10-14 12:42'
updated_date: '2026-10-14 13:26'
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
- [x] #1 GLM CLI单轮无工具解析与模型用量回执可验证，失败保持pending，无隐式API回退
- [x] #2 收藏加载有时间与滚动上限，覆盖已收藏关注对象；明确未收藏不等同未采集
- [x] #3 缺失关注详情分配满足有界巡检，不稀释登录风控及已售状态边界
- [x] #4 核心离线回归与运行合同同步通过
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 调查发现均有处置依据，剩余事项已明确承接
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
当前owner直接实现，不委派。新增薄Claude CLI transport复用GLM launcher，隔离cwd/env、无tools/MCP/会话持久化、单轮与限时；模型和usage验证且无provider回退。收藏优先等待卡片并有界自然滚动直至关注对象齐或底部稳定，保留未找到对象为详情补查。关注缺口先分配现有详情额度，仅实际需要收藏时使用剩余额度，不为未发生收藏预留固定额度。为加载、缺失、风控及transport正常/失败补离线回归，保留chat兼容与数据历史；运行文档同批更新。
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
发现与处置：F1初始收藏DOM漏读延迟与滚动加载；F2缺失关注补查额度仅1使单次无法恢复完整；F3仅有chat API且actual model回执需GLM CLI支持。属于CUB-070直接依赖，当前owner可直接改此干净canonical仓，无独立owner工单。

发现与处置结案：F1离线延迟挂载、滚动加载、稳定底部、空DOM及风控停止回归通过；F2五个缺失关注/五详情/三AI/启用收藏的合成回归证明全部补齐且复用缓存仍可AI，无多余固定预留；F3Claude统计使用客户端alias，采用stream-json assistant model核provider，真实合成调用证明glm-5.3-flash/claude-sonnet-5分别绑定，单轮289输出token并通过Analyzer JSON边界。核心回归281通过、新增额度回归1通过，diff检查通过。无schema迁移，旧API兼容和历史保留。宿主真实闲鱼巡检因前轮visible_platform_boundary等待用户同账号手动恢复，由CUB-070持有，不是核心离线失败。

现场新证据使F1需要补齐：用户确认继续Cubi且账号核对一致。首次修复真实favorites在10步底部判断稳定只找到1/7关注；时序探针确认第8步第二批由20增至33。底部连续三次计数不足以排除异步加载尚未完成，补末端networkidle与settlement等待并重新验。其它GLM与额度验收仍有效。

真实live run20仅余缺失关注的详情CaptureError；只读CLI现场显示精确平台通知“糟糕！宝贝被删掉了”。新增同URL/id且唯一可见删除通知的详情结果unavailable，登录/风控仍先验证；终态无当前价格也可完成关注确认，保留历史价格/报价，不把已删除商品当采集故障。为URL身份、正常返回、networkidle及无价格终态补回归。Mac live run21已ok且actual GLM回执验证通过。

F1/F2实际结案：287核心离线回归全通过（14.51秒）、diff检查通过。补底部networkidle/渲染settlement后现场读取6/7关注；唯一缺失旧商品平台明确删除通知，加入URL/id+唯一可见提示确认终态并保留历史价。正式Mac live run21=ok/3详情2AI；PC最终live run22=ok/3详情1AI，两缺失关注均经详情解决，无unconfirmed_status；actual GLM回执glm-5.3-flash，client claude-sonnet-5，单轮869输出token。Cubi独立health=v1 ok/null，期限原值不变。没有新增收藏、没有发送新通知、没有交易。后续宿主收尾由CUB-070完成。
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
GLM CLI单轮无工具分析已真实验证；修复收藏首批/滚动/末端异步加载和详情额度，精确页面删除通知确认终态并保留历史价格。287核心回归通过，Mac run21和PC run22均ok，实际glm-5.3-flash回执及健康哨兵ok。
<!-- SECTION:FINAL_SUMMARY:END -->
