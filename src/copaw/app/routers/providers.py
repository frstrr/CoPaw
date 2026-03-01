# -*- coding: utf-8 -*-
"""API routes for LLM providers and models."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException, Path
from pydantic import BaseModel, Field

from ...providers import (
    ActiveModelsInfo,
    ModelInfo,
    ModelSlotConfig,
    ProviderDefinition,
    ProviderInfo,
    ProvidersData,
    add_fallback_llm,
    add_model,
    create_custom_provider,
    delete_custom_provider,
    get_provider,
    list_providers,
    load_providers_json,
    mask_api_key,
    remove_fallback_llm,
    remove_model,
    set_active_llm,
    set_fallback_llms,
    update_provider_settings,
)

router = APIRouter(prefix="/models", tags=["models"])


class ProviderConfigRequest(BaseModel):
    api_key: Optional[str] = Field(default=None)
    base_url: Optional[str] = Field(default=None)


class ModelSlotRequest(BaseModel):
    provider_id: str = Field(..., description="Provider to use")
    model: str = Field(..., description="Model identifier")


class CreateCustomProviderRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)
    default_base_url: str = Field(default="")
    api_key_prefix: str = Field(default="")
    models: List[ModelInfo] = Field(default_factory=list)


class AddModelRequest(BaseModel):
    id: str = Field(...)
    name: str = Field(...)


def _build_provider_info(
    provider: ProviderDefinition,
    data: ProvidersData,
) -> ProviderInfo:
    if provider.is_local:
        return ProviderInfo(
            id=provider.id,
            name=provider.name,
            api_key_prefix="",
            models=list(provider.models),
            extra_models=[],
            is_custom=False,
            is_local=True,
            has_api_key=True,  # always "configured"
            current_api_key="",
            current_base_url="",
        )

    cur_base_url, cur_api_key = data.get_credentials(provider.id)
    configured = data.is_configured(provider)

    settings = data.providers.get(provider.id)
    extra = (
        list(settings.extra_models)
        if settings and not provider.is_custom
        else []
    )

    return ProviderInfo(
        id=provider.id,
        name=provider.name,
        api_key_prefix=provider.api_key_prefix,
        models=list(provider.models) + extra,
        extra_models=extra,
        is_custom=provider.is_custom,
        is_local=provider.is_local,
        has_api_key=configured,
        current_api_key=mask_api_key(cur_api_key),
        current_base_url=cur_base_url,
    )


@router.get(
    "",
    response_model=List[ProviderInfo],
    summary="List all providers",
)
async def list_all_providers() -> List[ProviderInfo]:
    data = load_providers_json()
    return [_build_provider_info(p, data) for p in list_providers()]


@router.put(
    "/{provider_id}/config",
    response_model=ProviderInfo,
    summary="Configure a provider",
)
async def configure_provider(
    provider_id: str = Path(...),
    body: ProviderConfigRequest = Body(...),
) -> ProviderInfo:
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(404, detail=f"Provider '{provider_id}' not found")

    base_url = body.base_url if provider.is_custom else None
    data = update_provider_settings(
        provider_id,
        api_key=body.api_key,
        base_url=base_url,
    )
    return _build_provider_info(provider, data)


@router.post(
    "/custom-providers",
    response_model=ProviderInfo,
    summary="Create a custom provider",
    status_code=201,
)
async def create_custom_provider_endpoint(
    body: CreateCustomProviderRequest = Body(...),
) -> ProviderInfo:
    try:
        data = create_custom_provider(
            provider_id=body.id,
            name=body.name,
            default_base_url=body.default_base_url,
            api_key_prefix=body.api_key_prefix,
            models=body.models,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider = get_provider(body.id)
    assert provider is not None
    return _build_provider_info(provider, data)


@router.delete(
    "/custom-providers/{provider_id}",
    response_model=List[ProviderInfo],
    summary="Delete a custom provider",
)
async def delete_custom_provider_endpoint(
    provider_id: str = Path(...),
) -> List[ProviderInfo]:
    try:
        data = delete_custom_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [_build_provider_info(p, data) for p in list_providers()]


@router.post(
    "/{provider_id}/models",
    response_model=ProviderInfo,
    summary="Add a model to a provider",
    status_code=201,
)
async def add_model_endpoint(
    provider_id: str = Path(...),
    body: AddModelRequest = Body(...),
) -> ProviderInfo:
    try:
        data = add_model(provider_id, ModelInfo(id=body.id, name=body.name))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    provider = get_provider(provider_id)
    assert provider is not None
    return _build_provider_info(provider, data)


@router.delete(
    "/{provider_id}/models/{model_id:path}",
    response_model=ProviderInfo,
    summary="Remove a model from a provider",
)
async def remove_model_endpoint(
    provider_id: str = Path(...),
    model_id: str = Path(...),
) -> ProviderInfo:
    try:
        data = remove_model(provider_id, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    provider = get_provider(provider_id)
    assert provider is not None
    return _build_provider_info(provider, data)


@router.get(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Get active LLM",
)
async def get_active_models() -> ActiveModelsInfo:
    data = load_providers_json()
    return ActiveModelsInfo(
        active_llm=data.active_llm,
        fallback_llms=data.fallback_llms,
    )


# ---- Fallback LLM CRUD ----


@router.get(
    "/fallbacks",
    response_model=List[ModelSlotConfig],
    summary="Get fallback LLM list",
)
async def get_fallbacks() -> List[ModelSlotConfig]:
    data = load_providers_json()
    return data.fallback_llms


@router.post(
    "/fallbacks",
    response_model=List[ModelSlotConfig],
    summary="Add a fallback LLM",
    status_code=201,
)
async def add_fallback(
    body: ModelSlotRequest = Body(...),
) -> List[ModelSlotConfig]:
    provider = get_provider(body.provider_id)
    if provider is None:
        raise HTTPException(
            404, detail=f"Provider '{body.provider_id}' not found"
        )
    if not body.model:
        raise HTTPException(400, detail="model must not be empty.")
    try:
        data = add_fallback_llm(body.provider_id, body.model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data.fallback_llms


@router.put(
    "/fallbacks",
    response_model=List[ModelSlotConfig],
    summary="Replace the fallback LLM list (used for reordering)",
)
async def replace_fallbacks(
    body: List[ModelSlotRequest] = Body(...),
) -> List[ModelSlotConfig]:
    slots = [ModelSlotConfig(provider_id=r.provider_id, model=r.model) for r in body]
    data = set_fallback_llms(slots)
    return data.fallback_llms


@router.delete(
    "/fallbacks/{index}",
    response_model=List[ModelSlotConfig],
    summary="Remove a fallback LLM by index",
)
async def remove_fallback(
    index: int = Path(..., description="0-based index of the fallback to remove"),
) -> List[ModelSlotConfig]:
    try:
        data = remove_fallback_llm(index)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return data.fallback_llms


@router.put(
    "/active",
    response_model=ActiveModelsInfo,
    summary="Set active LLM",
)
async def set_active_model(
    body: ModelSlotRequest = Body(...),
) -> ActiveModelsInfo:
    provider = get_provider(body.provider_id)
    if provider is None:
        raise HTTPException(
            404,
            detail=f"Provider '{body.provider_id}' not found",
        )

    data = load_providers_json()
    if not data.is_configured(provider):
        if provider.is_custom:
            msg = (
                f"Provider '{provider.name}' has no base_url configured. "
                "Please configure the base URL first."
            )
        else:
            msg = (
                f"Provider '{provider.name}' has no API key configured. "
                "Please configure the API key first."
            )
        raise HTTPException(status_code=400, detail=msg)

    if not body.model:
        raise HTTPException(status_code=400, detail="Model is required.")

    data = set_active_llm(body.provider_id, body.model)
    return ActiveModelsInfo(active_llm=data.active_llm)
