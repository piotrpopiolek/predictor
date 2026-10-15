"""Styles for the match detail page."""

_DETAIL_CSS = """
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
    --bar: #58a6ff;
    --bar-away: #8b949e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.45;
  }
  header {
    position: sticky;
    top: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 12px 24px;
    align-items: baseline;
    justify-content: space-between;
    padding: 16px 20px;
    background: rgba(13, 17, 23, 0.92);
    border-bottom: 1px solid var(--line);
    backdrop-filter: blur(8px);
    z-index: 1;
  }
  h1 {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
    flex-shrink: 0;
  }
  .nav { display: flex; gap: 8px; font-size: 0.85rem; }
  .nav a {
    color: var(--muted); text-decoration: none;
    padding: 4px 10px; border-radius: 999px;
  }
  .nav a:hover { color: var(--text); }
  .nav a.active {
    color: var(--text); background: var(--card);
    border: 1px solid var(--line);
  }
  main { max-width: 920px; margin: 0 auto; padding: 20px 20px 48px; }
  .hero, .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 14px;
  }
  .kicker {
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 0.75rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .headline {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 16px;
    align-items: center;
  }
  .team {
    display: flex; gap: 10px; align-items: center;
    font-weight: 650; min-width: 0;
  }
  .team.away { flex-direction: row-reverse; text-align: right; }
  .team-id {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .team.away .team-id { align-items: flex-end; }
  .team-name { overflow: hidden; text-overflow: ellipsis; }
  .team-odd, .score-odd {
    font-variant-numeric: tabular-nums;
    font-weight: 650;
    font-size: 0.95rem;
  }
  .logo {
    width: 36px; height: 36px; object-fit: contain; flex-shrink: 0;
  }
  .scoreblock { text-align: center; }
  .score {
    font-size: 2rem; font-weight: 700;
    font-variant-numeric: tabular-nums; letter-spacing: 0.04em;
  }
  .meta, .chips, .periods, .sub, .muted { color: var(--muted); }
  .clock { color: var(--live); font-weight: 650; }
  .chips, .periods {
    display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 0;
    font-size: 0.8rem;
  }
  .chips span, .periods span {
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 3px 10px;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }
  h2 {
    margin: 0 0 12px;
    font-size: 0.78rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 650;
  }
  h3 { margin: 0 0 8px; font-size: 0.92rem; }
  .timeline, .xi-list, .h2h, .compare {
    list-style: none; margin: 0; padding: 0;
  }
  .timeline li {
    display: grid;
    grid-template-columns: 3.2rem 1fr;
    gap: 10px;
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
    align-items: start;
  }
  .timeline li.away {
    grid-template-columns: 1fr 3.2rem;
  }
  .timeline li.away .minute { order: 2; text-align: right; }
  .timeline li.away .ev { order: 1; text-align: right; }
  .timeline .ev {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .timeline .club {
    font-weight: 650;
    font-size: 0.82rem;
  }
  .timeline li.home .club { color: var(--text); }
  .timeline li.away .club { color: var(--muted); }
  .h2h li, .compare li, .xi-list li {
    display: grid;
    grid-template-columns: 3.2rem 1fr auto;
    gap: 8px;
    padding: 6px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
  }
  .xi-list li { grid-template-columns: 1.6rem 1fr auto; }
  .minute, .num, .when, .pos {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .what em { color: var(--muted); font-style: normal; }
  .stat { margin: 10px 0 14px; }
  .stat-top, .pct, .odds, .compare li {
    display: flex; justify-content: space-between; gap: 8px;
    font-variant-numeric: tabular-nums; font-size: 0.86rem;
  }
  .stat-label {
    color: var(--muted); text-align: center; flex: 1;
  }
  .bars { display: flex; gap: 4px; height: 4px; margin-top: 6px; }
  .bars i {
    display: block; height: 4px; border-radius: 99px;
    background: var(--bar); min-width: 2px;
  }
  .bars i.away { background: var(--bar-away); margin-left: auto; }
  .xi { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .tables, .form-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .tables .kicker { margin-bottom: 8px; }
  .form-cols h3 { margin-bottom: 4px; }
  .form-cols .muted { margin: 0 0 8px; font-size: 0.82rem; }
  table.stand { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  table.stand th {
    color: var(--muted); font-weight: 500; text-align: right;
    font-size: 0.72rem; padding: 0 4px 6px;
  }
  table.stand th.name, table.stand td.name { text-align: left; }
  table.stand td {
    padding: 5px 4px; border-top: 1px solid var(--line);
    text-align: right; font-variant-numeric: tabular-nums;
  }
  table.stand tr.club td.name { font-weight: 650; }
  table.stand tr.up td.name { color: var(--live); }
  table.stand tr.down td.name { color: #f85149; }
  .split, .moves { margin: 0 0 8px; font-size: 0.82rem; }
  .split { color: var(--text); }
  .delta.up { color: var(--live); }
  .delta.down { color: #f85149; }
  .delta.flat { color: var(--muted); }
  .split-title { margin-top: 16px; }
  .split-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .split-card {
    border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px;
    font-size: 0.8rem;
  }
  .split-card.active { border-color: var(--live); }
  .split-card p { margin: 0 0 4px; }
  .split-label {
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em;
    font-size: 0.68rem;
  }
  .strength { margin: 0 0 4px; font-size: 0.82rem; font-weight: 650; }
  .strength-note { margin: 0 0 8px; color: var(--muted); font-size: 0.75rem; }
  .bench { color: var(--muted); font-size: 0.8rem; margin: 10px 0 0; }
  .bench span {
    display: block; text-transform: uppercase; letter-spacing: 0.05em;
    font-size: 0.68rem; margin-bottom: 4px;
  }
  .advice { margin: 8px 0 0; }
  .odds { align-items: baseline; flex-wrap: wrap; margin: 8px 0; }
  .odds em { color: var(--muted); font-style: normal; font-weight: 500; }
  .subs { list-style: none; margin: 0; padding: 0; }
  .subs li {
    padding: 8px 0;
    border-top: 1px solid var(--line);
    font-size: 0.88rem;
  }
  .effect { margin: 4px 0 0; color: var(--muted); font-size: 0.82rem; }
  .chance {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: baseline;
    margin: 0 0 8px;
  }
  .chance strong { font-variant-numeric: tabular-nums; font-size: 1.15rem; }
  .market { margin: 0 0 14px; }
  .market h3 {
    margin: 0 0 6px;
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 650;
  }
  .quotes { display: flex; flex-wrap: wrap; gap: 8px; }
  .quotes span {
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 4px 8px;
    font-variant-numeric: tabular-nums;
    font-size: 0.86rem;
  }
  .quotes em {
    color: var(--muted);
    font-style: normal;
    font-weight: 500;
    margin-right: 6px;
  }
  .empty { text-align: center; color: var(--muted); padding: 48px 16px; }
  .sub { font-size: 0.78rem; text-align: right; }
  @media (max-width: 720px) {
    .grid, .xi, .headline, .tables, .form-cols, .split-grid {
      grid-template-columns: 1fr;
    }
    .team.away { flex-direction: row; text-align: left; }
    .team.away .team-id { align-items: flex-start; }
    .scoreblock { order: -1; }
  }
"""
