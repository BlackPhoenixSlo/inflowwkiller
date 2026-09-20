#!/usr/bin/env bash
# Find LLM calls we paid for that produced nothing. Read-only: one ssh, only
# SELECTs, writes nothing anywhere.
#
#   scripts/llm-waste-check.sh            # last 4 hours
#   scripts/llm-waste-check.sh --hours 24
#   scripts/llm-waste-check.sh --hours 2 --min-waste 3
#
# The number that matters is WASTED = calls a fan cost us minus messages he
# actually received. A generate-then-discard loop shows up as a fan with
# hundreds of calls and single-digit sends; it never self-heals, because the
# discard stamps nothing and the next tick re-admits him.
set -euo pipefail
HOST="${CONVO_COACH_HOST:-root@YOUR_VPS_IP}"
DB="${LLM_WASTE_DB:-~/fastt/service/chatterly.db}"
CONTAINER="${CONVO_COACH_CONTAINER:-fastt-relay}"
HOURS=4
MIN_WASTE=5

while [ $# -gt 0 ]; do
  case "$1" in
    --hours)     HOURS="$2"; shift 2 ;;
    --min-waste) MIN_WASTE="$2"; shift 2 ;;
    -h|--help)   sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
case "$HOURS$MIN_WASTE" in *[!0-9]*) echo "--hours/--min-waste must be integers" >&2; exit 2 ;; esac

echo "=== LLM waste check · last ${HOURS}h · $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

ssh "$HOST" "sqlite3 -readonly $DB" <<SQL
.headers on
.mode column
.nullvalue -

.print ''
.print '--- 0. HEADLINE: what we spent and what it bought ---'
.print '    sends EXCLUDES blasts: a mass_run copy is a template, no LLM behind it.'
WITH c AS (SELECT COUNT(*) n, COALESCE(SUM(cost_cents),0) mc FROM grok_calls
           WHERE called_at >= datetime('now','-${HOURS} hours')),
     s AS (SELECT COUNT(*) n FROM messages
           WHERE direction='out' AND automation_kind IS NOT NULL AND mass_run_id IS NULL
             AND created_at >= datetime('now','-${HOURS} hours')),
     d AS (SELECT COALESCE(SUM(je.value),0) n FROM automation_runs,
             json_each(json_extract(stats_json,'\$.stale_drops')) je
           WHERE started_at >= datetime('now','-${HOURS} hours'))
SELECT c.n AS calls, ROUND(c.mc/10000.0,4) AS dollars, s.n AS bot_sends,
       ROUND(1.0*c.n/MAX(1,s.n),1) AS calls_per_send,
       d.n AS discarded_at_wire,
       ROUND(100.0*d.n/MAX(1,c.n),1) AS pct_wasted
FROM c, s, d;

.print ''
.print '--- 1. LOOPS: fans whose calls never became messages  (THE headline detector) ---'
.print '    ai_chatter spends ~2 calls per reply. calls_per_send in double digits is a loop.'
WITH w AS (SELECT datetime('now','-${HOURS} hours') AS t0),
calls AS (SELECT account_id, fan_id, purpose, COUNT(*) n, SUM(cost_cents) mc
          FROM grok_calls, w WHERE called_at >= t0 AND fan_id IS NOT NULL GROUP BY 1,2,3),
sends AS (SELECT account_id, fan_id, COUNT(*) n FROM messages, w
          WHERE created_at >= t0 AND direction='out' GROUP BY 1,2)
SELECT c.account_id AS acct, c.fan_id, c.purpose,
       c.n AS calls, COALESCE(s.n,0) AS sent,
       c.n - COALESCE(s.n,0) AS wasted,
       ROUND(c.mc/10000.0,4) AS dollars
FROM calls c LEFT JOIN sends s ON s.account_id=c.account_id AND s.fan_id=c.fan_id
WHERE c.n - COALESCE(s.n,0) >= ${MIN_WASTE}
ORDER BY wasted DESC LIMIT 15;

.print ''
.print '--- 2. DISCARDED AFTER GENERATION: the counters that name the reason ---'
.print '    stale_drops = paid for, then thrown away at the wire. Healthy is zero rows.'
SELECT account_id AS acct, kind, 'stale_drop:'||je.key AS reason,
       SUM(je.value) AS n, COUNT(*) AS ticks,
       substr(MIN(started_at),12,5)||'-'||substr(MAX(started_at),12,5) AS span
FROM automation_runs, json_each(json_extract(stats_json,'\$.stale_drops')) je
WHERE started_at >= datetime('now','-${HOURS} hours')
GROUP BY 1,2,3
UNION ALL
SELECT account_id, kind, 'draft:'||je.key, SUM(je.value), COUNT(*),
       substr(MIN(started_at),12,5)||'-'||substr(MAX(started_at),12,5)
FROM automation_runs, json_each(json_extract(stats_json,'\$.draft_outcomes')) je
WHERE started_at >= datetime('now','-${HOURS} hours') AND je.key NOT IN ('sent','send_failed')
GROUP BY 1,2,3
UNION ALL
SELECT account_id, kind, k, SUM(v), COUNT(*),
       substr(MIN(started_at),12,5)||'-'||substr(MAX(started_at),12,5)
FROM (SELECT account_id, kind, started_at, 'dropped_empty' k,
             json_extract(stats_json,'\$.dropped_empty') v FROM automation_runs
      UNION ALL SELECT account_id, kind, started_at, 'skipped_answered_elsewhere',
             json_extract(stats_json,'\$.skipped_answered_elsewhere') FROM automation_runs
      UNION ALL SELECT account_id, kind, started_at, 'errors',
             json_extract(stats_json,'\$.errors') FROM automation_runs)
WHERE started_at >= datetime('now','-${HOURS} hours') AND v > 0
GROUP BY 1,2,3
ORDER BY n DESC LIMIT 20;

.print ''
.print '--- 3. FAILED / EMPTY GENERATIONS: billed, produced no text ---'
SELECT account_id AS acct, purpose, status,
       COUNT(*) AS n, ROUND(SUM(cost_cents)/10000.0,4) AS dollars,
       substr(MAX(COALESCE(error_text,'')),1,40) AS sample_error
FROM grok_calls
WHERE called_at >= datetime('now','-${HOURS} hours')
  AND (status <> 'done' OR COALESCE(error_text,'') <> '' OR COALESCE(TRIM(response_text),'') = '')
GROUP BY 1,2,3 ORDER BY n DESC LIMIT 10;

.print ''
.print '--- 4. TALKING TO A WALL: calls spent on fans who stopped replying ---'
.print '    Not automatically waste (a win-back IS the product) but a month of silence is.'
WITH c AS (SELECT account_id, fan_id, COUNT(*) n, SUM(cost_cents) mc
           FROM grok_calls WHERE called_at >= datetime('now','-${HOURS} hours')
             AND fan_id IS NOT NULL GROUP BY 1,2)
SELECT c.account_id AS acct, c.fan_id, c.n AS calls,
       ROUND(c.mc/10000.0,4) AS dollars,
       COALESCE(substr(MAX(m.created_at),1,16),'never') AS last_fan_msg,
       CAST(julianday('now') - julianday(MAX(m.created_at)) AS INT) AS days_silent
FROM c LEFT JOIN messages m
  ON m.account_id=c.account_id AND m.fan_id=c.fan_id AND m.direction='in'
GROUP BY c.account_id, c.fan_id
HAVING days_silent >= 7 OR days_silent IS NULL
ORDER BY c.n DESC LIMIT 10;

.print ''
.print '--- 5. REGENERATING THE SAME LINE: same opening 40 chars, same fan ---'
SELECT account_id AS acct, fan_id, COUNT(*) AS times,
       ROUND(SUM(cost_cents)/10000.0,4) AS dollars,
       substr(MAX(response_text),1,46) AS line
FROM grok_calls
WHERE called_at >= datetime('now','-${HOURS} hours') AND COALESCE(response_text,'') <> ''
GROUP BY account_id, fan_id, substr(lower(response_text),1,40)
HAVING times >= 3 ORDER BY times DESC LIMIT 10;

.print ''
.print '--- 6. WHERE THE MONEY GOES: per purpose ---'
SELECT purpose, COUNT(*) AS calls, ROUND(SUM(cost_cents)/10000.0,4) AS dollars,
       COUNT(DISTINCT fan_id) AS fans,
       ROUND(1.0*COUNT(*)/MAX(1,COUNT(DISTINCT fan_id)),1) AS calls_per_fan,
       SUM(tokens_in) AS tok_in, SUM(tokens_out) AS tok_out
FROM grok_calls WHERE called_at >= datetime('now','-${HOURS} hours')
GROUP BY purpose ORDER BY calls DESC;

.print ''
.print '--- 7. CAP HEADROOM (millicents/10000 = dollars; cap_cents/100 = dollars) ---'
.print '    Summed across providers: grok_daily_cost holds one row per provider per day.'
SELECT d.account_id AS acct, d.day,
       ROUND(SUM(d.cost_cents)/10000.0,3) AS spent,
       SUM(d.call_count) AS calls,
       ROUND(MAX(a.daily_cost_cap_cents)/100.0,2) AS cap_dollars,
       ROUND(100.0*(SUM(d.cost_cents)/10000.0)
             /MAX(0.01,MAX(a.daily_cost_cap_cents)/100.0),1) AS pct_of_cap,
       MAX(d.is_capped) AS capped
FROM grok_daily_cost d LEFT JOIN account_ai_config a ON a.account_id=d.account_id
WHERE d.day >= date('now','-1 day')
GROUP BY d.account_id, d.day
ORDER BY pct_of_cap DESC;
SQL

echo ""
echo "--- 8. RELAY LOG: cap trips + discard lines ---"
echo "    NOTE: docker logs begin at container boot, so this window is capped by uptime:"
ssh "$HOST" "docker inspect -f '    {{.Name}} up since {{.State.StartedAt}}' $CONTAINER"
echo "    LLMCapExceeded (0 is healthy):"
ssh "$HOST" "docker logs --since ${HOURS}h $CONTAINER 2>&1 | grep -c 'LLMCapExceeded' || true"
echo "    discard lines by reason (empty is healthy):"
ssh "$HOST" "docker logs --since ${HOURS}h $CONTAINER 2>&1 \
  | grep -oE 'dropped (a generated reply|empty reply) account=[0-9]+ fan=[0-9]+( reason=[a-z_]+)?' \
  | sed -E 's/fan=[0-9]+/fan=N/' | sort | uniq -c | sort -rn | head -10 || true"
