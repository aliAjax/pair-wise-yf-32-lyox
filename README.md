# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 冷缺血剩余窗口

登记器官时可填写 `explant_at`（离体时刻）和 `max_cold_minutes`（最长耐受分钟，需同时填写）。系统按当前时间计算剩余分钟并分三档：正常、预警（剩余不足一成）、超限（剩余为零）。预警档下接受、发起交接、确认交接必须携带 `dispatcher_confirm`（调度员标识），确认人写入审计。超限后接受、交接、植入等所有继续流转均被拒绝，器官标为 `failed` 并记录触发环节（`cold_failed_stage`）与实际超时分钟（`cold_overtime_minutes`），处置痕迹可通过 `GET /api/donors/{id}/audit` 查询。未登记离体数据的旧记录继续按原有效期办理。

## 运行

```bash
python3 app.py --db organ_allocation.db
```

默认监听 `127.0.0.1:8203`，首页 `/`，健康检查 `/health`。

身份头：`X-User-Id`、`X-Role`。角色为 `viewer`、`hospital`、`coordinator`、`allocation_officer`、`auditor`；医院角色还需 `X-Hospital`。

## 主要接口

- `POST /api/donors`、`POST /api/candidates`：登记器官与候选患者。
- `GET /api/donors/{id}/ranking`：查看兼容候选排序。
- `POST /api/allocations`：提出唯一分配。
- `POST /api/allocations/{id}/accept`、`withdraw`：医院确认或撤回。
- `POST /api/allocations/{id}/transit`、`delay`：冷链转运和延误上报。
- `POST /api/allocations/{id}/handoff`、`handoff-accept`：来源医院发起、接收医院确认。
- `POST /api/allocations/{id}/implant`：确认植入。
- `GET /api/allocations/{id}/audit`、`GET /api/donors/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
