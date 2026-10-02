"""请求解析、FHIR 资源校验与术语映射。

本模块只做纯数据处理，不涉及 stdin/stdout 与审计文件 I/O，便于测试。
"""

import json

from .errors import FhirValidationError, InputError, TermMappingError

# 支持的资源类型。
ALLOWED_RESOURCE_TYPES = frozenset({"Patient", "Observation", "Condition"})

# FHIR R4 Observation.status 允许的取值。
OBSERVATION_STATUSES = frozenset(
    {
        "registered",
        "preliminary",
        "final",
        "amended",
        "corrected",
        "cancelled",
        "entered-in-error",
        "unknown",
    }
)

# 需要参与术语映射的编码在资源中的路径：
# 路径最后一个元素是包含 system/code 的对象（CodeableConcept.coding 或直接 CodeableReference 形式）。
# 每个三元组为 (资源类型, 编码对象路径, 目标列表键)：
#  - Observation.code.coding[*]
#  - Condition.clinicalStatus.coding[*]


def _is_nonempty_string(value):
    return isinstance(value, str) and value != ""


def parse_request(raw):
    """解析并校验请求的整体结构，返回 (resource, term_maps, audit_context)。

    JSON 非法、顶层结构/字段类型非法、audit_context 非法均抛 InputError。
    注意：FHIR 资源与术语映射项的业务校验不在此处，分别由
    validate_resource / build_term_map 负责。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InputError("request body is not valid JSON: %s" % exc)
    else:
        payload = raw

    if not isinstance(payload, dict):
        raise InputError("request must be a JSON object")

    for key in ("resource", "term_maps", "audit_context"):
        if key not in payload:
            raise InputError("request is missing required field %r" % key)

    resource = payload["resource"]
    term_maps = payload["term_maps"]
    audit_context = payload["audit_context"]

    if not isinstance(resource, dict):
        raise InputError("field 'resource' must be an object")
    if not isinstance(term_maps, list):
        raise InputError("field 'term_maps' must be an array")
    audit = validate_audit_context(audit_context)

    return resource, term_maps, audit


def validate_audit_context(audit_context):
    """校验 audit_context，返回可直接用于审计留痕的 dict。"""
    if not isinstance(audit_context, dict):
        raise InputError("field 'audit_context' must be an object")
    for key in ("request_id", "actor", "recorded_at"):
        if key not in audit_context:
            raise InputError("audit_context is missing required field %r" % key)
        if not _is_nonempty_string(audit_context[key]):
            raise InputError(
                "audit_context field %r must be a non-empty string" % key
            )
    return {
        "request_id": audit_context["request_id"],
        "actor": audit_context["actor"],
        "recorded_at": audit_context["recorded_at"],
    }


def _require_coding(obj, descriptor):
    """校验一个含 system/code 的编码对象，两个字段都必须是非空字符串。"""
    if not isinstance(obj, dict):
        raise FhirValidationError("%s must be an object" % descriptor)
    for key in ("system", "code"):
        if key not in obj:
            raise FhirValidationError("%s is missing field %r" % (descriptor, key))
        if not _is_nonempty_string(obj[key]):
            raise FhirValidationError(
                "%s field %r must be a non-empty string" % (descriptor, key)
            )


def validate_resource(resource):
    """校验 FHIR 资源，非法时抛 FhirValidationError。

    校验范围（按需求约定，非完整 FHIR R4 规范）：
      - resourceType ∈ {Patient, Observation, Condition}
      - id 为非空字符串
      - Observation.status 为 FHIR R4 合法状态
      - Observation.code / Condition.clinicalStatus 的 coding 中每个编码
        的 system、code 均为非空字符串（CodeableConcept 至少含一个 coding）
    """
    resource_type = resource.get("resourceType")
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        if resource_type is None:
            raise FhirValidationError("resource is missing field 'resourceType'")
        raise FhirValidationError(
            "unsupported resourceType %r; allowed: Patient, Observation, Condition"
            % resource_type
        )

    if not _is_nonempty_string(resource.get("id")):
        raise FhirValidationError("resource field 'id' must be a non-empty string")

    if resource_type == "Observation":
        status = resource.get("status")
        if status not in OBSERVATION_STATUSES:
            raise FhirValidationError(
                "Observation.status %r is not a valid FHIR R4 observation status"
                % status
            )
        _require_codeable_concept(resource.get("code"), "Observation.code")

    if resource_type == "Condition":
        _require_codeable_concept(
            resource.get("clinicalStatus"), "Condition.clinicalStatus"
        )

    return resource_type


def _require_codeable_concept(concept, descriptor):
    """校验 CodeableConcept：必须是对象，coding 为非空列表，每项含非空 system/code。"""
    if not isinstance(concept, dict):
        raise FhirValidationError("%s must be an object" % descriptor)
    coding = concept.get("coding")
    if not isinstance(coding, list) or not coding:
        raise FhirValidationError(
            "%s.coding must be a non-empty array" % descriptor
        )
    for index, item in enumerate(coding):
        _require_coding(item, "%s.coding[%d]" % (descriptor, index))


def build_term_map(term_maps):
    """从 term_maps 数组构建源编码 -> 目标编码的映射表。

    每个映射项的 system、code、target_system、target_code 必须是非空字符串，
    否则抛 TermMappingError；同一 (system, code) 出现两个不同目标同样报错。
    """
    table = {}
    for index, item in enumerate(term_maps):
        where = "term_maps[%d]" % index
        if not isinstance(item, dict):
            raise TermMappingError("%s must be an object" % where)
        values = {}
        for key in ("system", "code", "target_system", "target_code"):
            if key not in item:
                raise TermMappingError("%s is missing field %r" % (where, key))
            if not _is_nonempty_string(item[key]):
                raise TermMappingError(
                    "%s field %r must be a non-empty string" % (where, key)
                )
            values[key] = item[key]

        source = (values["system"], values["code"])
        target = (values["target_system"], values["target_code"])
        existing = table.get(source)
        if existing is not None and existing != target:
            raise TermMappingError(
                "source code (%s, %s) maps to multiple targets: (%s, %s) and (%s, %s)"
                % (
                    source[0],
                    source[1],
                    existing[0],
                    existing[1],
                    target[0],
                    target[1],
                )
            )
        table[source] = target
    return table


def iter_resource_codings(resource, resource_type):
    """按资源类型产出参与映射的编码对象（按固定顺序）。

    产出的是资源内原始 coding 对象的引用，但调用方不得改写它们。
    """
    if resource_type == "Observation":
        concept = resource.get("code")
        if isinstance(concept, dict) and isinstance(concept.get("coding"), list):
            for coding in concept["coding"]:
                yield coding
    elif resource_type == "Condition":
        concept = resource.get("clinicalStatus")
        if isinstance(concept, dict) and isinstance(concept.get("coding"), list):
            for coding in concept["coding"]:
                yield coding


def apply_mappings(resource, resource_type, table):
    """生成映射结果列表，不改写 resource。

    每项结构（字段顺序固定）：
      {"source": {"system", "code"}, "status": "mapped"/"unmapped",
       "target": {"system", "code"} / null}
    """
    results = []
    for coding in iter_resource_codings(resource, resource_type):
        # 能进入此处的编码均已通过 validate_resource，system/code 必为非空字符串。
        source = {"system": coding["system"], "code": coding["code"]}
        target_pair = table.get((source["system"], source["code"]))
        if target_pair is None:
            results.append(
                {"source": source, "status": "unmapped", "target": None}
            )
        else:
            results.append(
                {
                    "source": source,
                    "status": "mapped",
                    "target": {
                        "system": target_pair[0],
                        "code": target_pair[1],
                    },
                }
            )
    return results
