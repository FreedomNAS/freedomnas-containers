import json

import records


def test_json_lines_keep_their_names():
    assert records.parse('{"a": 1}\n\n{"a": 2}\n', {}) == [{"a": 1}, {"a": 2}]


def test_tsv_fields_values_and_defaults():
    spec = {"format": "tsv", "columns": ["name", "status", "image", "version"],
            "fields": {"name": "{name}", "state": "{status}", "image": "{image}", "label": "{name} ({version})"},
            "values": {"state": {"UP": "running", "*": "exited"}},
            "defaults": {"image": "FreeBSD {version}"}}
    rows = records.parse("web\tUP\t \t15.1\t\nold\tDOWN\tnginx\t14.3\t\n", spec)
    assert rows == [
        {"name": "web", "state": "running", "image": "FreeBSD 15.1", "label": "web (15.1)"},
        {"name": "old", "state": "exited", "image": "nginx", "label": "old (14.3)"}]


def test_json_path_lines_text_and_required():
    describe = json.dumps({"name": "sito", "services": [{"name": "web", "jail": "sito-web"}, {"name": "x"}]})
    spec = {"format": "json", "path": "services", "fields": {"name": "{jail}"}, "required": ["name"]}
    assert records.parse(describe, spec) == [{"name": "sito-web"}]
    assert records.parse("", spec) == []
    assert records.parse("a\n b \n", {"format": "lines", "fields": {"address": "{line}"}}) == [
        {"address": "a"}, {"address": "b"}]
    text = {"format": "text", "fields": {"version": "{text}", "driver": "zfs"}, "required": ["version"]}
    assert records.parse("5.9.1\n", text) == [{"version": "5.9.1", "driver": "zfs"}]
    assert records.parse("", text) == []
