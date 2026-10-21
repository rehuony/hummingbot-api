"""Durable commands for card-owned position groups. All responses are snapshots."""

from fastapi import APIRouter, Depends, Request

from models.position_groups import PositionGroupCommand, PositionGroupRequest

router = APIRouter(prefix="/executors/position-groups", tags=["Managed position groups"])


def service(request: Request):
    return request.app.state.position_group_service


@router.get("/capabilities")
async def capabilities(groups=Depends(service)):
    return groups.capabilities()


@router.post("/preview")
async def preview(body: PositionGroupRequest, groups=Depends(service)):
    return await groups.preview(body)


@router.put("/{group_id}")
async def create(group_id: str, body: PositionGroupRequest, groups=Depends(service)):
    return await groups.create(group_id, body)


@router.get("/{group_id}")
async def get(group_id: str, groups=Depends(service)):
    return await groups.get(group_id)


@router.post("/{group_id}/commands")
async def command(group_id: str, body: PositionGroupCommand, groups=Depends(service)):
    return await groups.command(group_id, body)
