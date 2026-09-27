# 器官分配与转运协调系统

Python 标准库独立项目。系统按器官类型、血型、地域、医疗匹配、紧急程度和等待时间排序候选患者，并管理提出、接受、转运、交接、植入或撤回流程。器官过期后所有继续流转操作都会被阻止，全部状态变化写入审计记录。

## 冷缺血剩余窗口

登记器官时可填写 `explant_at`（离体时刻）与 `max_tolerance_minutes`（最长耐受分钟，正整数，两者需同时填写）。系统按当前时间计算剩余窗口并分三档展示（剩余分钟见 `cold_ischemia.remaining_minutes`）：

- `normal` 正常：剩余不少于耐受的一成；
- `warning` 预警：剩余不足一成。此状态下 `accept`、`handoff`、`handoff-accept` 必须在请求体携带 `coordinator_confirmed_by`（调度员标识）确认，确认痕迹写入审计（`coordinator_cold_ischemia_confirmed`）；
- `exceeded` 超限：接受、交接、植入一律拒绝（409 `cold_ischemia_exceeded`），分配置为 `expired`、器官置为 `failed`，审计记录 `organ_cold_ischemia_failed`（含触发环节 `stage` 与实际超时分钟 `overtime_minutes`）。

`/api/state` 与分配详情均返回 `cold_ischemia`，首页展示剩余分钟、三档标识和处置痕迹。未填写离体信息的旧记录继续按原 `expires_at` 有效期办理，不参与冷缺血管控。

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
- `GET /api/allocations/{id}/audit`、`GET /api/state`：完整审计和权限视图。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

血型兼容与评分是演示规则，不包含 HLA 分型、器官大小、病程、儿科差异和真实移植网络规则。医院身份使用请求头模拟，SQLite 环境适合原型，不处理跨机构身份信任、远程患者隐私协议和真实冷链设备接入。
