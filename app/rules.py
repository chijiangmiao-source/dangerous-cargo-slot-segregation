"""隔离规则与裁决逻辑（纯函数，不依赖 Web 层）。"""

from dataclasses import dataclass
from typing import Sequence

from .models import Container

# 固定规则表（键为无序类别对，对称适用）：
#   A-B 至少 3，A-C 至少 2，B-D 至少 2；
#   其余任意组合（含同类，如 A-A）一律至少 1。
_PAIR_REQUIREMENTS: dict[frozenset[str], int] = {
    frozenset({"A", "B"}): 3,
    frozenset({"A", "C"}): 2,
    frozenset({"B", "D"}): 2,
}
DEFAULT_REQUIREMENT = 1

Position = tuple[int, int, int]


def required_distance(category_a: str, category_b: str) -> int:
    """返回两类危险品箱之间要求的最小曼哈顿距离。"""
    return _PAIR_REQUIREMENTS.get(frozenset({category_a, category_b}), DEFAULT_REQUIREMENT)


def manhattan_distance(a: Container, b: Container) -> int:
    """排差、列差、层差三者绝对值之和（非欧氏距离，不跳过中间空位）。"""
    return (
        abs(a.row - b.row)
        + abs(a.col - b.col)
        + abs(a.tier - b.tier)
    )


@dataclass(frozen=True)
class Conflict:
    smaller: Container
    larger: Container
    actual_distance: int
    required_distance: int


@dataclass(frozen=True)
class AdjudicationResult:
    pairs_compared: int
    first_conflict: Conflict | None


def _position(container: Container) -> Position:
    return (container.row, container.col, container.tier)


def adjudicate(containers: Sequence[Container]) -> AdjudicationResult:
    """比较全部箱位对，返回排序后的首个冲突；无冲突返回比较对数。

    冲突判定：实际距离 < 要求距离（恰好相等为合规）。
    排序键：先按一对箱中较小箱位的 (排, 列, 层)，再按较大箱位的
    (排, 列, 层)；类别不参与排序。因此首个冲突与输入顺序无关，唯一确定。
    """
    first: tuple[tuple[Position, Position], Conflict] | None = None
    pairs_compared = 0

    for i in range(len(containers)):
        for j in range(i + 1, len(containers)):
            left, right = containers[i], containers[j]
            pairs_compared += 1

            actual = manhattan_distance(left, right)
            required = required_distance(left.category, right.category)
            if actual >= required:
                continue

            # 坐标唯一，故同一对内的大小次序无并列。
            if _position(left) > _position(right):
                left, right = right, left
            key = (_position(left), _position(right))
            conflict = Conflict(left, right, actual, required)

            if first is None or key < first[0]:
                first = (key, conflict)

    return AdjudicationResult(
        pairs_compared=pairs_compared,
        first_conflict=None if first is None else first[1],
    )


@dataclass(frozen=True)
class SlotRecommendation:
    coordinate: Position
    search_distance: int


def _shell_coordinates(
    center: Position,
    radius: int,
    bounds: tuple[int, int, int],
) -> list[Position]:
    """生成以 center 为中心、曼哈顿距离恰为 radius 的舱内壳层坐标。

    各轴先按舱段边界裁剪偏移范围，并用其余两轴在舱内可容纳的最大偏移
    收紧本轴下界（窄舱中 |dr| 过小则剩余距离无法在列/层方向展开），因此
    不物化完整网格：跨全部壳层的总迭代量不超过舱格数的常数倍，稀疏舱
    则通常在最近壳层即返回。返回结果按 (排, 列, 层) 字典序排列，保证
    同层并列时取首项与输入顺序无关。
    """
    max_row, max_col, max_tier = bounds
    cr, cc, ct = center
    cap_col = max(cc - 1, max_col - cc)
    cap_tier = max(ct - 1, max_tier - ct)

    points: list[Position] = []
    row_off_lo, row_off_hi = 1 - cr, max_row - cr
    col_off_lo, col_off_hi = 1 - cc, max_col - cc
    # |dc|+|dt| 在舱内至多为 cap_col+cap_t，故 |dr| 不得低于此下界。
    dr_abs_lo = max(0, radius - cap_col - cap_tier)

    for abs_dr in range(dr_abs_lo, radius + 1):
        dr_values = (0,) if abs_dr == 0 else (-abs_dr, abs_dr)
        rest_r = radius - abs_dr
        # 剩余距离中 |dt| 至多为 cap_tier，故 |dc| 不得低于此下界。
        dc_abs_lo = max(0, rest_r - cap_tier)
        for dr in dr_values:
            if not row_off_lo <= dr <= row_off_hi:
                continue
            row = cr + dr
            for abs_dc in range(dc_abs_lo, rest_r + 1):
                dt_abs = rest_r - abs_dc
                dc_values = (0,) if abs_dc == 0 else (-abs_dc, abs_dc)
                for dc in dc_values:
                    if not col_off_lo <= dc <= col_off_hi:
                        continue
                    col = cc + dc
                    dt_values = (0,) if dt_abs == 0 else (-dt_abs, dt_abs)
                    for dt in dt_values:
                        tier = ct + dt
                        if 1 <= tier <= max_tier:
                            points.append((row, col, tier))

    points.sort()
    return points


def _manhattan_ball_offsets(radius: int) -> tuple[tuple[int, int, int], ...]:
    """预计算 |dr|+|dc|+|dt| <= radius 的全部偏移（半径 0/1/2 仅 1/7/25 个）。"""
    offsets: list[tuple[int, int, int]] = []
    for dr in range(-radius, radius + 1):
        rest_row = radius - abs(dr)
        for dc in range(-rest_row, rest_row + 1):
            rest_col = rest_row - abs(dc)
            for dt in range(-rest_col, rest_col + 1):
                offsets.append((dr, dc, dt))
    return tuple(offsets)


# 隔离要求距离至多为 3：候选不安全当且仅当半径 (要求-1) 邻域内有对应类别箱。
_NEIGHBOR_OFFSETS = {
    radius: _manhattan_ball_offsets(radius) for radius in range(3)
}


def recommend_slot(
    max_row: int,
    max_col: int,
    max_tier: int,
    containers: Sequence[Container],
    category: str,
    expected: Position,
) -> SlotRecommendation | None:
    """从期望点出发按曼哈顿距离逐层寻找最近的安全箱位。

    合法请求下现存箱位已保证不越界、不重复；本函数不改动它们，只判断
    待装箱与各现存箱的隔离关系（实际距离 >= 要求距离，临界相等即可）。
    候选逐层展开：只生成舱内壳层坐标，跳过已占用位置；同层按
    (排, 列, 层) 字典序取首个安全点。整舱无安全位置返回 None。

    安全性按类别做常数大小的邻域查表：要求距离至多为 3，候选对某类别
    不安全当且仅当其半径 (要求-1) <= 2 的邻域内存在该类别箱；偏移组合
    最多 25 个，与现存箱数量无关，故大尺寸稀疏舱段不会按箱数线性放大。
    """
    bounds = (max_row, max_col, max_tier)
    occupied: set[Position] = set()
    by_category: dict[str, set[Position]] = {}
    for existing in containers:
        position = _position(existing)
        occupied.add(position)
        by_category.setdefault(existing.category, set()).add(position)

    # 要求距离为 1 的类别对：候选不被占用即天然安全（唯一箱位最小距离为 1），
    # 无需再查半径 0 邻域；其余类别查表半径 1 或 2 的常数偏移集。
    forbidden_neighborhoods = [
        (positions, _NEIGHBOR_OFFSETS[required_distance(category, existing_category) - 1])
        for existing_category, positions in by_category.items()
        if required_distance(category, existing_category) > 1
    ]

    def is_safe(candidate: Position) -> bool:
        row, col, tier = candidate
        for positions, offsets in forbidden_neighborhoods:
            for dr, dc, dt in offsets:
                if (row + dr, col + dc, tier + dt) in positions:
                    return False
        return True

    max_radius = (
        max(expected[0] - 1, max_row - expected[0])
        + max(expected[1] - 1, max_col - expected[1])
        + max(expected[2] - 1, max_tier - expected[2])
    )

    for radius in range(max_radius + 1):
        for candidate in _shell_coordinates(expected, radius, bounds):
            if candidate not in occupied and is_safe(candidate):
                return SlotRecommendation(coordinate=candidate, search_distance=radius)

    return None
