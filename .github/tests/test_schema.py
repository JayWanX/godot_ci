#!/usr/bin/env python3
"""schema.py 自测：零依赖 JSON Schema 子集的判据边界。

覆盖：
- type 判定：integer 不含 bool、null 单列、多类型并集
- 未知 type 直接报错（不静默放行：笔误会让整条校验消失）
- const / enum 按 JSON 语义比较（`true` 不等于 `1`）
- 错误一次收齐（不是遇错即停）
- apply_defaults 深拷贝（改实例不污染 schema）
- $ref 展开：~0/~1 转义、环引用报错
- load_schema 读 JSONC（含注释与尾逗号）

用法：python .github/tests/test_schema.py
"""
import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from schema import (apply_defaults, expand_refs, load_schema,  # noqa: E402
                    validate)

CASES = []


def check(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@check("type 判定：integer 不含 bool，number 不含 bool")
def t_type_strictness():
    assert validate(7, {"type": "integer"}) == []
    assert validate(True, {"type": "integer"}), "bool 被当成 integer"
    assert validate(True, {"type": "number"}), "bool 被当成 number"
    assert validate(None, {"type": "null"}) == []
    assert validate("", {"type": "null"}), "空串被当成 null"
    # 多类型并集：'string' 或 'null' 都合法
    union = {"type": ["string", "null"]}
    assert validate("x", union) == [] and validate(None, union) == []
    assert validate(1, union), "整数被并集放行"


@check("未知 type 直接报错（不静默跳过整条校验）")
def t_unknown_type_fails():
    for bogus in ("int", "map", "strings"):
        try:
            validate("x", {"type": bogus})
        except ValueError as e:
            assert "不支持的 type" in str(e), e
        else:
            raise AssertionError(f"未知 type '{bogus}' 被静默放行")
    # 并集里混入未知 type 同样报错（否则该分支的校验消失）
    try:
        validate("x", {"type": ["string", "bogus"]})
    except ValueError:
        pass
    else:
        raise AssertionError("并集里的未知 type 被静默跳过")


@check("const / enum 按 JSON 语义比较：true 不等于 1")
def t_bool_int_distinct():
    # const
    assert validate(1, {"const": 1}) == []
    assert validate(True, {"const": 1}), "True 通过了 const: 1"
    assert validate(1, {"const": True}), "1 通过了 const: true"
    # enum
    assert validate(1, {"enum": [1, 2]}) == []
    assert validate(True, {"enum": [1, 2]}), "True 通过了 enum: [1, 2]"
    assert validate(False, {"enum": [0]}), "False 通过了 enum: [0]"
    assert validate(0, {"enum": [False]}), "0 通过了 enum: [false]"
    # 同类型值照常相等
    assert validate("on", {"enum": ["on", "off"]}) == []


@check("错误一次收齐（不是遇错即停）")
def t_collects_all_errors():
    schema = {
        "type": "object",
        "required": ["a", "b"],
        "additionalProperties": False,
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer", "minimum": 5},
        },
    }
    errors = validate({"b": 1, "extra": 2}, schema)
    # 缺 a + b 过小 + 未知字段 extra：三条都要报
    assert len(errors) == 3, errors
    joined = " ".join(errors)
    assert "'a'" in joined and "minimum" in joined and "extra" in joined, errors


@check("apply_defaults 深拷贝：改实例不污染 schema")
def t_defaults_deepcopy():
    schema = {"type": "object", "properties": {
        "list": {"type": "array", "default": []},
        "obj": {"type": "object", "default": {}, "properties": {
            "k": {"type": "string", "default": "v"}}},
    }}
    original = copy.deepcopy(schema)
    inst = apply_defaults({}, schema)
    assert inst == {"list": [], "obj": {"k": "v"}}, inst
    inst["list"].append("mutated")
    inst["obj"]["k"] = "changed"
    assert schema == original, "apply_defaults 把默认值写回了 schema（共享引用）"
    # 再补一次仍拿到干净的默认值
    assert apply_defaults({}, schema) == {"list": [], "obj": {"k": "v"}}


@check("$ref 展开：~1 / ~0 转义与环引用")
def t_refs():
    root = {
        "definitions": {
            "a/b": {"type": "string"},
            "til~de": {"type": "integer"},
            "loop": {"$ref": "#/definitions/loop"},
        },
    }
    got = expand_refs({"$ref": "#/definitions/a~1b"}, root)
    assert got == {"type": "string"}, got
    got = expand_refs({"$ref": "#/definitions/til~0de"}, root)
    assert got == {"type": "integer"}, got
    # $ref 同级其它键覆盖目标（JSON Schema draft-07 语义）
    got = expand_refs({"$ref": "#/definitions/a~1b", "minLength": 2}, root)
    assert got == {"type": "string", "minLength": 2}, got
    try:
        expand_refs({"$ref": "#/definitions/loop"}, root)
    except ValueError as e:
        assert "环引用" in str(e), e
    else:
        raise AssertionError("自引用 $ref 未报错")


@check("load_schema 读 JSONC：注释与尾逗号")
def t_load_jsonc():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.json"
        p.write_text(
            '{\n'
            '  // 行注释\n'
            '  "type": "object",\n'
            '  "properties": {\n'
            '    "a": { "type": "string" }, // 尾注释\n'
            '  },\n'
            '  /* 块注释 */\n'
            '}\n', encoding="utf-8")
        schema = load_schema(str(p))
        assert schema["type"] == "object", schema
        assert "a" in schema["properties"], schema


def main() -> int:
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print(f"[ok] {name}")
        except Exception as e:  # noqa: BLE001 - 自测捕获任意异常
            failed.append((name, e))
            print(f"[FAIL] {name}: {type(e).__name__}: {e}")
    print(f"\n{len(CASES) - len(failed)}/{len(CASES)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
