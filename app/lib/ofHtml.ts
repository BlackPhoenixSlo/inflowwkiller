/**
 * Strip OF's HTML wrappers: `<br>` → \n, `</p><p>` → \n\n, then drop
 * remaining tags and decode the common named entities. OF markdown-rendered
 * bodies arrive wrapped in `<p>`. Used wherever we render an OF message body
 * as plain text (drawer previews, scheduled-send popovers, …).
 */
export function stripOFHtml(html: string | null | undefined): string {
  if (!html) return "";
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>\s*<p[^>]*>/gi, "\n\n")
    .replace(/<\/?p[^>]*>/gi, "")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .trim();
}
