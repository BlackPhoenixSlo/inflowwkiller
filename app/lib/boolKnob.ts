/** The ONE reader for a catalogued `type: "bool"` automation knob.
 *
 *  Every automation payload knob typed `bool` in the relay's `_CATALOG` has a
 *  declared default, and five UI surfaces read those knobs: the Brain welcome
 *  card, the Growth tabs, the mass-send tabs and the typed RuleEditor. They must
 *  give the same answer as the automation, and the automation reads them through
 *  exactly one helper (`service/automations/_common.py::bool_knob`). This is its
 *  other half — grep `bool_knob` to find both.
 *
 *  ⚠️ `== null`, deliberately loose. It catches BOTH `undefined` (the key was
 *  never written) and `null`, and `null` is not hypothetical: the rules API's
 *  `_validate_payload_for_kind` used to SKIP a None value rather than rejecting
 *  it, which made `null` the one non-boolean that reached storage for a bool
 *  knob. That boundary now POPS the key, so no NEW rule can store one — this
 *  null clause defends rules written before that line, and stored nulls do not
 *  expire. The two spellings that look like this one are wrong for exactly that
 *  value:
 *
 *      Boolean(v)                 // null → false, ignoring the default
 *      v === undefined ? d : !!v  // null → false, ignoring the default
 *
 *  On `follow_back_gate` and `money_gate` those two answers are "we price-check
 *  every fan before following" and "we buy a subscription to every priced
 *  creator in the pool". A value that SAYS nothing means what absent means.
 *
 *  ⚠️ Everything else is read with PYTHON truthiness, not JavaScript's. The
 *  relay is the side that acts on the knob, so where the two languages disagree
 *  the relay's answer is the true one and this half has to follow it: `[]` and
 *  `{}` are FALSY in python and TRUTHY in JS, so a bare `Boolean(v)` here would
 *  render a knob ON that the automation runs OFF. (`0` and `""` agree already;
 *  they are spelled out below anyway so the intent is not mistaken for a bug.)
 *  A JSON payload holds only null/bool/number/string/array/object, which is the
 *  whole domain this has to cover.
 *
 *  Both halves are pinned to ONE table — `service/tests/fixtures/bool_knob_cases.json`
 *  — by `boolKnob.test.ts` here and `test_automation_rules_api`
 *  `.case_bool_knob_matches_the_typescript_half` there. Nothing else keeps them
 *  in step, and before that fixture existed they had already drifted. */
export function boolKnob(v: unknown, dflt: boolean): boolean {
  if (v == null) return dflt;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === "object") return Object.keys(v).length > 0;
  return Boolean(v);
}
