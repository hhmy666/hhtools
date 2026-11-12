"""Read-only structural summary of a decoded Tile template."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, Tuple

from .models import TemplateDocument, Tile


Profile = Tuple[Tuple[int, int], ...]


@dataclass(frozen=True)
class InspectionIssue:
    code: str
    severity: str
    message: str
    evidence: Tuple[object, ...] = ()


@dataclass(frozen=True)
class StackInspection:
    anchor: Tuple[int, int]
    count: int
    z_levels: Tuple[int, ...]
    tile_indices: Tuple[int, ...]


@dataclass(frozen=True)
class TemplateInspection:
    template_id: str
    total_tiles: int
    a_count: int
    b_count: int
    stacks: Tuple[StackInspection, ...]
    tower_tile_count: int
    tower_ratio: float
    non_tower_layers: Tuple[int, ...]
    symmetry: Tuple[str, ...]
    profiles: Tuple[Profile, ...]
    layer_profiles: Dict[int, Tuple[Profile, ...]]
    issues: Tuple[InspectionIssue, ...]
    non_tower_a_count: int = 0
    non_tower_b_count: int = 0

    @property
    def needs_optimization(self) -> bool:
        return self.tower_tile_count > 0

    @property
    def can_optimize(self) -> bool:
        return self.needs_optimization and not any(i.severity == "error" for i in self.issues)


def recommend_replenish_type(inspection: TemplateInspection):
    """Recommend the dominant original non-tower type; equal counts choose A."""
    a, b = inspection.non_tower_a_count, inspection.non_tower_b_count
    if not a and not b:
        return None
    return 'A' if a >= b else 'B'


def _overlaps(left: Tile, right: Tile) -> bool:
    return abs(left.x - right.x) < 2 and abs(left.y - right.y) < 2


def _symmetry(tiles: Tuple[Tile, ...]) -> Tuple[str, ...]:
    if not tiles:
        return ()
    x_sum = min(tile.x for tile in tiles) + max(tile.x for tile in tiles)
    y_sum = min(tile.y for tile in tiles) + max(tile.y for tile in tiles)
    positions = Counter((tile.x, tile.y, tile.z) for tile in tiles)
    transforms = (
        ("horizontal", lambda x, y: (x_sum - x, y)),
        ("vertical", lambda x, y: (x, y_sum - y)),
        ("rotation", lambda x, y: (x_sum - x, y_sum - y)),
    )
    return tuple(name for name, transform in transforms
                 if all(positions[(x, y, z)] == positions[(*transform(x, y), z)]
                        for x, y, z in positions))


def _profiles(tiles: Tuple[Tile, ...]) -> Tuple[Tuple[Profile, ...], Dict[int, Tuple[Profile, ...]],
                                               Tuple[InspectionIssue, ...]]:
    by_z = defaultdict(list)
    for tile in tiles:
        by_z[tile.z].append(tile)
    per_layer = {}
    issues = []
    for z in sorted(by_z):
        if z == 0:
            continue
        lower_layer = by_z.get(z - 1, ())
        if not lower_layer:
            issues.append(InspectionIssue("missing_lower_non_tower_layer", "warning",
                                          "原有非塔层未接触紧邻下层，原样保留", (z, tuple(t.index for t in by_z[z]))))
        signatures = set()
        for upper in by_z[z]:
            offsets = tuple(sorted(
                ((lower.x - upper.x, lower.y - upper.y)
                 for lower in lower_layer if _overlaps(lower, upper)),
                reverse=True,
            ))
            if offsets:
                signatures.add(offsets)
            else:
                issues.append(InspectionIssue("unsupported_non_tower_tile", "warning",
                                              "原有非塔Tile未接触紧邻下层，原样保留", (upper.index,)))
        per_layer[z] = tuple(sorted(signatures))
    all_profiles = tuple(sorted({profile for profiles in per_layer.values() for profile in profiles}))
    return all_profiles, per_layer, tuple(issues)


def inspect_template(document: TemplateDocument) -> TemplateInspection:
    """Summarize the source; unsafe existing anchors are reported, not rewritten."""
    issues = []
    if document.width is None or document.height is None:
        issues.append(InspectionIssue("missing_board_dimensions", "error",
                                      "模板缺少棋盘宽高，无法验证Tile footprint",
                                      (document.width, document.height)))
    anchor_counts = Counter((stack.x, stack.y) for stack in document.stacks)
    anchor_set = set(anchor_counts)
    for anchor, count in sorted(anchor_counts.items()):
        if count > 1:
            issues.append(InspectionIssue("duplicate_stack_anchor", "error",
                                          "塔锚点重复", (anchor, count)))

    tower_by_anchor = defaultdict(list)
    non_tower = []
    for tile in document.tiles:
        if (tile.x, tile.y) in anchor_set:
            tower_by_anchor[(tile.x, tile.y)].append(tile)
        else:
            non_tower.append(tile)
        if not 2 <= tile.x <= 14 or not 2 <= tile.y <= 16:
            issues.append(InspectionIssue("outside_safe_anchor", "warning",
                                          "源Tile锚点位于新增牌安全区外", (tile.index, tile.x, tile.y)))
        if ((document.width is not None and not 0 <= tile.x <= document.width - 2)
                or (document.height is not None and not 0 <= tile.y <= document.height - 2)):
            issues.append(InspectionIssue("outside_board_footprint", "warning",
                                          "原有Tile的2×2占位超出模板边界，原样保留",
                                          (tile.index, tile.x, tile.y, document.width, document.height)))

    stacks = []
    for stack in document.stacks:
        members = sorted(tower_by_anchor[(stack.x, stack.y)], key=lambda tile: (tile.z, tile.index))
        levels = tuple(tile.z for tile in members)
        stacks.append(StackInspection((stack.x, stack.y), len(members), levels,
                                      tuple(tile.index for tile in members)))
        if not members:
            issues.append(InspectionIssue("empty_stack", "error", "塔锚点没有对应Tile",
                                          ((stack.x, stack.y),)))
        if levels != tuple(range(len(levels))):
            issues.append(InspectionIssue("tower_z_gap", "error", "塔Z不是从0开始的连续序列",
                                          ((stack.x, stack.y), levels)))
        for tile in members:
            if tile.tile_id not in (-10, -11):
                issues.append(InspectionIssue("special_tower_tile", "error", "塔内含特殊Tile",
                                              (tile.index, tile.tile_id)))

    if anchor_set and not non_tower:
        issues.append(InspectionIssue("no_non_tower_surface", "error", "纯塔模板没有承接面"))

    a_count = sum(tile.tile_id == -10 for tile in document.tiles)
    b_count = sum(tile.tile_id == -11 for tile in document.tiles)
    if a_count % 3 or b_count % 3:
        issues.append(InspectionIssue("ab_count_not_multiple_of_three", "error",
                                      "A/B数量必须各自是3的倍数", (a_count, b_count)))

    by_z = defaultdict(list)
    for tile in non_tower:
        by_z[tile.z].append(tile)
    all_by_z = defaultdict(list)
    for tile in document.tiles:
        all_by_z[tile.z].append(tile)
    for z, layer in all_by_z.items():
        for left_index, left in enumerate(layer):
            for right in layer[left_index + 1:]:
                if _overlaps(left, right):
                    issues.append(InspectionIssue("same_layer_overlap", "warning", "原有同层Tile重叠，保留原状；补牌不得新增重叠",
                                                  (z, left.index, right.index)))

    profiles, layer_profiles, profile_issues = _profiles(tuple(non_tower))
    issues.extend(profile_issues)
    tower_count = sum(len(members) for members in tower_by_anchor.values())
    return TemplateInspection(
        document.template_id, len(document.tiles),
        a_count, b_count,
        tuple(stacks), tower_count,
        tower_count / len(document.tiles) if document.tiles else 0.0,
        tuple(sorted(by_z)), _symmetry(tuple(non_tower)),
        profiles, layer_profiles, tuple(issues),
        sum(tile.tile_id == -10 for tile in non_tower),
        sum(tile.tile_id == -11 for tile in non_tower),
    )
