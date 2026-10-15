"""Styles for the live board, day page, and bet history."""

_LIVE_CSS = """
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --line: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --live: #3fb950;
    --score: #f0f6fc;
    --red: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.4;
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
    font-size: 1.25rem;
    font-weight: 650;
    display: flex;
    gap: 10px;
    align-items: center;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--live);
    box-shadow: 0 0 0 4px rgba(63, 185, 80, 0.25);
  }
  .header-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    align-items: baseline;
  }
  .nav {
    display: flex;
    gap: 8px;
    font-size: 0.85rem;
  }
  .nav a {
    color: var(--muted);
    text-decoration: none;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid transparent;
  }
  .nav a:hover { color: var(--text); }
  .nav a.active {
    color: var(--text);
    border-color: var(--line);
    background: var(--card);
  }
  .sub { color: var(--muted); font-size: 0.9rem; }
  main { max-width: 1040px; margin: 0 auto; padding: 16px 20px 40px; }
  .league {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  h2 {
    margin: 0;
    padding: 12px 16px;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .league-logo {
    width: 18px;
    height: 18px;
    object-fit: contain;
    flex-shrink: 0;
  }
  .league-logo.fallback { display: none; }
  .match {
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--line);
  }
  .match:last-child { border-bottom: 0; }
  .match-link {
    display: block;
    color: inherit;
    text-decoration: none;
  }
  .match-link:hover { background: rgba(255, 255, 255, 0.03); }
  .headline {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    align-items: center;
  }
  .team {
    display: flex;
    gap: 10px;
    align-items: center;
    min-width: 0;
  }
  .team.away { flex-direction: row-reverse; text-align: right; }
  .team-text { min-width: 0; }
  .name {
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-flex;
    gap: 6px;
    align-items: center;
    max-width: 100%;
  }
  .team.away .name { flex-direction: row-reverse; }
  .team-odd, .score-odd {
    display: block;
    margin-top: 2px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    font-size: 0.92rem;
  }
  .score-odd { text-align: center; }
  .logo {
    width: 28px;
    height: 28px;
    object-fit: contain;
    flex-shrink: 0;
    border-radius: 4px;
    background: #0d1117;
  }
  .logo.fallback {
    display: inline-block;
    border: 1px solid var(--line);
  }
  .formation {
    display: block;
    color: var(--muted);
    font-size: 0.72rem;
    font-weight: 500;
    margin-top: 2px;
  }
  .reds { display: inline-flex; gap: 2px; flex-shrink: 0; }
  .redcard {
    width: 7px;
    height: 10px;
    border-radius: 1px;
    background: var(--red);
  }
  .scoreblock { text-align: center; min-width: 7.5rem; }
  .score {
    font-variant-numeric: tabular-nums;
    font-size: 1.45rem;
    font-weight: 700;
    color: var(--score);
    letter-spacing: 0.04em;
  }
  .meta {
    color: var(--muted);
    font-size: 0.8rem;
    display: flex;
    gap: 8px;
    justify-content: center;
  }
  .clock { color: var(--live); font-weight: 650; }
  .scorers {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
  }
  .scorer-list {
    list-style: none;
    margin: 0;
    padding: 0;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .scorer-col.away { text-align: right; }
  .who { color: var(--text); }
  .facts, .stats, .next-goal {
    margin: 8px 0 0;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .form {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    gap: 12px;
    margin-top: 8px;
    color: var(--muted);
    font-size: 0.78rem;
  }
  .form-col.away { text-align: right; }
  .form-gap {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
    align-self: center;
  }
  .next-goal {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 14px;
    align-items: baseline;
  }
  .ng-label {
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.68rem;
    font-weight: 650;
  }
  .ng-odd {
    color: var(--text);
    font-variant-numeric: tabular-nums;
    font-weight: 650;
  }
  .ng-team { color: var(--muted); font-weight: 500; }
  .next-goal.suspended .ng-odd { color: var(--muted); }
  .empty { color: var(--muted); text-align: center; padding: 48px 16px; }
  @media (max-width: 640px) {
    .headline, .scorers { grid-template-columns: 1fr; text-align: center; }
    .team, .team.away { flex-direction: column; }
    .name, .team.away .name { white-space: normal; flex-direction: row; }
    .scorer-col.away { text-align: center; }
    .form { grid-template-columns: 1fr; text-align: center; }
    .form-col.away { text-align: center; }
    .next-goal { justify-content: center; }
  }

  .bet-actions, .bet-open {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--line);
  }
  .bet-toggle, .bet-form button, .bet-settle button {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 12px;
    font: inherit;
    cursor: pointer;
  }
  .bet-toggle:hover, .bet-form button:hover, .bet-settle button:hover {
    border-color: var(--muted);
  }
  .bet-form {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    align-items: center;
    margin-top: 8px;
  }
  .bet-market {
    margin: 0;
    width: 100%;
    color: var(--muted);
    font-size: 0.85rem;
  }
  .bet-market strong { color: var(--text); }
  .bet-odd input {
    width: 5.5rem;
    margin-left: 6px;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 4px 8px;
    font: inherit;
  }
  .bet-stake { color: var(--muted); font-size: 0.82rem; }
  .bet-badge {
    display: inline-block;
    color: var(--live);
    font-size: 0.82rem;
    font-weight: 650;
    margin-right: 10px;
  }
  .bet-settle { display: inline-flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .day-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin: 0 0 18px;
  }
  .day-bar a, .day-bar button {
    color: var(--text);
    text-decoration: none;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 6px 12px;
    font: inherit;
    cursor: pointer;
  }
  .day-bar a:hover, .day-bar button:hover { border-color: var(--muted); }
  .day-bar input[type="date"] {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 6px 8px;
    font: inherit;
  }
  .day-error { color: #f85149; margin: 0 0 12px; }
  .kickoff {
    color: var(--muted);
    margin-right: 6px;
    font-variant-numeric: tabular-nums;
  }
"""

_BETS_CSS = """
  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  .metrics div {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 14px;
  }
  .metrics strong {
    display: block;
    font-size: 1.15rem;
    font-variant-numeric: tabular-nums;
  }
  .metrics span {
    color: var(--muted);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: var(--card);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  th, td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    white-space: nowrap;
  }
  th {
    color: var(--muted);
    font-weight: 600;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.won td:nth-child(7) { color: var(--live); }
  tr.lost td:nth-child(7) { color: var(--red); }
  tr.void td:nth-child(7) { color: var(--muted); }
  tr.open td:nth-child(7) { color: #58a6ff; }
  .empty-row { text-align: center; color: var(--muted); padding: 24px !important; }
  .open-block {
    margin-bottom: 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 16px;
  }
  .open-block h2 {
    border: 0;
    padding: 0 0 8px;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.95rem;
    color: var(--text);
  }
  .open-block ul { margin: 0; padding-left: 18px; }
  .open-block li {
    margin: 6px 0;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .bet-settle.inline { display: inline-flex; gap: 4px; }
  .charts {
    display: grid;
    grid-template-columns: 1fr;
    gap: 16px;
    margin-top: 20px;
  }
  @media (min-width: 960px) {
    .charts { grid-template-columns: 1fr 1fr; }
  }
  .chart-card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 14px 8px;
  }
  .chart-card h2 {
    margin: 0 0 8px;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text);
  }
  .chart-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 14px;
    margin: 0 0 8px;
    font-size: 0.72rem;
    color: var(--muted);
  }
  .chart-legend span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .chart-legend i {
    width: 12px;
    height: 3px;
    border-radius: 1px;
    display: inline-block;
  }
  .chart-empty {
    color: var(--muted);
    font-size: 0.85rem;
    padding: 24px 8px;
    text-align: center;
  }
  .chart-card svg {
    width: 100%;
    height: auto;
    display: block;
  }
"""
