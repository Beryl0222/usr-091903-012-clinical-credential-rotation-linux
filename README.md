# 医护独立执业轮转 · 轮转履约后端

区域医联体骨干医师、注册护士到县级机构轮转的履约服务：出发前锁定派出协议，
轮转期间所有职责变更、能力授予、交接接续与结业证明都有事实依据、可审计、可还原。

仅依赖 Python 标准库，无第三方依赖。

## 运行

```bash
python3 service.py --check          # 核对服务配置
python3 service.py --port 8000      # 启动服务，GET /health 返回服务身份
npm test                            # 运行全部契约测试（18 项）
```

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `rotation_service.py` | 领域服务：协议版本、互斥校验、授予台账、交接清单、时点还原、证书 |
| `rotation_api.py` | 标准库 HTTP 路由分发（`X-Actor` 身份、`Idempotency-Key` 幂等） |
| `service.py` | 运行入口，保留稳定的 `/health` 与 `Handler` |
| `test_rotation_contract.py` | 覆盖全部业务规则的 HTTP 契约测试 |

## 业务规则与实现要点

1. **出发前锁定派出协议** `POST /plans`：执业地点、专业权限（独立/监督）、
   学习目标、带教人、有效时段一并固化为 v1 快照；注册范围不符、执业地点未备案、
   证照在窗口结束前到期都会在出发前被拒绝（`credential_mismatch`，逐项给出原因）。
2. **变更须双方批准** `POST /amendments`、`POST /amendments/{id}/approvals`：
   临时支援（support）与延长轮转（extension）提出后职责不变，且批准方必须属于
   对应机构；第二方批准时才生成新版本快照，按版本时段生效，历史版本永久保留。
3. **两院互斥拒绝** `POST /tasks`：同一人员重叠时段在另一机构承担互斥任务返回
   `409 mutual_exclusion` 并指明冲突任务；同院并行、时段不重叠、显式非互斥任务允许，
   取消任务后释放占用。
4. **授予只发生一次** `POST /receipts`、`POST /assessments`：监管回执按
   `人员+回执编号`、考核按考核 ID 自然键去重，并支持 `Idempotency-Key`
   （同键不同内容返回 `409`）；重试回放首次结果，授予审计只出现一次。
5. **交接逐项确认** `POST /plans/{id}/handovers`：带教人休假、证照到期、紧急返院
   触发清单，未签署病历、随访责任、（休假/返院时的）监督关系各成一项；
   每一项只能由新责任人（或管理者）逐项 `confirm`，无整体改名接口，
   监督人只在监督项确认后才切换。
6. **病例摘要最小可见** `GET /cases/{id}`：轮转人员只能查看本人负责的摘要，
   越权返回 `403 case_access_denied` 并留痕，管理者可查看全部。
7. **任一时点还原** `GET /plans/{id}/effective?at=...`：返回当时有效的协议版本、
   谁可独立执行/须监督（含阻断原因：证照失效、监管回执未到、考核未签或已撤销）、
   谁承担监督、交接是否完成；管理者另有 `GET /audit` 完整操作链。
8. **结业证明有据可查** `POST /plans/{id}/certificates`：只以"已签署 + 通过 +
   未撤销"的考核事实为依据；撤销考核会挂回对应授予并使已发证明在
   `GET /certificates/{id}/verify` 立即变为 `invalidated`。

## 请求约定

- 身份：请求头 `X-Actor: <人员或管理者 ID>`；管理者通过 `POST /actors`
  注册并绑定机构，临床人员通过 `POST /staff` 注册。
- 幂等：写操作可带 `Idempotency-Key`，相同键 + 相同请求体重放首次结果。
- 回放：写操作请求体或 GET 查询串支持 `at`（ISO-8601），用于确定性时点测试；
  证照到期日使用 `YYYY-MM-DD`。
- 错误：统一返回 `{error, message, details}`，状态码 400/403/404/409/405。
