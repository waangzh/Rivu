"""系统诊断与受限的本地维护 API。"""

import asyncio
import os
import json
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, HTTPException, status

from app.api.reviews import get_review_service
from app.llm.registry import ProviderRegistry
from app.llm.providers import LLMProviderError
from app.core.config import persist_editable_settings, settings
from app.core.database import schema_is_current
from app.models.operations import (
    ModelCatalogResponse,
    ProviderRegistryResponse,
    ModelConnectionRequest,
    ModelConnectionResponse,
    PublicationCapability,
    SystemDiagnostics,
    SystemSettingsUpdate,
    WorkspaceCleanupPreview,
    WorkspaceCleanupRequest,
    WorkspaceCleanupResponse,
)
from app.services.workspaces.maintenance_service import workspace_cleanup_lock, workspace_ttl_seconds
from app.llm.model_catalog import ModelCatalogError, fetch_model_catalog
from app.review.configuration import capabilities
from app.review.observability import diagnostics as tracing_diagnostics
from app.tools.workspace_cleanup import inspect_orphaned_workspaces, reap_orphaned_workspaces


router = APIRouter(prefix="/system", tags=["system"])

@router.get("/review-capabilities")
async def get_review_capabilities():
    return capabilities()


@router.get("/publication-capability", response_model=PublicationCapability)
async def get_publication_capability() -> PublicationCapability:
    """发布页进入前探测；未启用时前端只提供复制评论路径。"""
    enabled = settings.rivu_github_publish_enabled
    return PublicationCapability(enabled=enabled, mode="publish" if enabled else "copy_only")


def _active_workspace_paths() -> tuple[object, ...]:
    return get_review_service().active_workspace_paths()


@router.get("/models", response_model=ModelCatalogResponse)
async def get_available_models() -> ModelCatalogResponse:
    """使用后端配置的凭证查询当前 Provider 实际开放的模型。"""
    try:
        if settings.rivu_model_providers:
            registry = ProviderRegistry(settings)
            provider = registry.resolve()
            return ModelCatalogResponse(provider=registry.default_id,
                default_model=provider._default_model,
                models=[{"id": profile.model_id, "profile": profile}
                        for profile in provider.config.models])
        return await fetch_model_catalog(
            provider=settings.rivu_provider,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            default_model=settings.rivu_model,
        )
    except ModelCatalogError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/providers", response_model=ProviderRegistryResponse)
async def get_model_providers():
    registry = ProviderRegistry(settings)
    return ProviderRegistryResponse(default_provider=registry.default_id, providers=[
        {"id": identifier, "config": config,
         "credentials_configured": bool(registry.resolve(identifier)._api_key)}
        for identifier, config in registry.configs().items()])


@router.post("/providers/{provider_id}/test", response_model=ModelConnectionResponse)
async def test_model_connection(provider_id: str, value: ModelConnectionRequest):
    from pydantic import BaseModel
    from langchain_core.messages import HumanMessage

    class ConnectionCheck(BaseModel):
        connected: bool

    try:
        provider = ProviderRegistry(settings).resolve(provider_id)
        profile = provider.profile(value.model)
        model = await asyncio.to_thread(provider.agent_model, value.model,
                                       output_tokens=min(profile.max_output_tokens, 4096))
        response = await asyncio.wait_for(model.bind_tools([ConnectionCheck]).ainvoke([
            HumanMessage(content="Call ConnectionCheck with connected=true. Do not use other tools.")
        ]), timeout=settings.rivu_model_request_timeout_seconds)
        connected = any(call["name"] == "ConnectionCheck" and call["args"] == {"connected": True}
                        for call in response.tool_calls)
        return ModelConnectionResponse(status="connected" if connected else "tools_not_returned",
                                       usage=response.usage_metadata)
    except LLMProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="模型连接测试失败") from exc


@router.get("/diagnostics", response_model=SystemDiagnostics)
async def get_system_diagnostics() -> SystemDiagnostics:
    try:
        app_version = version("rivu-backend")
    except PackageNotFoundError:
        app_version = "0.1.0"

    service = get_review_service()
    provider = ProviderRegistry(settings).resolve()
    worker_running = service.worker_running()
    artifact_path = settings.rivu_artifact_dir.resolve()
    writable_target = artifact_path if artifact_path.exists() else artifact_path.parent

    return SystemDiagnostics(
        version=app_version,
        provider=provider.provider_id,
        default_model=provider._default_model,
        model_base_url=provider._base_url,
        database_schema_current=schema_is_current(),
        worker_status="running" if worker_running else "idle",
        artifact_directory_writable=writable_target.exists() and os.access(writable_target, os.W_OK),
        langsmith_enabled=settings.rivu_langsmith_tracing,
        langsmith=tracing_diagnostics(),
        retention_days=settings.rivu_retention_days,
        configured_secrets={
            "github": bool(settings.github_token),
            "llm_provider": bool(provider._api_key),
            "langsmith": bool(settings.langsmith_api_key),
            "github_webhook": bool(settings.rivu_github_webhook_secret),
        },

    )


@router.patch("/settings", response_model=SystemDiagnostics)
async def update_system_settings(value: SystemSettingsUpdate) -> SystemDiagnostics:
    """更新本地单用户可编辑配置；密钥和执行安全边界不在此接口内。"""
    updates: dict[str, object] = {}
    mapping = {
        "retention_days": "rivu_retention_days",
        "model_base_url": "openai_base_url",
        "model_name": "rivu_model",
    }
    changes: dict[str, object] = {}
    if settings.rivu_model_providers and (value.model_base_url or value.model_name):
        raise HTTPException(status_code=400, detail="请在 Provider 配置中修改模型与端点")
    for field, setting_name in mapping.items():
        item = getattr(value, field)
        if item is None:
            continue
        changes[setting_name] = item
        env_key = {
            "rivu_retention_days": "RIVU_RETENTION_DAYS",
            "openai_base_url": "OPENAI_BASE_URL",
            "rivu_model": "RIVU_MODEL",
        }[setting_name]
        updates[env_key] = item
    if value.model_providers is not None:
        changes["rivu_model_providers"] = value.model_providers
        updates["RIVU_MODEL_PROVIDERS"] = json.dumps(
            {key: config.model_dump(mode="json") for key, config in value.model_providers.items()},
            ensure_ascii=False)
    if value.default_provider is not None:
        changes["rivu_default_provider"] = value.default_provider
        updates["RIVU_DEFAULT_PROVIDER"] = value.default_provider
    if updates:
        try:
            type(settings).model_validate({**settings.model_dump(), **changes})
            await asyncio.to_thread(persist_editable_settings, updates)
            for setting_name, item in changes.items():
                setattr(settings, setting_name, item)
            get_review_service().reload_provider(ProviderRegistry(settings))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="保存本地配置失败") from exc
    return await get_system_diagnostics()


@router.get("/workspaces/cleanup/preview", response_model=WorkspaceCleanupPreview)
async def preview_workspace_cleanup() -> WorkspaceCleanupPreview:
    """只读扫描达到安全 TTL 的非活动工作目录。"""
    ttl_seconds = workspace_ttl_seconds()
    async with workspace_cleanup_lock:
        result = await asyncio.to_thread(
            inspect_orphaned_workspaces,
            workdir=settings.rivu_workdir,
            older_than_seconds=ttl_seconds,
            active_paths=_active_workspace_paths(),
        )
    return WorkspaceCleanupPreview(ttl_seconds=ttl_seconds, **result.__dict__)


@router.post("/workspaces/cleanup", response_model=WorkspaceCleanupResponse)
async def cleanup_expired_workspaces(
    request: WorkspaceCleanupRequest,
) -> WorkspaceCleanupResponse:
    """经显式确认后，清理达到安全 TTL 的非活动工作目录。"""
    if not request.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace cleanup requires explicit confirmation",
        )

    ttl_seconds = workspace_ttl_seconds()
    async with workspace_cleanup_lock:
        result = await asyncio.to_thread(
            reap_orphaned_workspaces,
            workdir=settings.rivu_workdir,
            older_than_seconds=ttl_seconds,
            active_paths=_active_workspace_paths(),
        )
    return WorkspaceCleanupResponse(ttl_seconds=ttl_seconds, **result.__dict__)
