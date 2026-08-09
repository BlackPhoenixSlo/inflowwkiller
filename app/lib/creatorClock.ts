/**
 * The creator clock — one number per account, and everything that formats it.
 *
 * An account stores exactly ONE clock: `utc_offset`, whole hours, written by the
 * single "Her clock (place & time)" dropdown in the Brain. It drives the
 * time-of-day line in her welcome, the RIGHT-NOW line in every chat prompt, her
 * sleep window and rule quiet hours (server side: `rhythm.tz_offset_for`).
 *
 * There used to be two — this list of IANA zones was itself a second control, and
 * the zone silently outranked the offset stored beside it. Dana ran three
 * hours off and told new subscribers "it's Friday night in US" at 07:38 her time.
 * The zones survive here as PLACE NAMES for the dropdown's labels, never as a
 * stored value: `clockOptions` turns them into offsets, which is what gets saved.
 *
 * These are pure functions over Intl. They lived in a 1.4k-line panel and in an
 * unrelated settings module, with the `UTC±n` ternary copy-pasted at three call
 * sites; a clock the whole product reads deserves one home.
 */

/** The creator timezones this agency actually books — a curated PLACE list, used to
 *  name offsets in the clock dropdown. Not a stored value: see `clockOptions`. */
export const TIMEZONES: string[] = [
  "America/Los_Angeles", "America/Denver", "America/Phoenix", "America/Chicago",
  "America/New_York", "America/Toronto", "America/Vancouver", "America/Mexico_City",
  "America/Bogota", "America/Lima", "America/Santiago", "America/Sao_Paulo",
  "America/Argentina/Buenos_Aires", "Europe/London", "Europe/Dublin", "Europe/Lisbon",
  "Europe/Madrid", "Europe/Paris", "Europe/Amsterdam", "Europe/Berlin", "Europe/Rome",
  "Europe/Prague", "Europe/Warsaw", "Europe/Ljubljana", "Europe/Budapest",
  "Europe/Bucharest", "Europe/Athens", "Europe/Kyiv", "Europe/Moscow", "Europe/Istanbul",
  "Africa/Lagos", "Africa/Johannesburg", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata",
  "Asia/Bangkok", "Asia/Jakarta", "Asia/Singapore", "Asia/Manila", "Asia/Hong_Kong",
  "Asia/Tokyo", "Asia/Seoul", "Australia/Perth", "Australia/Brisbane",
  "Australia/Sydney", "Pacific/Auckland",
];

/** "UTC-4" / "UTC+2" / "UTC+0" — the one spelling of a stored clock. */
export function utcLabel(offsetHours: number): string {
  return `UTC${offsetHours >= 0 ? "+" : ""}${offsetHours}`;
}

/**
 * A zone's offset RIGHT NOW, as minutes AND its display label.
 *
 * ONE Intl round-trip with the label DERIVED from the number, so a caller that
 * groups by offset and a caller that prints one can never disagree. The label is
 * not parsed back into a number anywhere — a display string must not be
 * load-bearing in logic.
 */
export function zoneOffsetNow(tz: string): { minutes: number; label: string } | null {
  try {
    const now = new Date();
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(now);
    const at = (t: string) => Number(parts.find((p) => p.type === t)?.value);
    // The same instant read as if the zone's wall clock were UTC; the gap IS the
    // offset. `% 24` because ICU spells midnight "24" in some versions.
    const asUtc = Date.UTC(at("year"), at("month") - 1, at("day"),
                           at("hour") % 24, at("minute"), at("second"));
    // `|| 0` normalises the -0 that Math.round hands back for UTC itself. It is
    // equal to 0 everywhere it matters, but it is the kind of value that shows up
    // in a stored config or a log one day and costs someone an afternoon.
    const minutes = Math.round((asUtc - now.getTime()) / 60_000) || 0;
    if (!Number.isFinite(minutes)) return null;
    const sign = minutes < 0 ? "-" : "+";
    const abs = Math.abs(minutes);
    const rest = abs % 60;
    return {
      minutes,
      label: `UTC${sign}${Math.floor(abs / 60)}${rest ? `:${String(rest).padStart(2, "0")}` : ""}`,
    };
  } catch {
    return null;
  }
}

/** "-8/-7" — a zone's winter/summer offsets ("-3" alone when it has no DST).
 *  Read at Jan 1 / Jul 1 so the label never goes stale. */
export function zoneOffsets(tz: string): string {
  const at = (d: Date) => {
    const v = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(d)
      .find((p) => p.type === "timeZoneName")?.value ?? "GMT";
    return v.replace("GMT", "") || "+0";
  };
  try {
    const y = new Date().getFullYear();
    const jan = at(new Date(Date.UTC(y, 0, 1)));
    const jul = at(new Date(Date.UTC(y, 6, 1)));
    return jan === jul ? jan : `${jan}/${jul}`;
  } catch {
    return "";
  }
}

/** "America/Vancouver" → "America Vancouver (-8/-7)". Legacy-zone display only. */
export function zoneLabel(z: string): string {
  const off = zoneOffsets(z);
  const name = z.replace(/_/g, " ");
  return off ? `${name} (${off})` : name;
}

/**
 * THE clock dropdown's options: one row per distinct whole-hour offset, named by
 * the places that sit on it right now. The stored value IS the offset, so the
 * control round-trips exactly — no zone string in between that could resolve to
 * something else later. Places are listed because "UTC-4" alone is not something
 * anyone knows about a creator; "New York, Toronto, Miami" is.
 *
 * Half-hour zones (Kolkata, Adelaide) are absent BY CONSTRUCTION: the stored column
 * is whole hours, so offering them would mean writing a clock 30 minutes off and
 * calling it correct. A creator there needs the column widened first.
 */
export function clockOptions(): { offset: number; label: string }[] {
  const byOffset = new Map<number, string[]>();
  for (const z of TIMEZONES) {
    const zone = zoneOffsetNow(z);
    if (!zone || zone.minutes % 60 !== 0) continue;
    const place = z.split("/").slice(-1)[0].replace(/_/g, " ");
    const hours = zone.minutes / 60;
    const places = byOffset.get(hours);
    if (places) places.push(place);
    else byOffset.set(hours, [place]);
  }
  return [...byOffset.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([offset, places]) => ({
      offset,
      label: `${utcLabel(offset)} — ${places.slice(0, 3).join(", ")}`,
    }));
}

/** Her wall clock RIGHT NOW from the stored offset — the same arithmetic every
 *  engine does (utcnow + offset), formatted by reading the shifted instant AS UTC.
 *  The operator's glance-check that the dropdown is telling the truth. */
export function localTimeAtOffset(offsetHours: number): string {
  return new Date(Date.now() + offsetHours * 3600_000).toLocaleString("en-US", {
    timeZone: "UTC", weekday: "long", hour: "numeric", minute: "2-digit",
  });
}

/** Wall clock in a legacy IANA zone — for the "still running on the old zone" note,
 *  so an operator can see what that account is currently telling fans. */
export function localTimeIn(tz: string): string | null {
  try {
    return new Date().toLocaleTimeString("en-US", {
      timeZone: tz, hour: "numeric", minute: "2-digit",
    });
  } catch {
    return null;
  }
}
