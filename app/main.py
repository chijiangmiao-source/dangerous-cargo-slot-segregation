"""FastAPI 入口。"""

from fastapi import FastAPI

from .models import (
    AdjudicationRequest,
    AdjudicationResponse,
    ConflictEvidence,
)
from .rules import adjudicate

app = FastAPI(
    title="舱段危险品箱隔离裁决服务",
    version="1.0.0",
    description="对同一舱段内的危险品箱位做强制隔离距离裁决（曼哈顿距离）。",
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/adjudicate",
    response_model=AdjudicationResponse,
    tags=["adjudication"],
    summary="裁决一批箱位是否满足隔离规则",
)
def adjudicate_endpoint(
    request: AdjudicationRequest,
) -> dict[str, object]:
    result = adjudicate(request.containers)

    if result.first_conflict is None:
        return {"status": "compliant", "pairs_compared": result.pairs_compared}

    conflict = result.first_conflict
    return {
        "status": "conflict",
        "first_conflict": ConflictEvidence(
            container_a=conflict.smaller,
            container_b=conflict.larger,
            actual_distance=conflict.actual_distance,
            required_distance=conflict.required_distance,
        ),
    }
