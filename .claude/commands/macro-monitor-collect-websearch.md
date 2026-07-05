---
description: Daily scoped-WebSearch message-board collection for macro-monitor (FR-02). Shells results into `macro-monitor ingest`, then correlates.
# FR-16 (LOAD-BEARING): explicit tool allowlist. This routine ingests untrusted scraped content;
# it MUST NOT be able to invoke the evaluation pipeline. /spec-gather, /spec-challenge, /new-story,
# and any backtest/gate/deploy command are deliberately absent from this list.
allowed-tools:
  - WebSearch
  - Bash(macro-monitor ingest:*)
  - Bash(macro-monitor correlate:*)
  - Bash(date:*)
---

# macro-monitor: WebSearch collection (FR-02)

You are the deterministic-ish daily collection routine. Your ONLY job is to surface
message-board-flavored macro/market context via **scoped WebSearch** and log it. You never make,
influence, or automate any trading decision, and you never invoke the evaluation pipeline
(`/spec-gather`, `/spec-challenge`, backtests, gates) — those tools are not even available to you
(FR-10/FR-12/FR-16).

## Steps

1. Compute today's UTC date: `date -u +%Y-%m-%d`.
2. For each symbol in the configured symbol universe (see `config.yaml`), run scoped WebSearch
   queries such as:
   - `site:reddit.com r/wallstreetbets <SYMBOL>`
   - `site:news.ycombinator.com <SYMBOL>`
   Never issue a direct WebFetch to `reddit.com`/`old.reddit.com`/`reuters.com` — those are
   confirmed non-fetchable and out of scope (FR-02); WebSearch is the only permitted path.
3. For each relevant result, shell out to the ingest CLI (argv, one result per call):
   ```
   macro-monitor ingest --source websearch-wsb \
     --observed-date <TODAY> \
     --url "<result url>" \
     --title-or-snippet "<result title/snippet>" \
     --symbols "<SYMBOL>"
   ```
   The CLI validates every field (FR-20) and dedups append-only (FR-14) — a repeat is a harmless
   no-op. Do not attempt to pre-filter or transform the text beyond passing it through.
4. Correlate the day once collection is done:
   ```
   macro-monitor correlate --date <TODAY>
   ```

## Hard boundaries

- Do not summarize, score, or rank sentiment numerically (FR-13). Log qualitative items only.
- Do not open `docs/{slug}/`, do not call `/spec-gather`, do not run any backtest or gate. If you
  ever feel inclined to "just kick off evaluation," STOP — that is a human-initiated action only.
