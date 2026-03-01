import { request } from "../request";
import type {
  ProviderInfo,
  ProviderConfigRequest,
  ActiveModelsInfo,
  ModelSlotConfig,
  ModelSlotRequest,
  CreateCustomProviderRequest,
  AddModelRequest,
} from "../types";

export const providerApi = {
  listProviders: () => request<ProviderInfo[]>("/models"),

  configureProvider: (providerId: string, body: ProviderConfigRequest) =>
    request<ProviderInfo>(`/models/${encodeURIComponent(providerId)}/config`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getActiveModels: () => request<ActiveModelsInfo>("/models/active"),

  setActiveLlm: (body: ModelSlotRequest) =>
    request<ActiveModelsInfo>("/models/active", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  /* ---- Custom provider CRUD ---- */

  createCustomProvider: (body: CreateCustomProviderRequest) =>
    request<ProviderInfo>("/models/custom-providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteCustomProvider: (providerId: string) =>
    request<ProviderInfo[]>(
      `/models/custom-providers/${encodeURIComponent(providerId)}`,
      { method: "DELETE" },
    ),

  /* ---- Model CRUD (works for both built-in and custom providers) ---- */

  addModel: (providerId: string, body: AddModelRequest) =>
    request<ProviderInfo>(`/models/${encodeURIComponent(providerId)}/models`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  removeModel: (providerId: string, modelId: string) =>
    request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}`,
      { method: "DELETE" },
    ),

  /* ---- Fallback LLM CRUD ---- */

  getFallbacks: () => request<ModelSlotConfig[]>("/models/fallbacks"),

  addFallback: (body: ModelSlotRequest) =>
    request<ModelSlotConfig[]>("/models/fallbacks", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setFallbacks: (body: ModelSlotRequest[]) =>
    request<ModelSlotConfig[]>("/models/fallbacks", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  removeFallback: (index: number) =>
    request<ModelSlotConfig[]>(`/models/fallbacks/${index}`, {
      method: "DELETE",
    }),
};
