"use client";

/**
 * EmployeesTab — manage the roster shown in the picker.
 *
 * Read /admin/employees?include_disabled=true so the table also shows
 * disabled rows (strikethrough). Mutations:
 *   • POST   /admin/employees             create
 *   • PATCH  /admin/employees/{id}        rename / recolor / re-enable
 *   • DELETE /admin/employees/{id}        soft-disable (audit history kept)
 *
 * Renames save on blur. Color picker writes immediately. Toggle button
 * flips active state.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { Button, Card } from "@/components/ui/primitives";
import { relay, type Employee } from "@/lib/relay";
import { cn } from "@/lib/utils";

interface EmployeesResp { employees: Employee[]; }

export default function EmployeesTab() {
  const qc = useQueryClient();

  const q = useQuery<EmployeesResp>({
    queryKey: ["employees", "all"],
    queryFn: () => relay.get<EmployeesResp>("/admin/employees?include_disabled=true"),
    staleTime: 10_000,
  });

  const createM = useMutation({
    mutationFn: (body: { display_name: string; color: string }) =>
      relay.post<Employee>("/admin/employees", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["employees"] }),
  });

  const patchM = useMutation({
    mutationFn: (args: { id: number; patch: Partial<Pick<Employee, "display_name" | "color" | "is_active">> }) =>
      relay.patch<Employee>(`/admin/employees/${args.id}`, args.patch),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["employees"] }),
  });

  const disableM = useMutation({
    mutationFn: (id: number) => relay.delete<unknown>(`/admin/employees/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["employees"] }),
  });

  // Hide chatter-mirror Employee rows (auto-created per linked chatter
  // login). They aren't owner-managed roster — renaming / disabling them
  // here would fight the chatter auth flow. Chatter management lives on
  // the Chatters tab; chatter mirrors stay in roster context so
  // attribution / audit / orphan-tip flows can still reference them.
  const rows = (q.data?.employees ?? []).filter((e) => !e.chatter_id);

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold">Roster</h2>
          <span className="text-[11px] text-fg-dim">
            {rows.filter((e) => e.is_active).length} active · {rows.length} total
          </span>
        </div>

        {q.isLoading && <div className="text-fg-dim text-sm">Loading…</div>}
        {q.error && (
          <div className="text-err text-sm">
            {(q.error as Error).message || "failed"}
          </div>
        )}

        {rows.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-fg-dim text-xs border-b border-border">
                <th className="px-3 py-2 font-medium w-12"></th>
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium w-32">Status</th>
                <th className="px-3 py-2 font-medium w-40">Created</th>
                <th className="px-3 py-2 font-medium w-32 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <Row
                  key={e.id}
                  e={e}
                  onRename={(name) => patchM.mutate({ id: e.id, patch: { display_name: name } })}
                  onRecolor={(color) => patchM.mutate({ id: e.id, patch: { color } })}
                  onToggle={() => {
                    if (e.is_active) disableM.mutate(e.id);
                    else patchM.mutate({ id: e.id, patch: { is_active: true } });
                  }}
                />
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <CreateCard
        onCreate={(name, color) => createM.mutate({ display_name: name, color })}
        pending={createM.isPending}
        error={createM.error as Error | null}
      />
    </div>
  );
}

function Row({
  e, onRename, onRecolor, onToggle,
}: {
  e: Employee;
  onRename: (name: string) => void;
  onRecolor: (color: string) => void;
  onToggle: () => void;
}) {
  const [name, setName] = useState(e.display_name);
  const lastSavedRef = useRef(e.display_name);

  useEffect(() => {
    setName(e.display_name);
    lastSavedRef.current = e.display_name;
  }, [e.display_name]);

  return (
    <tr className={cn(
      "border-b border-border/40 last:border-0",
      !e.is_active && "opacity-60",
    )}>
      <td className="px-3 py-2.5">
        <input
          type="color"
          value={e.color || "#888888"}
          onChange={(ev) => onRecolor(ev.target.value)}
          className="w-7 h-7 rounded-full border border-border bg-bg cursor-pointer p-0"
          aria-label={`Color for ${e.display_name}`}
        />
      </td>
      <td className="px-3 py-2.5">
        <input
          type="text"
          value={name}
          onChange={(ev) => setName(ev.target.value)}
          onBlur={() => {
            const trimmed = name.trim();
            if (!trimmed || trimmed === lastSavedRef.current) {
              setName(lastSavedRef.current);
              return;
            }
            lastSavedRef.current = trimmed;
            onRename(trimmed);
          }}
          className={cn(
            "bg-transparent border-0 px-1 py-0.5 text-sm rounded focus:outline-none focus:bg-bg-elev-1",
            !e.is_active && "line-through",
          )}
        />
      </td>
      <td className="px-3 py-2.5">
        <span className={cn(
          "text-[11px] px-2 py-0.5 rounded-full border",
          e.is_active
            ? "bg-ok/15 text-ok border-ok/30"
            : "bg-bg-elev-1 text-fg-dim border-border",
        )}>
          {e.is_active ? "active" : "disabled"}
        </span>
      </td>
      <td className="px-3 py-2.5 text-xs text-fg-dim">
        {fmtDate(e.created_at)}
      </td>
      <td className="px-3 py-2.5 text-right">
        <button
          type="button"
          onClick={onToggle}
          className="text-[11px] text-fg-dim hover:text-fg underline underline-offset-2"
        >
          {e.is_active ? "disable" : "re-enable"}
        </button>
      </td>
    </tr>
  );
}

function CreateCard({
  onCreate, pending, error,
}: { onCreate: (name: string, color: string) => void; pending: boolean; error: Error | null }) {
  const [name, setName] = useState("");
  const [color, setColor] = useState("#8b5cf6");

  return (
    <Card>
      <h3 className="text-sm font-semibold mb-3">Add employee</h3>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim()) return;
          onCreate(name.trim(), color);
          setName("");
        }}
        className="flex items-center gap-2"
      >
        <input
          type="text"
          placeholder="Name (e.g. Tim)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="flex-1 bg-bg border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:border-accent"
        />
        <input
          type="color"
          value={color}
          onChange={(e) => setColor(e.target.value)}
          className="w-9 h-9 rounded-md border border-border bg-bg cursor-pointer p-0"
          aria-label="Color"
        />
        <Button size="sm" type="submit" disabled={!name.trim() || pending}>
          {pending ? "Adding…" : "Add"}
        </Button>
      </form>
      {error && <p className="text-err text-xs mt-2">{error.message}</p>}
    </Card>
  );
}

function fmtDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}
