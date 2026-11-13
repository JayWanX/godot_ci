#!/usr/bin/env python3
"""零依赖 JSON Schema 子集校验器（runner 只保证 Python 标准库）。

支持关键词：type / properties / required / additionalProperties / items /
enum / const / default / pattern / minLength / minItems / minimum / maximum / $ref。
$ref 只解析文档内 "#/definitions/..." 指针；不支持 oneOf / anyOf / allOf
—— 本工作区的契约刻意不用这些构造。

- ``validate``      逐字段校验，收集全部错误（不是遇错即停），返回错误串列表。
- ``apply_defaults`` 按 schema 的 ``default`` 递归补齐缺省字段（先补再校验）。
- ``load_schema``   读 JSONC schema 文件（支持 // 与 /* */ 注释及尾逗号）。

``default`` 只作注解，不属于校验约束；语义由 ``apply_defaults`` 实现。
"""
import copy
import json
import re
from pathlib import Path

from jsonc import fail, read_json, strip_jsonc


_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _type_ok(value, expected):
    """类型判定；schema 写错类型名（不在 _TYPE_CHECKS 内）直接抛，不静默放行。

    静默跳过未知 type 是最危险的退化：`"type": "int"` 这类笔误会让该校验消失，
    而校验通过的假象与真通过无法区分。
    """
    types = expected if isinstance(expected, list) else [expected]
    unknown = [t for t in types if t not in _TYPE_CHECKS]
    if unknown:
        raise ValueError(f"schema 使用了不支持的 type：{unknown}")
    return any(_TYPE_CHECKS[t](value) for t in types)


def _type_name(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _same_value(instance, expected):
    """JSON 语义相等：`True` 与 `1` 不相等（Python 的 == 会把它们判等）。

    schema 的 `enum: [1]` 不该放行 `true`——两者在 JSON 里是不同类型，配置写错
    类型时静默通过会让下游按整数取值的代码拿到布尔。
    """
    if isinstance(instance, bool) != isinstance(expected, bool):
        return False
    if isinstance(instance, (int, float)) and isinstance(expected, (int, float)):
        return instance == expected
    return instance == expected


def validate(instance, schema, path="$"):
    """校验 instance，返回全部错误描述（列表为空表示通过）。"""
    errors = []

    expected = schema.get("type")
    if expected is not None and not _type_ok(instance, expected):
        want = "/".join(expected) if isinstance(expected, list) else expected
        errors.append(f"{path}: 类型应为 {want}，实际 {_type_name(instance)}")
        return errors  # 类型不符时不再下钻，避免连锁误报

    if "const" in schema and not _same_value(instance, schema["const"]):
        errors.append(f"{path}: 必须等于 {schema['const']!r}")
    if "enum" in schema and not any(_same_value(instance, v) for v in schema["enum"]):
        errors.append(f"{path}: 取值 {instance!r} 不在枚举 {schema['enum']!r}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必填字段 '{key}'")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for key, value in instance.items():
            sub = f"{path}.{key}"
            if key in props:
                errors.extend(validate(value, props[key], sub))
            elif addl is False:
                errors.append(f"{path}: 未知字段 '{key}'（additionalProperties=false）")
            elif isinstance(addl, dict):
                errors.extend(validate(value, addl, sub))

    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, value in enumerate(instance):
                errors.extend(validate(value, items, f"{path}[{i}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: 至少 {schema['minItems']} 项，实际 {len(instance)}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: 长度 {len(instance)} < minLength {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: '{instance}' 不匹配 /{schema['pattern']}/")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")

    return errors


def apply_defaults(instance, schema):
    """按 schema 的 default 递归补齐缺省字段，原地修改并返回 instance。"""
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key not in instance and "default" in sub:
                instance[key] = copy.deepcopy(sub["default"])
        for key, sub in props.items():
            if key in instance:
                apply_defaults(instance[key], sub)
    elif isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for value in instance:
                apply_defaults(value, items)
    return instance


def expand_refs(node, root, _seen=None):
    """就地展开文档内 "#/definitions/..." 引用；自引用/环引用直接报错。"""
    _seen = _seen or set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in _seen:
                raise ValueError(f"schema $ref 环引用：{ref}")
            target = root
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
            return expand_refs(merged, root, _seen | {ref})
        return {k: expand_refs(v, root, _seen) for k, v in node.items()}
    if isinstance(node, list):
        return [expand_refs(v, root, _seen) for v in node]
    return node


def load_schema(path):
    """读 JSONC schema 文件；展开文档内 $ref；不存在或非法 JSON 直接抛。"""
    schema = json.loads(strip_jsonc(Path(path).read_text(encoding="utf-8")))
    return expand_refs(schema, schema)


def validate_or_fail(instance, schema, label):
    """校验并在失败时用 ::error:: 逐条打印后 sys.exit(1)。"""
    errors = validate(instance, schema)
    if errors:
        for err in errors:
            fail(f"{label} {err}")
    return instance


def read_json_validated(file_path, description, schema, label=None):
    """读 JSONC 配置 → 补 schema default → 硬校验（非法/缺必填/类型不符 fail-fast）。

    放在本模块而不是 jsonc：JSONC 负责「怎么读」，本模块负责「读出来合不合法」。
    反过来放会让 jsonc 依赖 schema，而 schema 已经依赖 jsonc（strip_jsonc），
    形成循环导入——只能靠函数内局部导入绕开，那正是要避免的形态。
    """
    data = read_json(file_path, description)
    apply_defaults(data, schema)
    return validate_or_fail(data, schema, label or description)
