import { useQuery } from "@tanstack/react-query";
import { SandboxService } from "#/api/sandbox-service/sandbox-service.api";

/**
 * Hook to fetch available sandbox specs (templates for creating sandboxes).
 * These specs define different sandbox types like Docker containers or VMs.
 */
export const useSandboxSpecs = () =>
  useQuery({
    queryKey: ["sandbox-specs"],
    queryFn: () => SandboxService.searchSandboxSpecs(),
    staleTime: 1000 * 60 * 10, // 10 minutes - specs don't change often
    gcTime: 1000 * 60 * 30, // 30 minutes
  });
