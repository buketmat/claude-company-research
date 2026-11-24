---
name: brief
description: Brief company research: a short, sourced report on any company with its logo, red flags and PDF, Word and HTML downloads. Four fast agents search in parallel (the company itself, money, news, market and competitors); the report opens in the browser within seconds and fills in live as each agent finishes, complete in about 20 seconds. Use when the user types /brief-company-research-agent:brief, or asks for a company brief, or to research, profile, look up, vet or size up a company, startup, competitor, client or employer.
argument-hint: <company name or website>
model: haiku
effort: low
---

# Brief Company Research Agent

`S` = `${CLAUDE_SKILL_DIR}/scripts/report.py`. Run it with `python3`; if that command is missing (often on Windows), use `python`.

**Speed and economy are the point.** Do the whole job in this one turn. Ask nothing unless the company is missing or two real companies share the name. Do no research yourself, read no agent files, and write no progress messages.

## Step 1: Launch (one message, five calls together)
1. Company = `$ARGUMENTS` (or the user's message). Domain: if the user gave a website, use it. If you are sure of a well-known company's official domain (for example Mastercard → mastercard.com), use it without searching. Otherwise run **one** web search first to find it.
2. `slug` = lowercase name with hyphens. Run folder `D` = `<current working directory>/company-briefs/<slug>-<YYYY-MM-DD>` (absolute path).
3. In **one message**, send all five calls together:
   - Bash with `run_in_background: true`: `python3 "S" run "D" "<Company name>" <YYYY-MM-DD> <domain>`
     It creates the folder, fetches the logo, opens a live report in the browser that fills in as each agent's file lands, then writes the final report, PDF and Word file by itself.
   - Agent, `subagent_type: "brief-company-research-agent:cr-company"`
   - Agent, `subagent_type: "brief-company-research-agent:cr-money"`
   - Agent, `subagent_type: "brief-company-research-agent:cr-news"`
   - Agent, `subagent_type: "brief-company-research-agent:cr-market"`

   The four agents run at the same time. Each agent prompt is just four lines: `Company: <name>`, `Domain: <domain>`, `Research date: <YYYY-MM-DD>`, `Output path: D/frag-<company|money|news|market>.json`.

## Step 2: Wait for the report, then reply
**Write no text while the agents work**, not even when one of them finishes: the live page in the browser already shows progress. If agents report back one by one, end each of those turns with no text. Once all four have returned, run (foreground): `python3 "S" wait "D"`. It returns as soon as the report, PDF and Word file are written and prints a short summary. Then reply in at most 3 lines:
- **<Company>**: the summary line it printed (facts, sources, seconds).
- The report, PDF and Word paths (from the `Report:`, `PDF:` and `Word:` lines), as clickable links.

If the output says an agent did not finish, say which part is missing. Do not read the report or data files unless the user asks. If a notification about the background command arrives later, reply with nothing.

## Rules
- **Keep the approved look.** The report's design is fixed in `${CLAUDE_SKILL_DIR}/DESIGN.md` (the default `band` theme). Do not change colours, layout or titles unless the user asks for a design change.
- Evidence only: no opinions, ratings or investment advice. Missing facts show as "Not found"; never fill a gap yourself.
- PDF: the build also saves `<Company>-brief-<date>.pdf` (at most 2 A4 pages, clickable links) using the Chrome, Edge or Chromium already installed, and the report's Download menu points to it. With no such browser, it opens Save as PDF instead.
- To rebuild a report without researching again: `python3 "S" build "D" "<Company name>" <YYYY-MM-DD> <domain>`.
