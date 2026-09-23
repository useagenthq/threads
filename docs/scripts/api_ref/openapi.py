"""docs/openapi.json: host-api/openapi.json with its cross-file $refs inlined."""

from pathlib import Path

from .json_access import Json, Obj, load, obj
from .tables import DEV_SERVER, NOT_BUILT_ROUTES, SPEC, SUMMARIES

HOST_OPENAPI = (SPEC / "schema" / "host-api" / "openapi.json").resolve()


def schema_ids() -> dict[str, Path]:
    """$id -> file, so urn: refs resolve like relative ones."""
    ids: dict[str, Path] = {}
    for path in sorted((SPEC / "schema").rglob("*.json")):
        schema = load(path)
        schema_id = schema.get("$id") if isinstance(schema, dict) else None
        if isinstance(schema_id, str):
            ids[schema_id] = path.resolve()
    return ids


def resolve(doc: Json, pointer: str) -> Json:
    node = doc
    for part in pointer.strip("/").split("/"):
        if not isinstance(node, dict):
            raise ValueError(pointer)
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


class Bundler:
    """Moves every schema a $ref points at into components.schemas, once."""

    def __init__(self) -> None:
        self.ids = schema_ids()
        self.files: dict[Path, Json] = {}
        self.schemas: dict[str, Json] = {}
        self.names: dict[tuple[Path, str], str] = {}

    def load(self, path: Path) -> Json:
        if path not in self.files:
            self.files[path] = load(path)
        return self.files[path]

    def component(self, path: Path, pointer: str) -> str:
        if (path, pointer) in self.names:
            return self.names[(path, pointer)]
        parts = [p for p in pointer.split("/") if p not in ("", "$defs", "properties")]
        name = "_".join(parts)
        if not path.name.startswith("host-api"):
            name = path.name.split(".")[0] + "_" + name
        self.names[(path, pointer)] = name
        self.schemas[name] = None  # reserve before recursing: schemas may be recursive
        self.schemas[name] = self.rewrite(resolve(self.load(path), pointer), path)
        return name

    def ref(self, ref: str, base: Path) -> str:
        file, _, pointer = ref.partition("#")
        if file in self.ids:
            path = self.ids[file]
        else:
            path = (base.parent / file).resolve() if file else base
        if path == HOST_OPENAPI and not file:
            return ref
        return f"#/components/schemas/{self.component(path, pointer)}"

    def rewrite(self, node: Json, base: Path) -> Json:
        if isinstance(node, list):
            return [self.rewrite(x, base) for x in node]
        if not isinstance(node, dict):
            return node
        out: Obj = {}
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str) and not v.startswith("#/components"):
                out[k] = self.ref(v, base)
            elif k != "$id":
                out[k] = self.rewrite(v, base)
        return out


def built_paths(paths: Obj) -> Obj:
    """Routes without the operations that are not built, with readable summaries."""
    out: Obj = {}
    for route, item in paths.items():
        ops = {
            m: o
            for m, o in obj(item).items()
            if not (isinstance(o, dict) and o.get("operationId") in NOT_BUILT_ROUTES)
        }
        for op in ops.values():
            if isinstance(op, dict):
                op_id = op.get("operationId")
                if isinstance(op_id, str) and op_id in SUMMARIES:
                    op["summary"] = SUMMARIES[op_id]
        if any(isinstance(o, dict) and "operationId" in o for o in ops.values()):
            out[route] = ops
    return out


def bundle_openapi() -> Obj:
    bundler = Bundler()
    bundled = obj(bundler.rewrite(load(HOST_OPENAPI), HOST_OPENAPI))
    components = obj(bundled.setdefault("components", {}))
    components["schemas"] = dict(sorted(bundler.schemas.items()))
    bundled["servers"] = [{"url": DEV_SERVER, "description": "threads dev"}]
    bundled["paths"] = built_paths(obj(bundled["paths"]))
    return bundled
