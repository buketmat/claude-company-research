# Company Research (brief): approved look and feel

Approved on 2026-11-24. This is the standard look for the web report, the PDF and the Word file. Keep it exactly like this unless the owner asks for a change. It is built in `scripts/report.py` as the `band` theme, which is the default (`DEFAULT_THEME`).

## Principles
- Clean and calm: no rounded "AI-style" cards, pill chips, shadows or coloured boxes around content.
- Hierarchy comes from type size, weight and spacing, not from boxes.
- Short text: tiles and one-line points, never paragraphs.

## Colours
| Use | Light | Dark |
|---|---|---|
| Page background (warm) | `#f6f5f1` | `#121211` |
| Report panel | `#ffffff` | `#1c1b19` |
| Text | `#1b1b19` | `#ecebe6` |
| Secondary text | `#6d6a63` | `#a29e95` |
| Hairlines | `#e7e4dc` | `#302e2a` |
| Accent (links, small markers) | deep green `#2f5d50` | `#86c4ad` |
| At a glance band | neutral light gray `#f2f2f2` | `#262626` |
| Dividers inside the band | `#dcdcdc` | `#3a3a3a` |
| Red flag, serious | red `#b3261e` | `#f2a19b` |
| Red flag, other | amber `#9a6a00` | `#f0c36b` |

## Layout
- **Header:** logo (rounded square), company name (32px, bold), one-liner (muted), then one line of plain text links separated by middle dots: website, LinkedIn, "Researched <date>". Copy and Download are plain green text links at the top right.
- **Section titles:** CAPITALS with letter spacing, 15px, bold, dark ink. No bullet or marker in front. Same style for SOURCES.
- **At a glance:** one neutral light gray band (no border, square corners) holding all key figures in 4 columns, separated by thin vertical lines. Each figure: small label, bold value, small grey note (period or context, or "Other sources: …" when agents disagree). The label says what the figure is (Revenue, ARR, GMV, Pre-tax profit, and so on).
- **Key points:** small green dots, hairline between lines.
- **What they sell / Main competitors:** positioning line first, then open two-column lists with a hairline above each item (no boxes); competitors show their site icon.
- **Recent news:** date column plus one-line headline, hairlines between rows.
- **Red flags:** only flags that were found; label in red (serious) or amber with a small dot, then the detail. Hairlines, no tinted boxes. The section is hidden when nothing is found.
- **Not found:** one small grey line at the end.
- **Sources:** right sidebar on the page background, no box; underlined green links with site icon, domain below, small "Official" or "Press" tag.

## PDF (A4, two pages at most)
- Page 1: the whole brief, shrunk until it fits. Page 2: the sources as one list of clickable links.
- Same colours and titles, with tighter spacing.

## Word (.docx)
- Arial. Capital, bold section titles with no marker. At a glance as a light gray (`F2F2F2`) table, three figures per row. Sources on page 2 as clickable links.
