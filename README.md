# FHIR Gateway

医疗数据互操作网关：FHIR 资源校验、术语映射与审计留痕。

## 范围

本仓库从零开始实现上述方向的可用工具，不依赖外部同类实现。仅使用 Python 3
标准库，无第三方依赖。

## 用法

从标准输入读取一个 JSON 请求，通过 `--audit-file` 指定审计 JSON Lines 文件：

```sh
fhir-gateway --audit-file path/to/audit.jsonl < request.json
```

`--audit-file` 支持 `--audit-file=path` 形式。审计文件不存在时创建，
已有时以追加方式保留原有各行。

### 请求结构

```json
{
  "resource": { "resourceType": "Patient", "id": "pat-1" },
  "term_maps": [
    {
      "system": "http://loinc.org",
      "code": "29463-7",
      "target_system": "http://snomed.info/sct",
      "target_code": "27113001"
    }
  ],
  "audit_context": {
    "request_id": "req-1",
    "actor": "dr-who",
    "recorded_at": "2026-10-03T10:00:00Z"
  }
}
```

### 成功输出（stdout，一行 JSON）

字段顺序固定为 `status`、`resource_type`、`resource`、`mappings`、`audit`：

- `status`：`"accepted"`
- `resource_type`：资源类型（`Patient` / `Observation` / `Condition`）
- `resource`：输入资源原样返回，**不做改写**
- `mappings`：资源内待映射编码（`Observation.code.coding[*]`、
  `Condition.clinicalStatus.coding[*]`）逐项给出：
  - `source`：`{"system", "code"}`
  - `status`：命中为 `"mapped"`，未命中为 `"unmapped"`
  - `target`：命中为 `{"system", "code"}`，未命中为 `null`
- `audit`：保留 `request_id`、`actor`、`recorded_at`，补充
  `decision: "accepted"` 与非空 `audit_id`

成功后还会把**不含** `resource`、`mappings` 的审计对象追加为审计文件的一行。

### 校验规则

- `audit_context.request_id`、`actor`、`recorded_at` 必须为非空字符串。
- `resource.resourceType` 仅限 `Patient`、`Observation`、`Condition`，且 `id`
  为非空字符串。
- `Observation.status` 必须是 FHIR R4 合法状态（`registered`、`preliminary`、
  `final`、`amended`、`corrected`、`cancelled`、`entered-in-error`、`unknown`）。
- `Observation.code` 与 `Condition.clinicalStatus` 为非空 CodeableConcept，
  其每个 coding 的 `system`、`code` 均为非空字符串。
- `term_maps` 每项的 `system`、`code`、`target_system`、`target_code` 均为
  非空字符串；同一源编码 `(system, code)` 映射到多个不同目标视为错误
  （重复同一目标合法）。

### 错误处理

任何失败都：**不写审计文件**、stdout 为空、stderr 输出一行 JSON、退出码 2。
stderr 形状：

```json
{"error": {"type": "异常名", "message": "非空错误消息"}}
```

错误类型：

| 类型 | 触发场景 |
| --- | --- |
| `InputError` | 输入不是合法 JSON、请求结构/字段非法、`audit_context` 非法 |
| `FhirValidationError` | FHIR 资源校验失败 |
| `TermMappingError` | 映射项字段非法，或同码多目标 |
| `AuditWriteError` | `--audit-file` 缺失/无值、路径为目录、无权限、追加不完整 |

审计追加先于 stdout 执行，因此 `AuditWriteError` 时 stdout 保证为空。

## 项目结构

```
fhir_gateway/
  errors.py   # 四类异常
  core.py     # 请求解析、资源校验、术语映射
  audit.py    # 审计 JSON Lines 追加（含部分写入处理）
  cli.py      # 命令行处理与 I/O 契约
bin/fhir-gateway
tests/test_fhir_gateway.py
```

## 测试

```sh
python3 -m unittest discover -s tests -v
```

## 状态

已实现：FHIR 资源校验、术语映射、审计留痕及对应命令行工具，测试覆盖成功与
各类错误路径。

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
