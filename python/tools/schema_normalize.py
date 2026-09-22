"""Rewrites the event schema into an equivalent form that datamodel-codegen maps onto strict models.

The committed schema is written for validators: events are `allOf[Envelope, oneOf[ev_*]]`, refs
carry sibling keywords, and cross-field rules use `if`/`then`/`else`. The generator can't express
those, so this module rewrites them without changing which values are accepted:

- every event becomes one closed object (envelope plus its narrowed fields), named `<Type>Event`,
  with its `data` hoisted to `<Type>Data`; `Event` is a union discriminated on `type`;
- `UnknownEvent` becomes the bare envelope. Its `not KnownTag` clause is enforced by the parser,
  which routes a line to `Event` exactly when `KnownTag` matches;
- `allOf` and `$ref`-with-siblings are merged into single objects;
- rules no type can hold (`if`/`then`/`else`, `not`, `minProperties`, and `oneOf` over `required`)
  move verbatim to `x-allOf`, which the generator copies into `json_schema_extra["allOf"]` and the
  runtime base model evaluates (`threads._strict_model`).
"""

from collections.abc import Iterable

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None
type Obj = dict[str, Json]

DEFS_PREFIX = "#/$defs/"
JSON_OBJECT = "#/$defs/JsonObject"
# Keywords the runtime condition checker understands. Anything else in a condition is a bug.
CHECKER_KEYWORDS = frozenset(
    {"if", "then", "else", "not", "anyOf", "oneOf", "allOf", "required", "properties", "const"}
    | {"enum", "minProperties", "description"}
)
TRIGGERS = frozenset({"if", "not", "anyOf", "oneOf", "minProperties"})
DISCRIMINATORS = ("type", "kind")


class SchemaShapeError(Exception):
    """The committed schema uses a construct this rewrite does not know how to keep exact."""


def as_obj(node: Json) -> Obj:
    if not isinstance(node, dict):
        raise SchemaShapeError(f"expected an object, got {node!r}")
    return node


def as_list(node: Json) -> list[Json]:
    if not isinstance(node, list):
        raise SchemaShapeError(f"expected an array, got {node!r}")
    return node


def ref_name(node: Obj) -> str:
    ref = node["$ref"]
    if not isinstance(ref, str) or not ref.startswith(DEFS_PREFIX):
        raise SchemaShapeError(f"unsupported $ref {ref!r}")
    return ref.removeprefix(DEFS_PREFIX)


def pascal(snake: str) -> str:
    return "".join(word.capitalize() for word in snake.split("_"))


class Normalizer:
    def __init__(self, schema: Obj) -> None:
        self.source = as_obj(schema["$defs"])
        self.defs: Obj = {}

    def run(self) -> Obj:
        skip = {"Envelope", "Event", "UnknownEvent"}
        for name, node in self.source.items():
            body = as_obj(node)
            # Condition-only defs (TextOrContent, TextOrRef) are inlined where they're used.
            if name.startswith("ev_") or name in skip or is_condition(body):
                continue
            if set(body) <= {"$ref", "description"}:
                # A pure alias (EventId -> Uuid) keeps its own name but takes the target's body,
                # so each id is its own type in the output.
                body = {**self.deref(ref_name(body)), **body}
                del body["$ref"]
            self.defs[name] = self.norm(body)
        self.split_events()
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": self.defs}

    def deref(self, name: str) -> Obj:
        return as_obj(self.source[name])

    # Events -----------------------------------------------------------------------------------

    def split_events(self) -> None:
        envelope = self.deref("Envelope")
        event_all_of = as_list(self.deref("Event")["allOf"])
        refs: list[Json] = []
        for branch in as_list(as_obj(event_all_of[1])["oneOf"]):
            source_name = ref_name(as_obj(branch))
            tag = source_name.removeprefix("ev_")
            self.defs[f"{pascal(tag)}Event"] = self.event(envelope, self.deref(source_name), tag)
            refs.append({"$ref": f"{DEFS_PREFIX}{pascal(tag)}Event"})
        self.defs["Event"] = {"oneOf": refs, "discriminator": {"propertyName": "type"}}
        unknown_props: Obj = {**as_obj(envelope["properties"]), "data": {"$ref": JSON_OBJECT}}
        unknown: Obj = {
            **envelope,
            "description": self.deref("UnknownEvent")["description"],
            "properties": unknown_props,
        }
        self.defs["UnknownEvent"] = self.norm(unknown)

    def event(self, envelope: Obj, event: Obj, tag: str) -> Obj:
        data_name = f"{pascal(tag)}Data"
        self.defs[data_name] = self.norm(as_obj(as_obj(event["properties"])["data"]))
        narrowed: Obj = {**as_obj(event["properties"]), "data": {"$ref": DEFS_PREFIX + data_name}}
        actor = narrowed.get("actor")
        if isinstance(actor, dict) and set(actor) != {"$ref"}:
            self.defs[f"{pascal(tag)}Actor"] = self.norm(actor)
            narrowed["actor"] = {"$ref": f"{DEFS_PREFIX}{pascal(tag)}Actor"}
        overlay: Obj = {**event, "properties": narrowed}
        return self.flatten({"allOf": [envelope, overlay]})

    # Generic rewrite --------------------------------------------------------------------------

    def norm(self, node: Json) -> Json:
        if isinstance(node, list):
            return [self.norm(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "allOf" in node or ("$ref" in node and set(node) - {"$ref", "description"}):
            return self.flatten(node)
        out: Obj = {}
        rest = dict(node)
        lifted = pop_conditions(rest)
        for key, value in rest.items():
            if key == "properties":
                out[key] = {name: self.norm(prop) for name, prop in as_obj(value).items()}
            elif key in ("const", "enum", "required"):
                out[key] = value
            else:
                out[key] = self.norm(value)
        add_conditions(out, [self.condition(c) for c in lifted])
        self.add_discriminator(out)
        return out

    def flatten(self, node: Obj) -> Obj:
        rest = {k: v for k, v in node.items() if k not in ("allOf", "$ref")}
        parts: list[Json] = []
        if "$ref" in node:
            parts.append({"$ref": node["$ref"]})
        parts.extend(as_list(node.get("allOf", [])))
        parts.append(rest)
        merged: Obj = {}
        for part in parts:
            part_obj = as_obj(part)
            resolved = self.deref(ref_name(part_obj)) if "$ref" in part_obj else part_obj
            if is_condition(resolved):
                add_conditions(merged, [self.condition(resolved)])
            else:
                merge(merged, as_obj(self.norm(resolved)))
        return merged

    def condition(self, node: Json) -> Json:
        """Copies a condition, replacing refs by the part the base schema doesn't already check."""
        if isinstance(node, list):
            return [self.condition(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return self.ref_difference(node)
        unknown = set(node) - CHECKER_KEYWORDS
        if unknown:
            raise SchemaShapeError(f"condition uses unsupported keywords {sorted(unknown)}")
        out: Obj = {}
        for key, value in node.items():
            if key == "properties":
                out[key] = {name: self.condition(p) for name, p in as_obj(value).items()}
            elif key in ("const", "enum", "required", "minProperties", "description"):
                out[key] = value
            else:
                out[key] = self.condition(value)
        return out

    def ref_difference(self, node: Obj) -> Obj:
        # A then-branch like {"$ref": ActorWithPrincipal} narrows a property the envelope already
        # validates as Actor. ActorWithPrincipal is {"$ref": Actor, "required": [...]}, so only
        # its `required` is new. Any other ref shape here would need a real validator.
        target = self.deref(ref_name(node))
        if set(target) != {"$ref", "required"} or set(node) != {"$ref"}:
            raise SchemaShapeError(f"unsupported $ref inside a condition: {node!r}")
        return {"required": target["required"]}

    def add_discriminator(self, node: Obj) -> None:
        one_of = node.get("oneOf")
        if not isinstance(one_of, list) or not all(
            isinstance(b, dict) and "$ref" in b for b in one_of
        ):
            return
        branches = [self.deref(ref_name(as_obj(b))) for b in one_of]
        for key in DISCRIMINATORS:
            if all(has_const(branch, key) for branch in branches):
                node["discriminator"] = {"propertyName": key}
                return


def has_const(node: Obj, key: str) -> bool:
    props = node.get("properties")
    return (
        isinstance(props, dict)
        and isinstance(props.get(key), dict)
        and "const" in as_obj(props[key])
    )


def is_pure_condition(node: Json) -> bool:
    return isinstance(node, dict) and set(node) <= CHECKER_KEYWORDS


def is_condition(node: Obj) -> bool:
    """An allOf part that constrains without adding shape: nothing the generator could type."""
    keys = set(node)
    if not keys & TRIGGERS or not keys <= TRIGGERS | {"then", "else", "description"}:
        return False
    return all(is_pure_condition(branch) for branch in as_list(node.get("oneOf", [])))


def pop_conditions(node: Obj) -> list[Json]:
    lifted: list[Json] = []
    if "if" in node:
        lifted.append({k: node.pop(k) for k in ("if", "then", "else") if k in node})
    for key in ("not", "minProperties"):
        if key in node:
            lifted.append({key: node.pop(key)})
    one_of = node.get("oneOf")
    if isinstance(one_of, list) and all(is_pure_condition(branch) for branch in one_of):
        lifted.append({"oneOf": node.pop("oneOf")})
    return lifted


def add_conditions(node: Obj, conditions: Iterable[Json]) -> None:
    items = list(conditions)
    if items:
        node["x-allOf"] = [*as_list(node.get("x-allOf", [])), *items]


def merge(into: Obj, part: Obj) -> None:
    """allOf merge. Every overlap in v1 narrows (a const, a sub-enum, a stricter ref), so the later
    part wins; enums intersect so a narrowing enum can't widen."""
    for key, value in part.items():
        if key == "properties":
            props = as_obj(into.setdefault("properties", {}))
            for name, prop in as_obj(value).items():
                props[name] = merge_property(props.get(name), as_obj(prop))
        elif key == "required":
            seen = as_list(into.get("required", []))
            into["required"] = [*seen, *(r for r in as_list(value) if r not in seen)]
        elif key == "x-allOf":
            add_conditions(into, as_list(value))
        else:
            into[key] = value


def merge_property(current: Json, later: Obj) -> Json:
    if isinstance(current, dict) and "enum" in current and "enum" in later:
        allowed = as_list(later["enum"])
        return {**later, "enum": [v for v in as_list(current["enum"]) if v in allowed]}
    return later


def normalize(schema: Obj) -> Obj:
    return Normalizer(schema).run()
