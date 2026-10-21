"""YAML/JSON configuration loading and saving (ARC-05, CFG-07, GUI-06)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def load_model(path: str | Path, model: type[M]) -> M:
    """Load and validate a YAML or JSON file into `model`. Raises pydantic.ValidationError
    naming the offending field if the content is invalid."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    return model.model_validate(data)


def save_model(obj: BaseModel, path: str | Path) -> None:
    p = Path(path)
    data = obj.model_dump(mode="json")
    if p.suffix.lower() == ".json":
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def snapshot_json(*objs: BaseModel) -> str:
    """Full configuration snapshot for embedding in result files (CFG-07)."""
    return json.dumps({type(o).__name__: o.model_dump(mode="json") for o in objs}, indent=1)
