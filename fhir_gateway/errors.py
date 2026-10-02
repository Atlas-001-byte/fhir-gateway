"""fhir-gateway 的错误类型。

所有对外错误都继承 GatewayError，异常名即 stderr 中 error.type 的取值，
异常消息（非空）即 error.message。
"""


class GatewayError(Exception):
    """所有 fhir-gateway 错误的基类。"""


class InputError(GatewayError):
    """输入不是合法 JSON、请求结构非法或 audit_context 非法。"""


class FhirValidationError(GatewayError):
    """FHIR 资源未通过校验。"""


class TermMappingError(GatewayError):
    """术语映射项非法，或同一源编码存在多个映射目标。"""


class AuditWriteError(GatewayError):
    """审计参数缺失、路径不可用、无权限或审计行追加不完整。"""
