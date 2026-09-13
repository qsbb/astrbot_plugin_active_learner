from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "_conf_schema.json"
I18N_DIR = ROOT / ".astrbot-plugin" / "i18n"
LOCALES = ("zh-CN", "en-US")
NO_PREFIX_GROUP = "general"
MAX_LOCALE_BYTES = 1024 * 1024
FORBIDDEN_I18N_KEYS = frozenset({"options", "default", "type"})


class DuplicateKeyError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
    )


def _option_fields(schema: dict) -> dict[str, dict]:
    return {
        field: spec
        for field, spec in schema.items()
        if isinstance(spec, dict) and isinstance(spec.get("options"), list)
    }


def _group_for(spec: dict) -> str:
    description = spec.get("description", "")
    match = re.match(r"^\[([^\]]+)\]", description)
    return match.group(1) if match else NO_PREFIX_GROUP


def _config_entries(document: dict):
    config = document.get("config", {})
    assert isinstance(config, dict)
    for group, fields in config.items():
        assert isinstance(group, str) and group.strip()
        assert isinstance(fields, dict)
        for field, payload in fields.items():
            assert isinstance(field, str) and field.strip()
            assert isinstance(payload, dict)
            yield group, field, payload


def _assert_no_machine_keys(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in FORBIDDEN_I18N_KEYS, f"{path}.{key} is a machine-value key"
            _assert_no_machine_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_machine_keys(child, f"{path}[{index}]")


@pytest.fixture(scope="module")
def schema() -> dict:
    return _load_json(SCHEMA_PATH)


@pytest.fixture(scope="module")
def documents() -> dict[str, dict]:
    result = {}
    for locale in LOCALES:
        path = I18N_DIR / f"{locale}.json"
        assert path.is_file(), f"missing locale file: {path}"
        assert path.stat().st_size < MAX_LOCALE_BYTES, f"{path} must be smaller than 1 MB"
        try:
            result[locale] = _load_json(path)
        except DuplicateKeyError as exc:
            pytest.fail(f"{path}: {exc}")
    return result


def test_schema_option_labels_cover_every_option(schema):
    fields = _option_fields(schema)
    assert fields, "no option fields found in _conf_schema.json"
    for field, spec in fields.items():
        labels = spec.get("labels")
        assert isinstance(labels, list), f"{field}.labels must be a list"
        assert len(labels) == len(spec["options"]), f"{field}.labels length mismatch"
        assert all(isinstance(label, str) and label.strip() for label in labels), field


def test_i18n_config_paths_exist_in_schema(schema, documents):
    fields = _option_fields(schema)
    expected_paths = {(_group_for(spec), field) for field, spec in fields.items()}
    for locale, document in documents.items():
        for group, field, _ in _config_entries(document):
            assert (group, field) in expected_paths, (
                f"{locale}: config.{group}.{field} does not exist in _conf_schema.json"
            )


def test_english_labels_cover_all_option_fields(schema, documents):
    fields = _option_fields(schema)
    expected_paths = {(_group_for(spec), field) for field, spec in fields.items()}
    actual_paths = {
        (group, field)
        for group, field, _ in _config_entries(documents["en-US"])
    }
    assert actual_paths == expected_paths


def test_i18n_labels_match_schema_options(schema, documents):
    fields = _option_fields(schema)
    for locale, document in documents.items():
        for group, field, payload in _config_entries(document):
            spec = fields[field]
            assert group == _group_for(spec), f"{locale}: wrong group for {field}"
            if "labels" in payload:
                labels = payload["labels"]
                assert isinstance(labels, list), f"{locale}.{group}.{field}.labels must be a list"
                assert len(labels) == len(spec["options"]), (
                    f"{locale}.{group}.{field}.labels length mismatch"
                )
                assert all(isinstance(label, str) and label.strip() for label in labels), (
                    f"{locale}.{group}.{field}: labels must be non-empty strings"
                )


def test_i18n_does_not_override_machine_values(documents):
    for locale, document in documents.items():
        _assert_no_machine_keys(document, locale)


def test_pages_have_metadata_in_both_locales(documents):
    page_names = {path.name for path in (ROOT / "pages").iterdir() if path.is_dir()}
    assert page_names, "no page directories found"
    for locale, document in documents.items():
        pages = document.get("pages")
        assert isinstance(pages, dict), f"{locale}.pages must be an object"
        assert set(pages) == page_names, f"{locale}.pages must cover every page directory"
        for page_name in page_names:
            metadata = pages[page_name]
            assert isinstance(metadata, dict), f"{locale}.pages.{page_name} must be an object"
            for field in ("title", "description"):
                value = metadata.get(field)
                assert isinstance(value, str) and value.strip(), (
                    f"{locale}.pages.{page_name}.{field} must be a non-empty string"
                )
