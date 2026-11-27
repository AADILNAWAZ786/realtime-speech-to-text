import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { useApi } from "../api/ApiProvider";
import type {
  Transcription,
  TranscriptionCreate,
  TranscriptionListParams,
  TranscriptionUpdate,
} from "../api/types";

export const transcriptionKeys = {
  all: ["transcriptions"] as const,
  lists: () => [...transcriptionKeys.all, "list"] as const,
  list: (params: TranscriptionListParams) => [...transcriptionKeys.lists(), params] as const,
  stats: () => [...transcriptionKeys.all, "stats"] as const,
  detail: (id: string) => [...transcriptionKeys.all, "detail", id] as const,
};

export function useTranscriptionList(params: TranscriptionListParams) {
  const { endpoints } = useApi();
  return useQuery({
    queryKey: transcriptionKeys.list(params),
    queryFn: ({ signal }) => endpoints.transcriptions.list(params, signal),
    placeholderData: keepPreviousData,
  });
}

export interface TranscriptionStats {
  total: number;
  completedRecordings: number;
  manual: number;
}

/** Counts from the list endpoint's `total` (page_size=1): there is no stats endpoint. */
export function useTranscriptionStats() {
  const { endpoints } = useApi();
  return useQuery({
    queryKey: transcriptionKeys.stats(),
    queryFn: async ({ signal }): Promise<TranscriptionStats> => {
      const count = (params: TranscriptionListParams) =>
        endpoints.transcriptions
          .list({ ...params, page: 1, page_size: 1 }, signal)
          .then((p) => p.total);
      const [total, completedRecordings, manual] = await Promise.all([
        count({}),
        count({ source: "live", status: "completed" }),
        count({ source: "manual" }),
      ]);
      return { total, completedRecordings, manual };
    },
  });
}

export function useTranscription(id: string, { enabled = true } = {}) {
  const { endpoints } = useApi();
  return useQuery({
    queryKey: transcriptionKeys.detail(id),
    enabled,
    queryFn: ({ signal }) => endpoints.transcriptions.get(id, { includeSegments: true, signal }),
  });
}

export function useCreateTranscription() {
  const { endpoints } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: TranscriptionCreate) => endpoints.transcriptions.create(data),
    onSuccess: async (created) => {
      queryClient.setQueryData(transcriptionKeys.detail(created.id), { ...created, segments: [] });
      await queryClient.invalidateQueries({ queryKey: transcriptionKeys.lists() });
      await queryClient.invalidateQueries({ queryKey: transcriptionKeys.stats() });
    },
  });
}

export function useUpdateTranscription(id: string) {
  const { endpoints } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (data: TranscriptionUpdate) => endpoints.transcriptions.update(id, data),
    onSuccess: async (updated) => {
      // PATCH returns the record without segments: keep the ones already loaded.
      queryClient.setQueryData<Transcription>(transcriptionKeys.detail(id), (previous) => ({
        ...updated,
        segments: previous?.segments ?? null,
      }));
      await queryClient.invalidateQueries({ queryKey: transcriptionKeys.lists() });
    },
  });
}

export function useDeleteTranscription() {
  const { endpoints } = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => endpoints.transcriptions.remove(id),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: transcriptionKeys.lists() }),
        queryClient.invalidateQueries({ queryKey: transcriptionKeys.stats() }),
      ]),
  });
}

/**
 * Drop a deleted record's cached detail. Disable the detail query first (see
 * TranscriptionDetailPage): an enabled observer would refetch it and get a 404.
 */
export function forgetTranscription(queryClient: QueryClient, id: string) {
  queryClient.removeQueries({ queryKey: transcriptionKeys.detail(id) });
}
