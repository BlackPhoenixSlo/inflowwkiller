"use client";

import {
  ListColumn,
  SYSTEM_AUDIENCES,
  SysChip,
  useFanLists,
} from "./fanLists";

/**
 * Audience picker for an automation's house broadcast — the SAME parts the Mass
 * Messages composer uses (see `./fanLists`), so an operator who has picked an
 * audience once has picked one everywhere, and the include/exclude asymmetry is
 * defined in exactly one place.
 *
 * The exclude column is IDS ONLY: OF silently drops "fans"/"following" from
 * `excludedLists`, so the server 422s rather than storing an exclusion that
 * cannot work. Untick the system chip instead.
 */
export function BroadcastAudiencePicker({
  accountId,
  include,
  exclude,
  onToggleInclude,
  onToggleExclude,
  disabled = false,
}: {
  accountId: string;
  /** Included audience: system names and/or custom list ids, as strings. */
  include: Set<string>;
  /** Excluded list ids, as strings. Never system names. */
  exclude: Set<string>;
  onToggleInclude: (id: string) => void;
  onToggleExclude: (id: string) => void;
  disabled?: boolean;
}) {
  const { customLists, isLoading } = useFanLists(accountId);

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <div className="text-[11px] uppercase tracking-wide text-fg-dim">Send to</div>
        <div className="flex flex-wrap gap-1.5">
          {SYSTEM_AUDIENCES.map((b) => (
            <SysChip
              key={b.id}
              state={include.has(b.id) ? "in" : "off"}
              onClick={() => onToggleInclude(b.id)}
              label={b.label}
              disabled={disabled}
            />
          ))}
        </div>
        {include.size === 0 && (
          // STATE, not a scolding — an empty audience is a legal thing to want.
          // But it has to be SAID: silence here would read as "still sending",
          // and the only other signal is a flat revenue day.
          <div className="text-[11px] text-warn">
            Nothing selected — this broadcast will not send. Fans already in the
            system still get their per-tier PPV.
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-border/60">
        <div>
          <div className="text-[11px] text-fg-dim mb-1.5">Custom lists — also send to</div>
          <ListColumn
            lists={customLists}
            loading={isLoading}
            selected={include}
            onToggle={onToggleInclude}
            disabled={disabled}
            emptyLabel="No custom lists on this account."
          />
        </div>
        <div>
          <div className="text-[11px] text-fg-dim mb-1.5">Custom lists — never send to</div>
          <ListColumn
            lists={customLists}
            loading={isLoading}
            selected={exclude}
            onToggle={onToggleExclude}
            disabled={disabled}
            emptyLabel="—"
          />
        </div>
      </div>
    </div>
  );
}
