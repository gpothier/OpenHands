// sandbox-service.api.ts
// This file contains API methods for /api/v1/sandboxes endpoints.

import { openHands } from "../open-hands-axios";
import type {
  V1SandboxInfo,
  V1SandboxSpecInfo,
  V1SandboxSpecInfoPage,
} from "./sandbox-service.types";

export class SandboxService {
  /**
   * Pause a V1 sandbox
   * Calls the /api/v1/sandboxes/{id}/pause endpoint
   */
  static async pauseSandbox(sandboxId: string): Promise<{ success: boolean }> {
    const { data } = await openHands.post<{ success: boolean }>(
      `/api/v1/sandboxes/${sandboxId}/pause`,
      {},
    );
    return data;
  }

  /**
   * Resume a V1 sandbox
   * Calls the /api/v1/sandboxes/{id}/resume endpoint
   */
  static async resumeSandbox(sandboxId: string): Promise<{ success: boolean }> {
    const { data } = await openHands.post<{ success: boolean }>(
      `/api/v1/sandboxes/${sandboxId}/resume`,
      {},
    );
    return data;
  }

  /**
   * Batch get V1 sandboxes by their IDs
   * Returns null for any missing sandboxes
   */
  static async batchGetSandboxes(
    ids: string[],
  ): Promise<(V1SandboxInfo | null)[]> {
    if (ids.length === 0) {
      return [];
    }
    if (ids.length > 100) {
      throw new Error("Cannot request more than 100 sandboxes at once");
    }
    const params = new URLSearchParams();
    ids.forEach((id) => params.append("id", id));
    const { data } = await openHands.get<(V1SandboxInfo | null)[]>(
      `/api/v1/sandboxes?${params.toString()}`,
    );
    return data;
  }

  /**
   * Search sandbox specs (templates for creating sandboxes)
   * Calls the /api/v1/sandbox-specs/search endpoint
   */
  static async searchSandboxSpecs(
    pageId?: string,
    limit: number = 100,
  ): Promise<V1SandboxSpecInfoPage> {
    const params = new URLSearchParams();
    if (pageId) {
      params.append("page_id", pageId);
    }
    params.append("limit", limit.toString());
    const { data } = await openHands.get<V1SandboxSpecInfoPage>(
      `/api/v1/sandbox-specs/search?${params.toString()}`,
    );
    return data;
  }

  /**
   * Batch get sandbox specs by their IDs
   * Returns null for any missing specs
   */
  static async batchGetSandboxSpecs(
    ids: string[],
  ): Promise<(V1SandboxSpecInfo | null)[]> {
    if (ids.length === 0) {
      return [];
    }
    if (ids.length > 100) {
      throw new Error("Cannot request more than 100 sandbox specs at once");
    }
    const params = new URLSearchParams();
    ids.forEach((id) => params.append("id", id));
    const { data } = await openHands.get<(V1SandboxSpecInfo | null)[]>(
      `/api/v1/sandbox-specs?${params.toString()}`,
    );
    return data;
  }

  /**
   * Start a new sandbox with a specific sandbox spec
   * Calls the POST /api/v1/sandboxes endpoint
   */
  static async startSandbox(sandboxSpecId?: string): Promise<V1SandboxInfo> {
    const params = new URLSearchParams();
    if (sandboxSpecId) {
      params.append("sandbox_spec_id", sandboxSpecId);
    }
    const { data } = await openHands.post<V1SandboxInfo>(
      `/api/v1/sandboxes${params.toString() ? `?${params.toString()}` : ""}`,
      {},
    );
    return data;
  }
}
