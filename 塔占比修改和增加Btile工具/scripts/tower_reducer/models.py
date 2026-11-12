"""Editable business view with the original wire order retained alongside it."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .protobuf_wire import WireField


@dataclass
class Tile:
    index: int
    top_field_index: int
    x: int
    y: int
    z: int
    tile_id: int
    debuff_data_index: Optional[int]
    fields: Tuple[WireField, ...]
    original_values: Tuple[int, int, int, int, Optional[int]] = field(repr=False)


@dataclass
class Stack:
    index: int
    top_field_index: int
    x: int
    y: int
    direction: int
    fields: Tuple[WireField, ...]


@dataclass
class TemplateDocument:
    raw: bytes
    fields: Tuple[WireField, ...]
    template_id: str
    template_id_field_index: int
    tiles: List[Tile]
    stacks: List[Stack]
    debuff_data: List[bytes]
    version: Optional[int] = None
    duplicate_id: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    original_template_id: str = field(default="", repr=False)
    original_metadata: Tuple[Optional[int], Optional[str], Optional[int], Optional[int]] = field(
        default=(None, None, None, None), repr=False
    )
    original_tile_positions: Tuple[int, ...] = field(default=(), repr=False)
    original_stack_values: Tuple[Tuple[int, int, int, int], ...] = field(default=(), repr=False)
    original_debuff_data: Tuple[bytes, ...] = field(default=(), repr=False)
