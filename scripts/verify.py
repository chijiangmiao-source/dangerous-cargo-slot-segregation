#!/usr/bin/env python3
"""一次性验收脚本（verify 服务入口）。

对运行中的裁决服务执行一组固定验收用例，全部通过则进程以 0 退出，
否则输出失败明细并以 1 退出。用例覆盖：

* 健康地址 /healthz、/health、/ 均返回 200 且 status=ok（Docker 健康检查
  所用地址不得 404），且容器健康检查脚本对健康服务退出码为 0；
* 三类特殊规则的临界距离（恰好等于要求 -> 合规），且刻意使用排差/列差/
  层差组合，证明使用的是三轴曼哈顿距离而非欧氏距离、不忽略中间空位；
* 临界再低一格 -> 唯一冲突，返回证据由本脚本独立按曼哈顿距离复算；
* 多冲突时首项按 (较小箱位, 较大箱位) 排序，且与输入顺序无关；
* 全部非法输入（尺寸非正、越界、重复、未知类别、缺字段、多字段）整份 422；
* 无冲突时返回比较对数 C(n, 2)；
* 推荐箱位接口：最近可行壳层并列时按 (排,列,层) 字典序取首项且与输入
  顺序无关、小舱无解返回 no_safe_slot 且不含坐标、非法请求整份 422。

环境变量：
    VERIFY_BASE_URL  被测服务地址，默认 http://api:8000（Compose 网络）。
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

BASE_URL = os.environ.get("VERIFY_BASE_URL", "http://api:8000").rstrip("/")
ENDPOINT = f"{BASE_URL}/api/v1/adjudicate"
SLOT_ENDPOINT = f"{BASE_URL}/api/v1/recommend-slot"
HEALTHCHECK_SCRIPT = Path(__file__).resolve().parent / "healthcheck.py"
TIMEOUT = 10.0

failures: list[str] = []
checks_run = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global checks_run
    checks_run += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


def manhattan(p: dict[str, Any], q: dict[str, Any]) -> int:
    """验收侧独立复算：排差 + 列差 + 层差。"""
    return abs(p["row"] - q["row"]) + abs(p["col"] - q["col"]) + abs(p["tier"] - q["tier"])


def box(row: int, col: int, tier: int, category: str) -> dict[str, Any]:
    return {"row": row, "col": col, "tier": tier, "category": category}


def layout(containers: list[dict[str, Any]], **sizes: Any) -> dict[str, Any]:
    return {
        "max_row": sizes.get("max_row", 20),
        "max_col": sizes.get("max_col", 20),
        "max_tier": sizes.get("max_tier", 10),
        "containers": containers,
    }


def post(client: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    return client.post(ENDPOINT, json=body, timeout=TIMEOUT)


def expect_compliant(client: httpx.Client, name: str, containers: list[dict[str, Any]],
                     expected_pairs: int | None = None, **sizes: Any) -> None:
    response = post(client, layout(containers, **sizes))
    ok_status = response.status_code == 200
    check(f"{name} [HTTP 200]", ok_status, f"got {response.status_code} {response.text}")
    if not ok_status:
        return
    data = response.json()
    check(f"{name} [status=compliant]", data.get("status") == "compliant", str(data))
    if expected_pairs is not None:
        n = len(containers)
        check(f"{name} [pairs_compared={expected_pairs}]",
              data.get("pairs_compared") == expected_pairs == n * (n - 1) // 2,
              str(data))


def expect_conflict(client: httpx.Client, name: str, containers: list[dict[str, Any]],
                    required: int, actual: int | None = None) -> dict[str, Any] | None:
    response = post(client, layout(containers))
    check(f"{name} [HTTP 200]", response.status_code == 200,
          f"got {response.status_code} {response.text}")
    if response.status_code != 200:
        return None
    data = response.json()
    check(f"{name} [status=conflict]", data.get("status") == "conflict", str(data))
    evidence = data.get("first_conflict")
    check(f"{name} [证据字段完整]",
          evidence is not None and set(evidence) == {
              "container_a", "container_b", "actual_distance", "required_distance"},
          str(evidence))
    if not evidence:
        return None

    a, b = evidence["container_a"], evidence["container_b"]
    smaller = (a["row"], a["col"], a["tier"])
    larger = (b["row"], b["col"], b["tier"])
    check(f"{name} [较小箱位排在 container_a]", smaller < larger, f"{smaller} !< {larger}")

    recomputed = manhattan(a, b)
    check(f"{name} [实际距离可独立复算]",
          evidence["actual_distance"] == recomputed,
          f"服务返回 {evidence['actual_distance']}，复算 {recomputed}")
    check(f"{name} [要求距离={required}]",
          evidence["required_distance"] == required,
          f"got {evidence['required_distance']}")
    check(f"{name} [实际距离={actual if actual is not None else required - 1} 且低于要求]",
          evidence["actual_distance"] == (actual if actual is not None else required - 1)
          and evidence["actual_distance"] < evidence["required_distance"],
          str(evidence))
    return data


def expect_422(client: httpx.Client, name: str, body: Any) -> None:
    response = client.post(ENDPOINT, json=body, timeout=TIMEOUT)
    check(f"{name} [HTTP 422]", response.status_code == 422,
          f"got {response.status_code} {response.text}")
    if response.status_code == 422:
        data = response.json()
        check(f"{name} [无任何裁决字段]",
              "status" not in data and "first_conflict" not in data
              and "pairs_compared" not in data and "detail" in data,
              str(data))


# ---------------------------------------------------------------------------
# /api/v1/recommend-slot
# ---------------------------------------------------------------------------

def slot_body(containers: list[dict[str, Any]], category: str,
              expected: tuple[int, int, int], **sizes: Any) -> dict[str, Any]:
    return {
        "max_row": sizes.get("max_row", 20),
        "max_col": sizes.get("max_col", 20),
        "max_tier": sizes.get("max_tier", 10),
        "containers": containers,
        "category": category,
        "expected": {"row": expected[0], "col": expected[1], "tier": expected[2]},
    }


def post_slot(client: httpx.Client, body: dict[str, Any]) -> httpx.Response:
    return client.post(SLOT_ENDPOINT, json=body, timeout=TIMEOUT)


def slot_requirement(a: str, b: str) -> int:
    pair = frozenset((a, b))
    if pair == frozenset(("A", "B")):
        return 3
    if pair == frozenset(("A", "C")) or pair == frozenset(("B", "D")):
        return 2
    return 1


def verify_slot_checks(client: httpx.Client) -> None:
    print("\n[6] 推荐箱位：最近层并列取字典序首位（乱序不变）")

    # 期望点 (3,3,3) 被 A 占据，待装 B 要求距 A 为 3；半径 1、2 壳层全部
    # 不足，半径 3 壳层并列安全点的字典序首位为 (1,2,3)。
    center = (3, 3, 3)
    nearest_case = [box(3, 3, 3, "A")]
    response = post_slot(
        client, slot_body(nearest_case, "B", center,
                          max_row=5, max_col=5, max_tier=5))
    check("并列取首位 [HTTP 200]", response.status_code == 200,
          f"got {response.status_code} {response.text}")
    data = response.json() if response.status_code == 200 else {}
    coord = data.get("coordinate")
    check("并列取首位 [status=recommended 且字段完整]",
          data.get("status") == "recommended" and isinstance(coord, dict)
          and set(coord) == {"row", "col", "tier"}
          and set(data) == {"status", "coordinate", "search_distance"},
          str(data))
    expected_coord = {"row": 1, "col": 2, "tier": 3}
    check("并列取首位 [中选 (1,2,3)，搜索距离 3]",
          coord == expected_coord and data.get("search_distance") == 3, str(data))
    if coord:
        # 独立复算：搜索距离即到期望点的曼哈顿距离，且对全部现存箱满足隔离。
        dist_to_expected = manhattan(coord, {"row": center[0], "col": center[1],
                                             "tier": center[2]})
        check("并列取首位 [search_distance 可独立复算]",
              dist_to_expected == data["search_distance"] == 3, str(data))
        for existing in nearest_case:
            d = manhattan(coord, existing)
            check(f"并列取首位 [对现存 {existing['category']} 箱距离 {d} >= "
                  f"{slot_requirement('B', existing['category'])}]",
                  d >= slot_requirement("B", existing["category"]), str(data))

    # 验收侧暴力枚举复核：中选点确为（半径升序、同层字典序）首个安全点。
    occupied = {(c["row"], c["col"], c["tier"]) for c in nearest_case}
    safe_points: list[tuple[int, tuple[int, int, int]]] = []
    for p in itertools.product(range(1, 6), repeat=3):
        if p in occupied:
            continue
        if all(
            abs(p[0] - c["row"]) + abs(p[1] - c["col"]) + abs(p[2] - c["tier"])
            >= slot_requirement("B", c["category"])
            for c in nearest_case
        ):
            radius = abs(p[0] - 3) + abs(p[1] - 3) + abs(p[2] - 3)
            safe_points.append((radius, p))
    brute_first = sorted(safe_points)[0]
    check("并列取首位 [暴力枚举复核首个安全点]",
          (data.get("search_distance"), (coord or {}).get("row"),
           (coord or {}).get("col"), (coord or {}).get("tier"))
          == (brute_first[0], *brute_first[1]),
          f"{str(data)} vs {brute_first}")

    # 加入更多现存箱后，全部 24 种输入排列必须返回完全相同的结果。
    shuffled_case = [
        box(3, 3, 3, "A"), box(5, 5, 5, "D"),
        box(1, 1, 1, "C"), box(2, 4, 3, "A"),
    ]
    reference = None
    all_same = True
    for permutation in itertools.permutations(shuffled_case):
        shuffled_response = post_slot(
            client, slot_body(list(permutation), "B", center))
        if shuffled_response.status_code != 200:
            all_same = False
            break
        payload_data = shuffled_response.json()
        if reference is None:
            reference = payload_data
        elif payload_data != reference:
            all_same = False
            break
    check("乱序输入（24 种排列）返回完全相同的推荐", all_same and reference is not None,
          f"reference={reference}")

    print("\n[7] 推荐箱位：小舱无安全位置 -> no_safe_slot 且不含坐标")
    # 1x1x1：唯一位置被占。
    response = post_slot(client, slot_body([box(1, 1, 1, "A")], "B", (1, 1, 1),
                                           max_row=1, max_col=1, max_tier=1))
    check("满舱无解 [HTTP 200]", response.status_code == 200,
          f"got {response.status_code} {response.text}")
    if response.status_code == 200:
        data = response.json()
        check("满舱无解 [仅返回 status=no_safe_slot]",
              data == {"status": "no_safe_slot"}, str(data))

    # 1x1x2：A 占一格，另一格距 A 为 1 < 3，B 全舱无解；两种排列一致。
    blocked = [box(1, 1, 1, "A"), box(1, 1, 2, "C")]
    bodies = [
        slot_body(blocked, "B", (1, 1, 1), max_row=1, max_col=1, max_tier=2),
        slot_body(list(reversed(blocked)), "B", (1, 1, 2),
                  max_row=1, max_col=1, max_tier=2),
    ]
    responses = [post_slot(client, body) for body in bodies]
    ok = all(r.status_code == 200 and r.json() == {"status": "no_safe_slot"}
             for r in responses)
    check("隔离受限小舱无解，且与输入顺序/期望点无关", ok,
          " / ".join(r.text for r in responses))

    print("\n[8] 推荐箱位：非法请求整份 422，不产生部分结果")
    good = slot_body([], "A", (1, 1, 1))
    invalid_bodies: list[tuple[str, Any]] = [
        ("期望排超出上界", {**good, "expected": {"row": 21, "col": 1, "tier": 1}}),
        ("期望列取下界 0", {**good, "expected": {"row": 1, "col": 0, "tier": 1}}),
        ("期望层为负数", {**good, "expected": {"row": 1, "col": 1, "tier": -2}}),
        ("待装未知类别 E", {**good, "category": "E"}),
        ("待装小写类别 a", {**good, "category": "a"}),
        ("现存箱重复占位", slot_body([box(1, 1, 1, "A"), box(1, 1, 1, "B")],
                                     "A", (1, 1, 1))),
        ("现存箱越界", slot_body([box(21, 1, 1, "A")], "A", (1, 1, 1))),
        ("现存箱未知类别", slot_body([box(1, 1, 1, "Z")], "A", (1, 1, 1))),
        ("尺寸为字符串", {**good, "max_row": "20"}),
        ("待装类别为数字", {**good, "category": 1}),
        ("期望坐标为字符串", {**good, "expected": {"row": "1", "col": 1, "tier": 1}}),
        ("期望坐标含未声明字段",
         {**good, "expected": {"row": 1, "col": 1, "tier": 1, "x": 1}}),
        ("缺少 category 字段",
         {k: v for k, v in good.items() if k != "category"}),
        ("缺少 expected 字段",
         {k: v for k, v in good.items() if k != "expected"}),
        ("现存箱含未声明字段",
         slot_body([{**box(1, 1, 1, "A"), "weight": 9}], "A", (1, 1, 1))),
        ("顶层含未声明字段", {**good, "ship": "x"}),
        ("空 JSON", {}),
    ]
    for name, invalid_body in invalid_bodies:
        response = post_slot(client, invalid_body)
        ok_status = response.status_code == 422
        check(f"{name} [HTTP 422]", ok_status,
              f"got {response.status_code} {response.text}")
        if ok_status:
            data = response.json()
            check(f"{name} [无任何推荐字段]",
                  "status" not in data and "coordinate" not in data
                  and "search_distance" not in data and "detail" in data,
                  str(data))

    # 多问题同时存在（期望越界 + 现存重复 + 未知类别）仍只回一次 422。
    response = post_slot(client, {
        "max_row": 3, "max_col": 3, "max_tier": 3,
        "containers": [box(1, 1, 1, "A"), box(1, 1, 1, "B"), box(9, 9, 9, "E")],
        "category": "B",
        "expected": {"row": 9, "col": 1, "tier": 1},
    })
    check("多问题并存仍整份 422",
          response.status_code == 422 and "status" not in response.json(),
          f"got {response.status_code} {response.text}")


def main() -> int:
    print(f"验收目标: {ENDPOINT}")
    with httpx.Client() as client:
        # 0. 健康地址（Docker 健康检查与直接访问均不得 404）
        for path in ("/healthz", "/health", "/"):
            response = client.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
            ok = response.status_code == 200 and response.json().get("status") == "ok"
            check(f"GET {path} -> 200 且 status=ok", ok,
                  f"{response.status_code} {response.text}")

        # 以容器健康检查所用脚本对运行中的服务探测，退出码必须为 0。
        script_env = {**os.environ, "HEALTHCHECK_URL": f"{BASE_URL}/healthz"}
        result = subprocess.run(
            [sys.executable, str(HEALTHCHECK_SCRIPT)],
            env=script_env, capture_output=True, timeout=TIMEOUT,
        )
        check("scripts/healthcheck.py 对健康服务退出码为 0",
              result.returncode == 0, result.stderr.decode())

        print("\n[1] 临界距离必须判为合规（实际 == 要求）")
        # A-B 要求 3
        expect_compliant(client, "A-B 纯列差=3（中间两格空位不折叠距离）",
                         [box(1, 1, 1, "A"), box(1, 4, 1, "B")], expected_pairs=1)
        expect_compliant(client, "A-B 排+列+层各差 1（曼哈顿 3，非欧氏 sqrt3 判定）",
                         [box(1, 1, 1, "A"), box(2, 2, 2, "B")], expected_pairs=1)
        expect_compliant(client, "A-B 排差 1+层差 2=3",
                         [box(3, 5, 1, "A"), box(4, 5, 3, "B")], expected_pairs=1)
        # A-C 要求 2
        expect_compliant(client, "A-C 纯层差=2",
                         [box(3, 3, 2, "A"), box(3, 3, 4, "C")], expected_pairs=1)
        expect_compliant(client, "A-C 列差 1+层差 1=2",
                         [box(3, 3, 2, "A"), box(3, 4, 3, "C")], expected_pairs=1)
        # B-D 要求 2
        expect_compliant(client, "B-D 纯排差=2",
                         [box(4, 1, 1, "B"), box(6, 1, 1, "D")], expected_pairs=1)
        expect_compliant(client, "B-D 排差 1+列差 1=2",
                         [box(4, 1, 1, "B"), box(5, 2, 1, "D")], expected_pairs=1)
        # 其余组合（含同类）要求 1：相邻即合规
        expect_compliant(client, "A-A 相邻（同类要求 1）",
                         [box(1, 1, 1, "A"), box(1, 1, 2, "A")], expected_pairs=1)
        expect_compliant(client, "A-D 相邻（其余组合要求 1）",
                         [box(1, 1, 1, "A"), box(2, 1, 1, "D")], expected_pairs=1)
        expect_compliant(client, "C-D 相邻（其余组合要求 1）",
                         [box(1, 1, 1, "C"), box(1, 2, 1, "D")], expected_pairs=1)
        # 边界坐标恰好等于舱段容量
        expect_compliant(client, "坐标恰好取到上界 (3,4,2)",
                         [box(1, 1, 1, "A"), box(3, 4, 2, "B")], expected_pairs=1,
                         max_row=3, max_col=4, max_tier=2)

        print("\n[2] 临界再低一格 -> 唯一冲突，证据可复算")
        expect_conflict(client, "A-B 列差=2（再低一格）",
                        [box(1, 1, 1, "A"), box(1, 3, 1, "B")], required=3)
        expect_conflict(client, "A-B 排差 1+层差 1=2（层差排差叠加，看似分散仍冲突）",
                        [box(1, 5, 1, "A"), box(2, 5, 2, "B")], required=3, actual=2)
        expect_conflict(client, "A-C 纯层差=1",
                        [box(3, 3, 2, "A"), box(3, 3, 3, "C")], required=2)
        expect_conflict(client, "B-D 纯排差=1",
                        [box(4, 1, 1, "B"), box(5, 1, 1, "D")], required=2)
        # 要求为 1 的组合：坐标唯一使最小实际距离也是 1，故不存在冲突，无可触发项。

        print("\n[3] 多冲突首项排序唯一，且与输入顺序无关")
        many = [
            box(2, 5, 1, "A"), box(2, 5, 2, "C"),  # 冲突对，较小 (2,5,1)
            box(1, 1, 4, "B"), box(1, 1, 5, "D"),  # 冲突对，较小 (1,1,4)
            box(1, 1, 1, "A"), box(1, 1, 2, "C"),  # 冲突对，较小 (1,1,1) 应取为首项
        ]
        first = expect_conflict(client, "多冲突首项为 (1,1,1)-(1,1,2)", many,
                                required=2, actual=1)
        if first:
            ev = first["first_conflict"]
            ordered = (
                (ev["container_a"]["row"], ev["container_a"]["col"], ev["container_a"]["tier"]) == (1, 1, 1)
                and (ev["container_b"]["row"], ev["container_b"]["col"], ev["container_b"]["tier"]) == (1, 1, 2)
            )
            check("首项双方坐标符合 (较小, 较大) 字典序", ordered, str(ev))

        reversed_response = post(client, layout(list(reversed(many))))
        check("逆序输入返回完全相同的 JSON",
              reversed_response.status_code == 200 and reversed_response.json() == first,
              f"{reversed_response.text} != {first}")

        print("\n[4] 无冲突返回比较对数")
        expect_compliant(client, "空舱 0 对", [], expected_pairs=0)
        expect_compliant(client, "单箱 0 对", [box(1, 1, 1, "A")], expected_pairs=0)
        expect_compliant(client, "4 箱全部合规 -> C(4,2)=6 对",
                         [box(1, 1, 1, "A"), box(1, 4, 1, "B"),
                          box(10, 10, 5, "C"), box(9, 10, 5, "C")],
                         expected_pairs=6)

        print("\n[5] 非法输入整份 422，不产生部分裁决")
        good = layout([])
        expect_422(client, "max_row=0", {**good, "max_row": 0})
        expect_422(client, "max_col=-3", {**good, "max_col": -3})
        expect_422(client, "max_tier=0", {**good, "max_tier": 0})
        expect_422(client, "排坐标 0（下界越界）", layout([box(0, 1, 1, "A")]))
        expect_422(client, "层坐标超出上界", layout([box(1, 1, 11, "A")]))
        expect_422(client, "列坐标超出上界", layout([box(1, 21, 1, "A")]))
        expect_422(client, "未知类别 E", layout([box(1, 1, 1, "E")]))
        expect_422(client, "小写类别 a", layout([box(1, 1, 1, "a")]))
        expect_422(client, "重复箱位", layout([box(1, 1, 1, "A"), box(1, 1, 1, "B")]))
        expect_422(client, "越界+重复+未知类别同时出现",
                   layout([box(99, 1, 1, "A"), box(1, 1, 1, "E"),
                           box(1, 1, 1, "A"), box(1, 1, 1, "B")]))
        expect_422(client, "尺寸为字符串", {**good, "max_row": "10"})
        expect_422(client, "坐标为字符串", layout([box("1", 1, 1, "A")]))
        expect_422(client, "缺少 containers 字段",
                   {"max_row": 10, "max_col": 10, "max_tier": 10})
        expect_422(client, "箱位缺少 category",
                   layout([{"row": 1, "col": 1, "tier": 1}]))
        expect_422(client, "箱位含未声明字段",
                   layout([{**box(1, 1, 1, "A"), "weight": 99}]))
        expect_422(client, "顶层含未声明字段", {**good, "ship": "x"})
        expect_422(client, "空 JSON", {})

        verify_slot_checks(client)

    print(f"\n共执行 {checks_run} 项检查。")
    if failures:
        print(f"验收失败：{len(failures)} 项未通过。")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("验收通过：裁决临界合规/首冲突/比较对数与推荐箱位三组场景全部符合预期，"
          "非法输入整份 422。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
