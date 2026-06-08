#!/usr/bin/env python3
"""Down-convert an OpenAPI 3.1 spec to 3.0 so that tools using the
`openapiv3` crate (e.g. progenitor) can parse it.

Handled differences:
  * openapi version string 3.1.x -> 3.0.3
  * info.license.identifier (SPDX) -> dropped (3.0 has no identifier field)
  * schema `type` as a list (e.g. [string, 'null']) -> single type + nullable: true
  * schema-level `examples` (a sequence) -> `example` (singular)
  * `const: X` -> `enum: [X]`
  * exclusiveMinimum/Maximum as numbers -> boolean form + minimum/maximum
"""

import sys
from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True
yaml.width = 4096


def is_schema_like(node):
    # Heuristic: a mapping that carries JSON-Schema keywords.
    if not isinstance(node, dict):
        return False
    keys = set(node.keys())
    schema_markers = {
        "type",
        "properties",
        "items",
        "allOf",
        "anyOf",
        "oneOf",
        "additionalProperties",
        "enum",
        "const",
        "format",
        "$ref",
    }
    return bool(keys & schema_markers)


def fix_type(node):
    t = node.get("type")
    if t == "null":
        node["nullable"] = True
        del node["type"]
        return
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        has_null = any(x == "null" for x in t)
        if has_null:
            node["nullable"] = True
        if len(non_null) == 1:
            node["type"] = non_null[0]
        elif len(non_null) == 0:
            # Only null -> drop the type, keep nullable.
            del node["type"]
        else:
            # Union of real types is unrepresentable in 3.0; keep the first
            # and rely on nullable. This is a lossy but parseable fallback.
            node["type"] = non_null[0]


def fix_examples(node):
    # In a schema object, 3.1 uses `examples` as a list; 3.0 uses `example`.
    if "examples" in node and isinstance(node["examples"], list):
        examples = node["examples"]
        if examples:
            node["example"] = examples[0]
        del node["examples"]


def fix_const(node):
    if "const" in node:
        node["enum"] = [node.pop("const")]


def fix_exclusive(node):
    for bound, limit in (
        ("exclusiveMinimum", "minimum"),
        ("exclusiveMaximum", "maximum"),
    ):
        v = node.get(bound)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            node[limit] = v
            node[bound] = True


def fix_deepobject_param(node):
    # progenitor's openapiv3 backend doesn't support `style: deepObject`.
    # Flatten such query parameters to a plain string so generation works.
    if node.get("style") == "deepObject" and node.get("in") == "query":
        node.pop("style", None)
        node.pop("explode", None)
        node["schema"] = {"type": "string"}


def walk(node, in_schema=False):
    if isinstance(node, dict):
        if node.get("style") == "deepObject":
            fix_deepobject_param(node)
        schema_here = in_schema or is_schema_like(node)
        if schema_here:
            fix_type(node)
            fix_const(node)
            fix_exclusive(node)
            fix_examples(node)
        for key, value in list(node.items()):
            # `properties` values are schemas; `examples` (media-type) are not.
            child_in_schema = key in (
                "items",
                "additionalProperties",
                "not",
            )
            if key in ("properties", "definitions", "$defs", "patternProperties"):
                if isinstance(value, dict):
                    for sub in value.values():
                        walk(sub, in_schema=True)
                continue
            if key in ("allOf", "anyOf", "oneOf"):
                if isinstance(value, list):
                    # Strip pure-null subschemas ({type: 'null'}) which are
                    # invalid in 3.0. We deliberately do NOT mark the parent
                    # `nullable` here: progenitor turns a nullable oneOf/anyOf
                    # into a recursive wrapper type with the same name, which
                    # causes duplicate-definition and infinite-size errors.
                    # Optionality is already conveyed via `required`.
                    kept = []
                    for sub in value:
                        if (
                            isinstance(sub, dict)
                            and sub.get("type") == "null"
                            and len(sub) == 1
                        ):
                            continue
                        kept.append(sub)
                    node[key] = kept
                    for sub in kept:
                        walk(sub, in_schema=True)
                continue
            if key == "schema":
                walk(value, in_schema=True)
                continue
            walk(value, in_schema=child_in_schema)
    elif isinstance(node, list):
        for item in node:
            walk(item, in_schema=in_schema)


def dedupe_parameters(doc):
    """Remove duplicate parameters within the same operation.

    Some operations declare the same parameter twice (e.g. an inline
    `Idempotency-Key` header plus a `$ref` to the shared `idempotency-key`
    parameter). progenitor maps both to the same Rust argument name and fails
    to compile. We keep the first occurrence of each (name, in) pair.
    """
    comp_params = doc.get("components", {}).get("parameters", {})

    def resolve(p):
        if isinstance(p, dict) and "$ref" in p:
            ref = p["$ref"]
            key = ref.rsplit("/", 1)[-1]
            target = comp_params.get(key, {})
            return target.get("name"), target.get("in")
        if isinstance(p, dict):
            return p.get("name"), p.get("in")
        return None, None

    def dedupe_list(params):
        seen = set()
        result = []
        for p in params:
            name, loc = resolve(p)
            ident = (name.lower() if isinstance(name, str) else name, loc)
            if ident in seen:
                continue
            seen.add(ident)
            result.append(p)
        return result

    HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path_item in doc.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        if isinstance(path_item.get("parameters"), list):
            path_item["parameters"] = dedupe_list(path_item["parameters"])
        for method, op in path_item.items():
            if (
                method in HTTP_METHODS
                and isinstance(op, dict)
                and isinstance(op.get("parameters"), list)
            ):
                op["parameters"] = dedupe_list(op["parameters"])


def denullify_path_param_schemas(doc):
    """Strip `nullable` from component schemas referenced by path parameters.

    A required path parameter can never be null. progenitor turns a nullable
    schema into a newtype wrapper around `Option<...>` which doesn't implement
    `Display`, so `to_string()` in the generated URL builder fails to compile.
    """
    components = doc.get("components", {})
    comp_params = components.get("parameters", {})
    comp_schemas = components.get("schemas", {})

    def schema_refs(schema):
        # Yield component-schema names directly referenced by a param schema.
        if not isinstance(schema, dict):
            return
        if "$ref" in schema:
            yield schema["$ref"].rsplit("/", 1)[-1]
        for key in ("oneOf", "anyOf", "allOf"):
            for sub in schema.get(key, []) or []:
                if isinstance(sub, dict) and "$ref" in sub:
                    yield sub["$ref"].rsplit("/", 1)[-1]

    referenced = set()

    def collect(param):
        if isinstance(param, dict) and "$ref" in param:
            key = param["$ref"].rsplit("/", 1)[-1]
            param = comp_params.get(key, {})
        if isinstance(param, dict) and param.get("in") == "path":
            for name in schema_refs(param.get("schema")):
                referenced.add(name)

    HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    for path_item in doc.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for param in path_item.get("parameters", []) or []:
            collect(param)
        for method, op in path_item.items():
            if method in HTTP_METHODS and isinstance(op, dict):
                for param in op.get("parameters", []) or []:
                    collect(param)

    for name in referenced:
        schema = comp_schemas.get(name)
        if isinstance(schema, dict):
            schema.pop("nullable", None)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f:
        doc = yaml.load(f)

    v = str(doc.get("openapi", ""))
    if v.startswith("3.1"):
        doc["openapi"] = "3.0.3"

    info = doc.get("info", {})
    lic = info.get("license") if isinstance(info, dict) else None
    if isinstance(lic, dict) and "identifier" in lic:
        del lic["identifier"]

    walk(doc)
    dedupe_parameters(doc)
    denullify_path_param_schemas(doc)

    with open(dst, "w") as f:
        yaml.dump(doc, f)
    print(f"Wrote {dst}")


if __name__ == "__main__":
    main()
