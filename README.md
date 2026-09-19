# 医护独立执业轮转（clinical-credential-rotation）

区域医联体骨干医师、注册护士到县级机构轮转的**履约后端**。在出发前锁定派出协议与权责边界，
轮转期间只承认经双方批准的变更，轮转结束时保证未签署病历、随访责任逐项有人接续，
结业证明只依据已签署且未撤销的考核事实生成。

## 运行与测试

```bash
python3 service.py --check          # 基础配置自检
python3 service.py --port 8000      # 启动服务，GET /health 确认身份
npm test                            # 运行全部测试（基础契约 + 领域 + HTTP 契约，58 项）
```

设置环境变量 `ROTATION_STORE_PATH=/path/to/events.jsonl` 可把事件持久化到 JSONL，
重启后完整重放，事件序号连续。

## 核心设计：只增不改的事件溯源

系统没有可变的"当前状态表"。所有事实（`rotation/store.py`）都以带全局序号 `seq`
和提交人 `actor` 的事件追加存储，权责状态由事件归约得到（`rotation/domain.py: World`）。
因此：

- `GET /trace?at_seq=N` 可**还原第 N 个操作发生时**的完整权责：谁能独立执行、
  谁承担监督、交接确认到第几项；
- 监管回执重传、考核上传重试只回放原事件，**不可能产生第二次授予**；
- 撤销考核、作废旧证明也是追加事实，时间线上每一点都可审计。

### 能力阶梯

同一执业地点、同一日期，权限分三档：

| 档位 | 条件 |
|---|---|
| 无能力 | 不在经批准的有效窗口内（含窗口已因紧急返院截断），或证照失效/到期/被撤销 |
| 可监督执行 `supervised` | 有效窗口 ∩ 协议锁定的专业范围 ∩ 证照有效 |
| 可独立执行 `independent` | 以上 ∩ 监管回执已登记 ∩ 已签署且**未撤销**的考核 |

## 业务约束的落实

1. **出发前锁定**：`POST /agreements` 一次性锁定派出协议编号、执业地点、专业权限
   （至少一项）、学习目标、带教人、有效时段、双方批准人；重复锁定直接冲突。
2. **临时支援/延长须双方批准**：修正案先由一方提出（pending），另一方批准后才
   `applied` 并新增窗口；单方批准不变更任何职责，支援地点在批准前不能派任务。
   生效前复检两院互斥，防止批准间隙产生冲突。
3. **两院互斥任务必须拒绝**：跨院轮转窗口重叠在锁定协议时即拒绝；互斥职责任务
   （`kind=exclusive`，默认）时间重叠在派发与本人接受两个时点都复检并拒绝；
   咨询类（`kind=advisory`）可并存。
4. **回执/考核幂等**：回执编号、考核编号是业务幂等键。重传返回 `deduplicated=true`
   且不产生事件；编号改用于其他权限报冲突。考核被撤销后重传旧编号不会恢复授权
   （`granted=false`）。
5. **交接逐项确认，不整体改名**：带教人休假、证照到期、紧急返院三类事件触发
   交接清单，事项由系统从未结事实生成（未签病历、未完成随访、在执行任务、
   监督责任改派）。每个事项必须单独指定新责任人并由接收管理方逐项确认；
   禁止重复确认，监督事项不能改回原带教人，全部事项确认后清单才 completed。
6. **紧急返院**：派出方发起，所有跨越返院日的窗口当日截断并结束，此后不能再派
   任务或由原轮转人员签病历，异常结束的轮转不能发结业证明。
7. **病例最小可见**：轮转人员 `GET /staff/{id}/cases` 只能看到自己负责（或随访
   转交给自己）的病例摘要指引，不含病历全文；非责任人不能签署。
8. **考核撤销联动证明**：撤销最后一个有效考核时，已签发的结业证明自动作废；
   无已签署未撤销考核事实时不能签发。
9. **结业证明的前置条件**：所有窗口已结束、无未签病历、无未完成随访、无未完成
   交接、至少一项未撤销考核、轮转非异常结束；签发本身也幂等。

## HTTP 接口

所有写操作需要请求头 `X-Actor-Id: <操作人ID>`；主体（医务处/护理部管理员、
带教人、轮转人员）的区分由领域规则强制。错误统一为
`{"error": {"code", "message"}}`，状态码：400 校验失败、403 无权、404 不存在、
409 冲突、500 内部错误。

| 方法与路径 | 说明 |
|---|---|
| `POST /staff` / `POST /staff/credentials` | 注册人员 / 登记证照（含到期日） |
| `POST /agreements` | 出发前锁定轮转协议 |
| `GET /agreements/{id}` | 查看协议（窗口、修正案、权责） |
| `POST /agreements/{id}/amendments` | 提出临时支援/延长/范围修正（一方） |
| `POST /agreements/{id}/amendments/{aid}/approvals` | 另一方批准；双方齐则生效 |
| `POST /assignments` | 派发职责任务（互斥校验） |
| `POST /assignments/{id}/response` | 本人接受/拒绝（接受时复检互斥） |
| `POST /assignments/{id}/complete` | 完结任务 |
| `POST /receipts` | 监管回执登记（幂等） |
| `POST /assessments` | 带教人登记考核（幂等，校验证照/窗口/带教状态） |
| `POST /assessments/{id}/revoke` | 管理方撤销考核（联动作废证明） |
| `POST /agreements/{id}/mentor-leave` | 登记带教人休假并触发交接 |
| `POST /agreements/{id}/credential-handover` | 证照到期触发交接 |
| `POST /agreements/{id}/emergency-return` | 紧急返院，截断窗口并交接 |
| `GET /handovers/{id}` | 交接清单及逐项状态 |
| `POST /handovers/{id}/items/{item}/confirm` | 逐项确认，指定新责任人 |
| `POST /cases` | 向轮转人员分派病例 |
| `POST /cases/{id}/sign` | 责任人签署（记录是否独立执行） |
| `POST /cases/{id}/followup` / `.../followup-complete` | 登记/完成随访 |
| `GET /staff/{id}/cases` | 本人病例摘要（最小可见） |
| `GET /staff/{id}/capability?on_date=` | 指定日期的有效能力分档 |
| `POST /agreements/{id}/certificate` | 签发结业证明（幂等） |
| `GET /trace?at_seq=N[&on_date=]` | 还原任一操作时的权责与交接 |
| `GET /events?after_seq=N` | 原始事件流（审计/巡检） |

## 代码结构

```
service.py                 运行入口（保留原有健康检查契约）
rotation/store.py          线程安全的只增事件存储（内存或 JSONL）
rotation/domain.py         全部业务规则：World 归约 + RotationService 命令/查询
rotation/api.py            HTTP JSON 接入层、主体识别、错误码映射
rotation/test_domain.py    领域端到端测试（55 项）
rotation/test_api.py       HTTP 契约测试
service_contract.py        基础服务身份契约（原有 3 项）
```
