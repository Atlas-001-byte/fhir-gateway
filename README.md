# FHIR Gateway

医疗数据互操作网关：FHIR 资源校验、术语映射与审计留痕。

一次调用依次完成：**资源校验 → 术语映射 → 审计留痕**。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。

- 仅使用 Python 3 标准库，无需安装第三方依赖。
- 不引入任何外部 FHIR/术语库或同类网关实现。

## 用法

```sh
fhir-gateway --audit-file PATH < request.json
```

- `--audit-file PATH`（必需）：审计 JSON Lines 文件；文件不存在则创建，已存在则保留旧行并追加。
- 请求 JSON 从标准输入读取。

## 请求格式

```json
{
  "resource": { "resourceType": "Patient|Observation|Condition", "id": "...", "...": "..." },
  "term_maps": [
    {
      "system": "http://loinc.org",
      "code": "8867-4",
      "target_system": "http://snomed.info/sct",
      "target_code": "8499000"
    }
  ],
  "audit_context": {
    "request_id": "req-1",
    "actor": "dr-house",
    "recorded_at": "2026-10-03T10:00:00Z"
  }
}
```

## 校验规则

- `audit_context.request_id`、`actor`、`recorded_at` 必须为非空字符串。
- `resource.resourceType` 仅限 `Patient`、`Observation`、`Condition`，且 `id` 为非空字符串。
- `Observation.status` 必须符合 FHIR R4 ObservationStatus（`registered`、`preliminary`、`final`、`amended`、`cancelled`、`corrected`、`entered-in-error`、`unknown`）。
- `Observation.code` 与 `Condition.clinicalStatus` 的首个 `coding` 必须含非空 `system`、`code`。
- `term_maps` 每项 `system`、`code`、`target_system`、`target_code` 必须为非空字符串。
- 同一源编码（`system` + `code`）映射到多个不同目标时报错；重复但目标相同允许。

## 成功输出

退出码 `0`，stdout 一行 JSON，字段顺序固定：

```json
{
  "status": "accepted",
  "resource_type": "Observation",
  "resource": { "...": "原样返回，不改写" },
  "mappings": [
    {
      "source": { "system": "http://loinc.org", "code": "8867-4" },
      "status": "mapped",
      "target": { "system": "http://snomed.info/sct", "code": "8499000" }
    },
    {
      "source": { "system": "http://x", "code": "y" },
      "status": "unmapped",
      "target": null
    }
  ],
  "audit": {
    "request_id": "req-1",
    "actor": "dr-house",
    "recorded_at": "2026-10-03T10:00:00Z",
    "decision": "accepted",
    "audit_id": "非空随机ID"
  }
}
```

- 资源内所有含非空 `system`/`code` 的编码对象（含嵌套结构）逐项生成映射结果；命中给目标，未命中 `status=unmapped` 且 `target=null`。
- `resource` 原样返回，绝不改写。
- 审计行（不含 `resource`、`mappings`）随后追加到 `--audit-file`，一行一条 JSON。

## 错误输出

任意受控错误：**stdout 为空**，stderr 一行 JSON，退出码 `2`，且**不写审计文件**：

```json
{"error": {"type": "InputError", "message": "非空错误信息"}}
```

| 错误类型 | 触发场景 |
| --- | --- |
| `InputError` | 标准输入不是合法 JSON、请求结构非法、缺少字段、`audit_context` 非法 |
| `FhirValidationError` | 资源类型/id/status/编码不满足校验规则 |
| `TermMappingError` | 映射项字段非法，或同一源编码存在多个不同目标 |
| `AuditWriteError` | 缺少 `--audit-file`、路径为目录、父目录不存在、无权限或追加写入不完整；此时 stdout 为空 |

> 注：审计在 stdout 输出之前落盘，因此 `AuditWriteError` 发生时 stdout 保证为空。

## 测试

```sh
python3 test_fhir_gateway.py
```

## 状态

已实现 `fhir-gateway`：资源校验、术语映射、审计留痕端到端可用，含 21 个端到端测试。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
