import json

import pytest

from devmind.agent.core import _parse_patch


def test_parse_patch_reads_first_json_object_from_fenced_text():
    expected = {"operation": "insert", "path": "app.py", "content": "VALUE = 1"}

    assert _parse_patch(f"```json\n{json.dumps(expected)}\n```") == expected


def test_parse_patch_ignores_invalid_prefix_and_uses_first_object():
    first = {"operation": "insert", "path": "app.py", "content": "{ value }"}
    second = {"operation": "insert", "path": "wrong.py", "content": "ignored"}

    assert _parse_patch(f"not json {{broken}} {json.dumps(first)}{json.dumps(second)}") == first


def test_parse_patch_rejects_responses_without_json():
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_patch("no edit was proposed")
