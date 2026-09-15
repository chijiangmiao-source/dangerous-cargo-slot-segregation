"""POST /api/v1/recommend-slot 端到端测试。

自动化验收聚焦三组新场景（原裁决用例见 test_api.py / test_rules.py）：
1. 最近可行层存在并列时，乱序输入仍选中 (排,列,层) 字典序首位；
2. 小舱无安全位置 -> no_safe_slot 且响应不含坐标；
3. 非法请求整份 422（类型错误、越界、重复占位、未知类别）。
"""

import itertools

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

URL = "/api/v1/recommend-slot"


def box(row, col, tier, category):
    return {"row": row, "col": col, "tier": tier, "category": category}


def payload(**overrides):
    base = {
        "max_row": 5,
        "max_col": 5,
        "max_tier": 5,
        "containers": [],
        "category": "A",
        "expected": {"row": 1, "col": 1, "tier": 1},
    }
    base.update(overrides)
    return base


def recommend(containers, category, expected, **sizes):
    body = {
        "max_row": sizes.get("max_row", 5),
        "max_col": sizes.get("max_col", 5),
        "max_tier": sizes.get("max_tier", 5),
        "containers": containers,
        "category": category,
        "expected": {"row": expected[0], "col": expected[1], "tier": expected[2]},
    }
    return client.post(URL, json=body)


# ---------------------------------------------------------------------------
# 场景 1：最近层并列 -> 字典序首位；乱序输入结果一致
# ---------------------------------------------------------------------------

def test_expected_cell_free_is_picked_at_distance_zero():
    response = recommend([], "B", (3, 3, 3))
    assert response.status_code == 200
    assert response.json() == {
        "status": "recommended",
        "coordinate": {"row": 3, "col": 3, "tier": 3},
        "search_distance": 0,
    }


def test_tie_on_nearest_safe_layer_picks_lexicographic_first():
    # 期望点 (3,3,3) 被 A 占据：待装 B 与 A 要求距离 3。
    # 半径 1、2 的壳层全部距 A 不足 3；半径 3 的壳层上有多个并列安全点，
    # 字典序首位为 (1,2,3)（dr=-2,dc=-1,dt=0），搜索距离为 3。
    existing = [box(3, 3, 3, "A")]
    response = recommend(existing, "B", (3, 3, 3))
    assert response.status_code == 200
    assert response.json() == {
        "status": "recommended",
        "coordinate": {"row": 1, "col": 2, "tier": 3},
        "search_distance": 3,
    }


def test_shuffled_input_still_picks_lexicographic_first():
    containers = [
        box(3, 3, 3, "A"),
        box(5, 5, 5, "D"),
        box(1, 1, 1, "C"),
        box(2, 4, 3, "A"),
    ]
    responses = []
    for permutation in itertools.permutations(containers):
        response = recommend(list(permutation), "B", (3, 3, 3))
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "recommended"
        responses.append(data)

    first = responses[0]
    assert all(data == first for data in responses[1:])
    # (1,1,1) 被 C 占据；(1,2,3) 对 A(3,3,3) 距离恰为 3、对其余箱均满足要求。
    assert first["coordinate"] == {"row": 1, "col": 2, "tier": 3}
    assert first["search_distance"] == 3


def test_same_category_can_use_adjacent_shell_point():
    # 期望点被同类占据：相邻壳层距离 1 即满足同类要求 1，取字典序首位 (2,3,3)。
    response = recommend([box(3, 3, 3, "C")], "C", (3, 3, 3))
    assert response.status_code == 200
    assert response.json() == {
        "status": "recommended",
        "coordinate": {"row": 2, "col": 3, "tier": 3},
        "search_distance": 1,
    }


def test_candidate_must_satisfy_all_existing_containers():
    # 半径 1、2 的壳层对正中央的 A 全部不足要求 3。半径 3 壳层字典序首位
    # (1,2,3) 对 A(3,3,3) 恰好为 3，但对另一个 A(1,3,3) 只有 1 < 3，必须
    # 跳过；同层下一个对“全部”现存箱安全的字典序点为 (2,1,3)。
    containers = [box(3, 3, 3, "A"), box(1, 3, 3, "A")]
    response = recommend(containers, "B", (3, 3, 3))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "recommended"
    assert data["coordinate"] == {"row": 2, "col": 1, "tier": 3}
    assert data["search_distance"] == 3

    # 独立暴力枚举复核：中选点确为（半径升序、同层字典序）首个安全点。
    occupied = {(c["row"], c["col"], c["tier"]) for c in containers}
    candidates = sorted(
        (
            abs(p[0] - 3) + abs(p[1] - 3) + abs(p[2] - 3),
            p,
        )
        for p in itertools.product(range(1, 6), repeat=3)
        if p not in occupied
        and all(
            abs(p[0] - c["row"]) + abs(p[1] - c["col"]) + abs(p[2] - c["tier"])
            >= (3 if c["category"] == "A" else 1)
            for c in containers
        )
    )
    assert candidates[0] == (
        3,
        (data["coordinate"]["row"], data["coordinate"]["col"], data["coordinate"]["tier"]),
    )


def test_search_prefers_closer_safe_shell_over_farther():
    # A 占期望点；A-C 要求 2：半径 1 全部不足，半径 2 恰好合规。
    response = recommend([box(3, 3, 3, "A")], "C", (3, 3, 3))
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "recommended"
    assert data["search_distance"] == 2
    # 半径 2 壳层字典序首位：(1,3,3)。
    assert data["coordinate"] == {"row": 1, "col": 3, "tier": 3}


def test_boundary_coordinate_is_reachable():
    response = recommend([box(1, 1, 1, "B")], "A", (1, 1, 1),
                         max_row=1, max_col=4, max_tier=1)
    # A-B 要求 3：(1,1,1) 被占，(1,2,1) 距 1、(1,3,1) 距 2 均不足，
    # 取恰好距离 3 的舱段上界 (1,4,1)。
    assert response.status_code == 200
    assert response.json() == {
        "status": "recommended",
        "coordinate": {"row": 1, "col": 4, "tier": 1},
        "search_distance": 3,
    }


# ---------------------------------------------------------------------------
# 场景 2：全舱无可用位置 -> no_safe_slot，且不含坐标
# ---------------------------------------------------------------------------

def test_fully_occupied_hold_has_no_safe_slot():
    response = recommend([box(1, 1, 1, "A")], "B", (1, 1, 1),
                         max_row=1, max_col=1, max_tier=1)
    assert response.status_code == 200
    assert response.json() == {"status": "no_safe_slot"}


def test_small_hold_blocked_by_segregation_has_no_safe_slot():
    # 1x1x2：A 占一格，另一格与 A 距离 1 < 3（A-B），B 无安全位置。
    containers = [box(1, 1, 1, "A")]
    response = recommend(containers, "B", (1, 1, 2),
                         max_row=1, max_col=1, max_tier=2)
    assert response.status_code == 200
    data = response.json()
    assert data == {"status": "no_safe_slot"}
    assert "coordinate" not in data
    assert "search_distance" not in data
    assert "row" not in data


def test_no_safe_slot_response_independent_of_input_order():
    containers = [box(1, 1, 1, "A"), box(1, 1, 2, "C")]
    for permutation in itertools.permutations(containers):
        response = recommend(list(permutation), "B", (1, 1, 1),
                             max_row=1, max_col=1, max_tier=2)
        assert response.status_code == 200
        assert response.json() == {"status": "no_safe_slot"}


# ---------------------------------------------------------------------------
# 场景 3：非法请求整份 422
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_payload", [
    # 期望坐标越界（上界 / 下界）
    payload(expected={"row": 6, "col": 1, "tier": 1}),
    payload(expected={"row": 1, "col": 0, "tier": 1}),
    payload(expected={"row": 1, "col": 1, "tier": -1}),
    # 未知类别 / 小写
    payload(category="E"),
    payload(category="a"),
    # 现存箱位重复 / 越界 / 未知类别
    payload(containers=[box(1, 1, 1, "A"), box(1, 1, 1, "B")]),
    payload(containers=[box(6, 1, 1, "A")]),
    payload(containers=[box(1, 1, 1, "X")]),
    # 类型错误：尺寸、类别、坐标、现存箱坐标
    payload(max_row="5"),
    payload(category=1),
    payload(expected={"row": "3", "col": 3, "tier": 3}),
    payload(expected={"row": 3.0, "col": 3, "tier": 3}),
    payload(containers=[{"row": "1", "col": 1, "tier": 1, "category": "A"}]),
    # 缺字段 / 多字段
    {"max_row": 5, "max_col": 5, "max_tier": 5, "containers": [], "category": "A"},
    {"max_row": 5, "max_col": 5, "max_tier": 5, "containers": [],
     "expected": {"row": 1, "col": 1, "tier": 1}},
    payload(expected={"row": 1, "col": 1, "tier": 1, "extra": 1}),
    payload(containers=[{"row": 1, "col": 1, "tier": 1, "category": "A", "weight": 1}]),
    payload(unknown_top_field=1),
    {},
    None,
])
def test_invalid_payloads_return_422_without_partial_result(bad_payload):
    response = client.post(URL, json=bad_payload)
    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    assert "status" not in body
    assert "coordinate" not in body
    assert "search_distance" not in body


def test_multiple_problems_still_single_422():
    # 期望坐标越界 + 现存箱重复 + 未知类别同时出现：整份拒绝。
    response = client.post(URL, json={
        "max_row": 3, "max_col": 3, "max_tier": 3,
        "containers": [
            box(1, 1, 1, "A"), box(1, 1, 1, "B"), box(9, 9, 9, "E"),
        ],
        "category": "B",
        "expected": {"row": 9, "col": 1, "tier": 1},
    })
    assert response.status_code == 422
    assert "status" not in response.json()


def test_method_not_allowed_for_get_on_endpoint():
    assert client.get(URL).status_code == 405
