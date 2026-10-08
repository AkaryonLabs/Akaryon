import base64
import binascii
from io import BytesIO
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from PIL import Image, UnidentifiedImageError

from akaryon.core.exceptions import AkaryonError

router = APIRouter(prefix="/images", tags=["images"])
MAX_GENERATED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_GENERATED_PIXELS = 40_000_000


class ImageGenerationInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024"
    quality: Literal["low", "medium", "high"] = "medium"

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Prompt cannot be blank")
        return value


@router.post("/generate")
def generate_image(body: ImageGenerationInput, request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    provider = request.app.state.model_router.providers.get("openai")
    if provider is None or "image_generation" not in getattr(provider, "capabilities", frozenset()):
        raise HTTPException(status_code=503,
                            detail="Image generation requires AKARYON_OPENAI_API_KEY and the OpenAI provider")
    estimate = settings.image_generation_cost_reservation_usd
    if (settings.max_estimated_cost_per_task_usd is not None and
            estimate > settings.max_estimated_cost_per_task_usd):
        raise HTTPException(status_code=422,
                            detail="Image generation's configured cost reservation exceeds the per-task cost cap")
    reservation_id = None
    monthly_limit = settings.max_estimated_cost_per_month_usd
    if monthly_limit is not None:
        ledger = request.app.state.usage_ledger
        if not ledger:
            raise HTTPException(status_code=503, detail="Monthly usage ledger is unavailable")
        operation_id = str(uuid4())
        try:
            reservation_id = ledger.reserve_fixed_cost(
                operation_id, operation_id, "openai", request.app.state.model_router.openai_image_model,
                estimate, monthly_limit)
        except ValueError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
    try:
        try:
            image_base64 = provider.generate_image(prompt=body.prompt,
                                                   model=request.app.state.model_router.openai_image_model,
                                                   size=body.size, quality=body.quality)
        except AkaryonError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        image_bytes = base64.b64decode(image_base64, validate=True)
        if not image_bytes or len(image_bytes) > MAX_GENERATED_IMAGE_BYTES:
            raise ValueError("generated image exceeds the response size limit")
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format != "PNG" or image.width * image.height > MAX_GENERATED_PIXELS:
                raise ValueError("generated image has an unsupported format or dimensions")
            image.verify()
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="OpenAI returned an invalid or oversized image") from exc
    finally:
        if reservation_id:
            request.app.state.usage_ledger.settle_estimate(reservation_id)
    return {"model": request.app.state.model_router.openai_image_model,
            "mime_type": "image/png", "estimated_cost_usd": estimate,
            "image_data_url": "data:image/png;base64," + image_base64}
