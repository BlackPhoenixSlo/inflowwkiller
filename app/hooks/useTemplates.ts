"use client";

/**
 * useTemplates — OF's saved-reply / message-template feature.
 *
 * OF stores templates per account. Each carries text + optional media +
 * an optional `template` slot (currently only `reply_on_subscribe` for
 * the welcome message). We list/create/update/delete via the relay's
 * `/api/of/v2/messages/templates` passthrough.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay, type OFMessageTemplate } from "@/lib/relay";

const KEY_PREFIX = "templates";

export function useTemplates(accountId: string | null) {
  return useQuery<OFMessageTemplate[]>({
    queryKey: [KEY_PREFIX, accountId],
    enabled: !!accountId,
    queryFn: () =>
      relay.get<OFMessageTemplate[]>("/api/of/v2/messages/templates", {
        accountId: accountId ?? undefined,
      }),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}

interface CreateOpts {
  text: string;
  price?: number;
  lockedText?: boolean;
  mediaFiles?: Array<number | Record<string, unknown>>;
  template?: string;
}

// Relay's Pydantic schemas use snake_case (matches every other endpoint).
// Translate at the hook boundary so the rest of the UI keeps the camelCase
// it shares with the OF API surface.
function toWire(opts: Partial<CreateOpts>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (opts.text !== undefined) out.text = opts.text;
  if (opts.price !== undefined) out.price = opts.price;
  if (opts.lockedText !== undefined) out.locked_text = opts.lockedText;
  if (opts.mediaFiles !== undefined) out.media_files = opts.mediaFiles;
  if (opts.template !== undefined) out.template = opts.template;
  return out;
}

export function useCreateTemplate(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<OFMessageTemplate, Error, CreateOpts>({
    mutationFn: (opts) =>
      relay.post<OFMessageTemplate>("/api/of/v2/messages/templates", toWire(opts), {
        accountId: accountId ?? undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}

interface UpdateOpts extends Partial<CreateOpts> {
  id: string;
}

export function useUpdateTemplate(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<OFMessageTemplate, Error, UpdateOpts>({
    mutationFn: ({ id, ...patch }) =>
      relay.put<OFMessageTemplate>(`/api/of/v2/messages/templates/${id}`, toWire(patch), {
        accountId: accountId ?? undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}

export function useDeleteTemplate(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, string>({
    mutationFn: (id) =>
      relay.delete(`/api/of/v2/messages/templates/${id}`, {
        accountId: accountId ?? undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}

// ── Welcome message slot ─────────────────────────────────────────
// OF's welcome template lives behind a slot-specific URL — POST to
// /messages/templates/reply_on_subscribe to create/replace; DELETE the
// same URL to clear. Separate `replyOnSubscribe` flag on /users/me
// controls whether OF actually fires it on new subs.

interface WelcomeOpts {
  text: string;
  price?: number;
  lockedText?: boolean;
  mediaFiles?: Array<number | Record<string, unknown>>;
  previews?: number[];
}

function welcomeToWire(opts: WelcomeOpts): Record<string, unknown> {
  return {
    text: opts.text,
    price: opts.price ?? 0,
    locked_text: !!opts.lockedText,
    media_files: opts.mediaFiles ?? [],
    previews: opts.previews ?? [],
  };
}

export function useUpsertWelcome(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<OFMessageTemplate, Error, WelcomeOpts>({
    mutationFn: (opts) =>
      relay.post<OFMessageTemplate>(
        "/api/of/v2/messages/templates/reply_on_subscribe",
        welcomeToWire(opts),
        { accountId: accountId ?? undefined },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}

export function useDeleteWelcome(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, void>({
    mutationFn: () =>
      relay.delete("/api/of/v2/messages/templates/reply_on_subscribe", {
        accountId: accountId ?? undefined,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [KEY_PREFIX, accountId] }),
  });
}

// The welcome template's `isActive` field on /messages/templates does NOT
// track the master `replyOnSubscribe` switch (verified live: settings had
// replyOnSubscribe=true while the template returned isActive=false). Read
// the flag from /users/me/settings directly and use that to drive the
// Enable/Disable UI.
const REPLY_ON_SUBSCRIBE_KEY = "reply-on-subscribe";

interface _MySettings { replyOnSubscribe?: boolean }

export function useReplyOnSubscribeFlag(accountId: string | null) {
  return useQuery<boolean>({
    queryKey: [REPLY_ON_SUBSCRIBE_KEY, accountId],
    enabled: !!accountId,
    queryFn: async () => {
      const s = await relay.get<_MySettings>("/api/of/v2/users/me/settings", {
        accountId: accountId ?? undefined,
      });
      return !!s?.replyOnSubscribe;
    },
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useToggleReplyOnSubscribe(accountId: string | null) {
  const qc = useQueryClient();
  return useMutation<unknown, Error, boolean, { prev: boolean | undefined }>({
    mutationFn: (enabled) =>
      relay.patch("/api/of/v2/users/me/reply-on-subscribe", { enabled }, {
        accountId: accountId ?? undefined,
      }),
    onMutate: async (enabled) => {
      await qc.cancelQueries({ queryKey: [REPLY_ON_SUBSCRIBE_KEY, accountId] });
      const prev = qc.getQueryData<boolean>([REPLY_ON_SUBSCRIBE_KEY, accountId]);
      qc.setQueryData([REPLY_ON_SUBSCRIBE_KEY, accountId], enabled);
      return { prev };
    },
    onError: (_err, _v, ctx) => {
      if (ctx) qc.setQueryData([REPLY_ON_SUBSCRIBE_KEY, accountId], ctx.prev);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: [REPLY_ON_SUBSCRIBE_KEY, accountId] });
    },
  });
}
