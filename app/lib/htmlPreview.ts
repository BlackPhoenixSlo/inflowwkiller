/** Plain-text preview from OF's HTML body — used for both quote previews
 *  and search match snippets. Cheap and intentionally lossy: anything
 *  beyond `max` is truncated with an ellipsis. */
export function stripHtmlPreview(s: string, max: number): string {
  const plain = s
    .replace(/<br\s*\/?>/gi, " ")
    .replace(/<\/p\s*>/gi, " ")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
  return plain.length > max ? plain.slice(0, max - 1) + "…" : plain;
}
