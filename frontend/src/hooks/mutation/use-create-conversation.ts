import { useMutation, useQueryClient } from "@tanstack/react-query";
import V1ConversationService from "#/api/conversation-service/v1-conversation-service.api";
import { PluginSpec } from "#/api/conversation-service/v1-conversation-service.types";
import { SuggestedTask } from "#/utils/types";
import { Provider } from "#/types/settings";
import { useTracking } from "#/hooks/use-tracking";

interface CreateConversationVariables {
  query?: string;
  repository?: {
    name: string;
    gitProvider: Provider;
    branch?: string;
  };
  suggestedTask?: SuggestedTask;
  conversationInstructions?: string;
  parentConversationId?: string;
  agentType?: "default" | "plan";
  plugins?: PluginSpec[];
  sandboxSpecId?: string; // Sandbox spec for selecting Docker/Firecracker
  fcStorageSizeGb?: number; // Storage size in GB for Firecracker sandboxes
  fcRamSizeMib?: number; // RAM size in MiB for Firecracker sandboxes
  discoverAllRepos?: boolean; // Discover skills from all git repos in workspace
  skillsDiscoveryDepth?: number; // Max depth for git repo discovery
}

// Response type for V1 conversations
interface CreateConversationResponse {
  conversation_id: string;
  session_api_key: string | null;
  url: string | null;
  v1_task_id?: string;
  is_v1?: boolean;
}

export const useCreateConversation = () => {
  const queryClient = useQueryClient();
  const { trackConversationCreated } = useTracking();

  return useMutation({
    mutationKey: ["create-conversation"],
    mutationFn: async (
      variables: CreateConversationVariables,
    ): Promise<CreateConversationResponse> => {
      // Debug: log incoming variables
      console.log("useCreateConversation mutationFn: variables =", variables);

      const {
        query,
        repository,
        suggestedTask,
        conversationInstructions,
        parentConversationId,
        agentType,
        plugins,
        sandboxSpecId,
        fcStorageSizeGb,
        fcRamSizeMib,
        discoverAllRepos,
        skillsDiscoveryDepth,
      } = variables;

      // Debug: log extracted sandboxSpecId, fcStorageSizeGb, and fcRamSizeMib
      console.log(
        "useCreateConversation mutationFn: sandboxSpecId =",
        sandboxSpecId,
        ", fcStorageSizeGb =",
        fcStorageSizeGb,
        ", fcRamSizeMib =",
        fcRamSizeMib,
        ", discoverAllRepos =",
        discoverAllRepos,
        ", skillsDiscoveryDepth =",
        skillsDiscoveryDepth,
      );

      // Use V1 API - creates a conversation start task
      const startTask = await V1ConversationService.createConversation(
        repository?.name,
        repository?.gitProvider,
        query,
        repository?.branch,
        conversationInstructions,
        suggestedTask,
        undefined, // trigger - set by backend when applicable
        parentConversationId,
        agentType,
        plugins,
        undefined, // sandbox_id - not reusing an existing sandbox
        undefined, // llm_model - use default
        sandboxSpecId,
        fcStorageSizeGb,
        fcRamSizeMib,
        discoverAllRepos,
        skillsDiscoveryDepth,
      );

      // Return a special task ID that the frontend will recognize
      // Format: "task-{uuid}" so the conversation screen can poll the task
      // Once the task is ready, it will navigate to the actual conversation ID
      return {
        conversation_id: `task-${startTask.id}`,
        session_api_key: null,
        url: startTask.agent_server_url,
        v1_task_id: startTask.id,
        is_v1: true,
      };
    },
    onSuccess: async (_, { repository }) => {
      trackConversationCreated({
        hasRepository: !!repository,
      });

      queryClient.removeQueries({
        queryKey: ["user", "conversations"],
      });
    },
  });
};
