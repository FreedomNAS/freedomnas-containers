"""
What a profile command prints, read as records with the module's own field
names, as the profile describes it under `<action>_output`:

    format    jsonl (default): one JSON object per line
              json: one JSON document; "path" names the list inside it
              tsv: tab-separated values named by "columns"
              lines: one record {"line": ...} per non-empty line
              text: the whole output as {"text": ...}
    fields    field -> template over the record ("{Name}", "zfs", "{a} · {b}");
              without it the record keeps its own names
    values    field -> {value: replacement, "*": anything else}
    defaults  field -> template used when the field came out empty
    required  fields that must not be empty; a record without them is dropped

Templates use str.format names; a name the record lacks is empty.
"""

from __future__ import annotations

import json
import string
from typing import Any, Dict, List


class _Row(dict):
    def __missing__(self, key):
        return ""


_FORMATTER = string.Formatter()


def render(template: Any, values: Dict[str, Any]) -> Any:
    if not isinstance(template, str):
        return template
    row = _Row({k: "" if v is None else v for k, v in values.items()})
    return _FORMATTER.vformat(template, (), row)


def _raw(out: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = spec.get("format", "jsonl")
    if kind == "jsonl":
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    if kind == "json":
        data = json.loads(out) if out.strip() else []
        for key in (spec.get("path") or "").split("."):
            if key:
                data = (data or {}).get(key) if isinstance(data, dict) else None
        if isinstance(data, dict):
            return [data]
        return list(data or [])
    if kind == "tsv":
        columns = spec["columns"]
        rows = []
        for line in out.splitlines():
            cells = [cell.strip() for cell in line.split("\t")]
            if len(cells) >= len(columns) and cells[0]:
                rows.append(dict(zip(columns, cells)))
        return rows
    if kind == "lines":
        return [{"line": line.strip()} for line in out.splitlines() if line.strip()]
    if kind == "text":
        return [{"text": out.strip()}]
    raise ValueError(f"Unknown output format '{kind}'")


def parse(out: str, spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = spec or {}
    result = []
    for row in _raw(out or "", spec):
        if not isinstance(row, dict):
            continue
        fields = spec.get("fields")
        record = {key: render(template, row) for key, template in fields.items()} if fields else dict(row)
        for key, mapping in (spec.get("values") or {}).items():
            value = record.get(key)
            record[key] = mapping.get(str(value), mapping.get("*", value))
        for key, template in (spec.get("defaults") or {}).items():
            if record.get(key) in (None, ""):
                record[key] = render(template, row)
        if all(record.get(key) not in (None, "") for key in spec.get("required") or []):
            result.append(record)
    return result


def spec(engine, category: str, action: str) -> Dict[str, Any]:
    try:
        return engine.get_profile_value(category, f"{action}_output") or {}
    except Exception:
        return {}


def run(engine, category: str, action: str, timeout: int = 60, **kw) -> List[Dict[str, Any]]:
    out, _ = engine.execute(category, action, timeout=timeout, **kw)
    return parse(out, spec(engine, category, action))
