import { useMutation, useQueryClient } from "@tanstack/react-query";
import V1ConversationService from "#/api/conversation-service/v1-conversation-service.api";
import { V1AppConversation } from "#/api/conversation-service/v1-conversation-service.types";

export const useUpdateConversation = () => {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: (variables: { conversationId: string; newTitle: string }) =>
      V1ConversationService.updateConversationTitle(
        variables.conversationId,
        variables.newTitle,
      ),
    onMutate: async (variables) => {
      // Cancel any outgoing refetches to prevent them from overwriting optimistic update
      await queryClient.cancelQueries({ queryKey: ["user", "conversations"] });
      await queryClient.cancelQueries({
        queryKey: ["user", "conversation", variables.conversationId],
      });

      const previousConversations = queryClient.getQueryData([
        "user",
        "conversations",
      ]);
      const previousConversation =
        queryClient.getQueryData<V1AppConversation | null>([
          "user",
          "conversation",
          variables.conversationId,
        ]);

      queryClient.setQueryData(
        ["user", "conversations"],
        (old: { id: string; title: string }[] | undefined) =>
          old?.map((conv) =>
            conv.id === variables.conversationId
              ? { ...conv, title: variables.newTitle }
              : conv,
          ),
      );

      // Also optimistically update the active conversation query
      queryClient.setQueryData(
        ["user", "conversation", variables.conversationId],
        (old: V1AppConversation | null | undefined) =>
          old ? { ...old, title: variables.newTitle } : old,
      );

      return { previousConversations, previousConversation };
    },
    onError: (err, variables, context) => {
      // Rollback on error
      if (context?.previousConversations) {
        queryClient.setQueryData(
          ["user", "conversations"],
          context.previousConversations,
        );
      }
      if (context?.previousConversation !== undefined) {
        queryClient.setQueryData(
          ["user", "conversation", variables.conversationId],
          context.previousConversation,
        );
      }
    },
    onSuccess: (data, variables) => {
      // Update cache with the server response to ensure data consistency
      queryClient.setQueryData(
        ["user", "conversation", variables.conversationId],
        data,
      );

      // Update the conversation in the list with server response
      queryClient.setQueryData(
        ["user", "conversations"],
        (old: { id: string; title: string }[] | undefined) =>
          old?.map((conv) =>
            conv.id === variables.conversationId
              ? { ...conv, title: data.title }
              : conv,
          ),
      );
    },
  });
};
