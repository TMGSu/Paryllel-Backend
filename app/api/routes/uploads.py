from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from app.core.auth import verify_token
from app.core.storage import (
    upload_file,
    delete_file,
    ALLOWED_IMAGE_TYPES,
    ALLOWED_VIDEO_TYPES,
    MAX_IMAGE_SIZE,
    MAX_VIDEO_SIZE,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF allowed")

    data = await file.read()

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10MB")

    url = upload_file(data, file.content_type, folder="avatars")
    return {"url": url}


@router.post("/image")
async def upload_image(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF allowed")

    data = await file.read()

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10MB")

    url = upload_file(data, file.content_type, folder="posts/images")
    return {"url": url}


@router.post("/banner")
async def upload_banner(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF allowed")

    data = await file.read()

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10MB")

    url = upload_file(data, file.content_type, folder="banners")
    return {"url": url}


@router.post("/community-icon")
async def upload_community_icon(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF allowed")

    data = await file.read()

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10MB")

    url = upload_file(data, file.content_type, folder="community/icons")
    return {"url": url}


@router.post("/community-banner")
async def upload_community_banner(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, WebP, or GIF allowed")

    data = await file.read()

    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail="Image must be under 10MB")

    url = upload_file(data, file.content_type, folder="community/banners")
    return {"url": url}


@router.post("/video")
async def upload_video(
    file: UploadFile = File(...),
    payload: dict = Depends(verify_token),
):
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(status_code=400, detail="Only MP4, WebM, or MOV allowed")

    data = await file.read()

    if len(data) > MAX_VIDEO_SIZE:
        raise HTTPException(status_code=400, detail="Video must be under 200MB")

    url = upload_file(data, file.content_type, folder="posts/videos")
    return {"url": url}