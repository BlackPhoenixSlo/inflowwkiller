"use client";

/**
 * useReorder — generic HTML5 drag-and-drop helper for reordering an array
 * of items. Position-based: drag a tile from index A to index B and the
 * item at A lands at B. The hook owns the drag/drop event state; the
 * caller wires the returned handlers onto each rendered chip and applies
 * its own visual hint via `dragOverIndex` / `draggingIndex`.
 *
 * Keyboard a11y: callers should listen for ArrowLeft / ArrowRight on
 * focused chips and call `onKeyboardMove(idx, dir)` so users without a
 * pointing device can still reorder.
 *
 * `getKey` is required for stable `dataTransfer.setData` payloads (Firefox
 * refuses to start a drag without any data set) and for caller-side React
 * keys.
 */

import { useCallback, useRef, useState } from "react";

export interface UseReorderResult {
  onDragStart: (e: React.DragEvent, index: number) => void;
  onDragOver: (e: React.DragEvent, index: number) => void;
  onDrop: (e: React.DragEvent, index: number) => void;
  onDragEnd: (e: React.DragEvent) => void;
  onKeyboardMove: (index: number, dir: -1 | 1) => void;
  dragOverIndex: number | null;
  draggingIndex: number | null;
}

export function useReorder<T>(
  items: T[],
  onChange: (next: T[]) => void,
  getKey: (item: T) => string | number,
): UseReorderResult {
  const [draggingIndex, setDraggingIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);
  // Keep latest items in a ref so the stable handler closures see fresh
  // state without re-binding on every render.
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const move = useCallback(
    (from: number, to: number) => {
      const cur = itemsRef.current;
      if (from < 0 || from >= cur.length) return;
      const clampedTo = Math.max(0, Math.min(cur.length - 1, to));
      if (from === clampedTo) return;
      const next = cur.slice();
      const [picked] = next.splice(from, 1);
      next.splice(clampedTo, 0, picked);
      onChange(next);
    },
    [onChange],
  );

  const onDragStart = useCallback(
    (e: React.DragEvent, index: number) => {
      setDraggingIndex(index);
      e.dataTransfer.effectAllowed = "move";
      try {
        e.dataTransfer.setData("text/plain", String(getKey(itemsRef.current[index])));
      } catch {
        // Some sandboxes throw on setData; drag still proceeds without it.
      }
    },
    [getKey],
  );

  const onDragOver = useCallback((e: React.DragEvent, index: number) => {
    // preventDefault is what tells the browser "yes, this is a valid drop
    // target" — without it the drop event never fires.
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverIndex(index);
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent, index: number) => {
      e.preventDefault();
      const from = draggingIndex;
      setDraggingIndex(null);
      setDragOverIndex(null);
      if (from == null) return;
      move(from, index);
    },
    [draggingIndex, move],
  );

  const onDragEnd = useCallback(() => {
    setDraggingIndex(null);
    setDragOverIndex(null);
  }, []);

  const onKeyboardMove = useCallback(
    (index: number, dir: -1 | 1) => move(index, index + dir),
    [move],
  );

  return {
    onDragStart, onDragOver, onDrop, onDragEnd, onKeyboardMove,
    dragOverIndex, draggingIndex,
  };
}
