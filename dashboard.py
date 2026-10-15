"""
Dashboard web server — port 80.

Antigravity / API Control Panel (port 8080):
  - Direct selection of Active Profile (no round-robin; selected account responds directly).
  - Reasoning effort selection for Gemini 3.8 Flash: Low, Medium, High.
  - Real-time token consumption tracking (Hourly, Daily, Monthly, Account, Recent Requests).
  - Real-time Google account quota monitoring via AGY CLI.
  - Privacy Mode toggle to mask sensitive profile emails.
"""

import json
import os
import time
import urllib.error
import urllib.request
from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8080")

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AGY — Control Panel & Google Quotas</title>
  <style>
    :root {
      --bg: #0b0d13;
      --surface: #141722;
      --surface2: #1e2233;
      --border: #282d42;
      --accent: #3b82f6;
      --accent2: #60a5fa;
      --green: #10b981;
      --red: #ef4444;
      --yellow: #f59e0b;
      --purple: #8b5cf6;
      --text: #f3f4f6;
      --muted: #94a3b8;
      --radius: 12px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      min-height: 100vh;
      padding: 24px 20px;
    }

    /* ── HEADER ── */
    header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 16px; margin-bottom: 28px;
    }
    .logo { display: flex; align-items: center; gap: 14px; }
    .logo-icon {
      width: 44px; height: 44px;
      background: linear-gradient(135deg, #3b82f6, #10b981, #f59e0b, #ef4444);
      border-radius: 12px;
      display: flex; align-items: center; justify-content: center; font-size: 22px;
      box-shadow: 0 4px 12px rgba(59, 130, 246, 0.25);
    }
    h1 { font-size: 1.6rem; font-weight: 800; letter-spacing: -0.02em; }
    .subtitle { color: var(--muted); font-size: 0.85rem; margin-top: 3px; }

    .header-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .last-update {
      font-size: 0.8rem; color: var(--muted);
      background: var(--surface); padding: 7px 14px;
      border-radius: 20px; border: 1px solid var(--border);
    }
    .btn-privacy {
      background: var(--surface); color: var(--muted);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 8px 14px; font-size: 0.82rem; font-weight: 600;
      cursor: pointer; display: flex; align-items: center; gap: 6px;
      transition: all .2s;
    }
    .btn-privacy:hover { color: var(--text); border-color: var(--accent2); }
    .btn-privacy.active {
      color: var(--accent2); border-color: rgba(59, 130, 246, 0.4);
      background: rgba(59, 130, 246, 0.1);
    }
    .btn-refresh {
      background: var(--accent); color: #fff; border: none;
      padding: 10px 20px; border-radius: 8px; cursor: pointer;
      font-size: 0.9rem; font-weight: 600;
      display: flex; align-items: center; gap: 8px;
      transition: all .2s;
    }
    .btn-refresh:hover { background: #2563eb; transform: translateY(-1px); }
    .btn-refresh:active { transform: translateY(0); }
    .btn-refresh.loading .spin { animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .auto-refresh { display: flex; align-items: center; gap: 8px; font-size: .8rem; color: var(--muted); }
    .auto-refresh select {
      background: var(--surface); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 6px 10px; font-size: .8rem; cursor: pointer;
    }

    /* ── CONFIG PANEL (MAIN) ── */
    .config-panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 32px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.2);
    }
    .config-header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 12px;
      margin-bottom: 20px; padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }
    .config-title {
      font-size: 1.15rem; font-weight: 800; display: flex; align-items: center; gap: 8px;
    }
    .status-badge-active {
      font-size: 0.8rem; font-weight: 600;
      background: rgba(16, 185, 129, 0.12);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.3);
      padding: 5px 12px; border-radius: 20px;
    }

    .config-group { margin-bottom: 22px; }
    .config-group:last-child { margin-bottom: 0; }
    .group-label {
      font-size: 0.8rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.05em; color: var(--muted); margin-bottom: 12px;
      display: flex; align-items: center; gap: 6px;
    }

    /* Profile Direct Selector */
    .profile-select-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 14px;
    }
    .profile-select-card {
      background: var(--surface2);
      border: 2px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      display: flex; align-items: center; justify-content: space-between;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .profile-select-card:hover {
      border-color: rgba(96, 165, 250, 0.6);
      transform: translateY(-2px);
    }
    .profile-select-card.active {
      border-color: var(--accent);
      background: rgba(59, 130, 246, 0.08);
      box-shadow: 0 0 16px rgba(59, 130, 246, 0.25);
    }
    .card-left { display: flex; align-items: center; gap: 12px; }
    .p-avatar {
      width: 40px; height: 40px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 1rem; font-weight: 800; color: #fff;
    }
    .p-avatar.p1 { background: linear-gradient(135deg, #4285f4, #34a853); }
    .p-avatar.p2 { background: linear-gradient(135deg, #fbbc05, #ea4335); }
    .p-avatar.p3 { background: linear-gradient(135deg, #9c27b0, #3f51b5); }

    .p-meta-name { font-weight: 700; font-size: 0.95rem; }
    .p-meta-email { font-size: 0.78rem; color: var(--muted); margin-top: 2px; }

    .active-tag {
      font-size: 0.7rem; font-weight: 700;
      padding: 4px 10px; border-radius: 6px;
      text-transform: uppercase; letter-spacing: 0.04em;
    }
    .profile-select-card.active .active-tag {
      background: var(--accent); color: #fff;
    }
    .profile-select-card:not(.active) .active-tag {
      background: var(--surface); color: var(--muted); border: 1px solid var(--border);
    }

    /* Model effort selector */
    .effort-select-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }
    .effort-card {
      background: var(--surface2);
      border: 2px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;
    }
    .effort-card:hover {
      border-color: rgba(96, 165, 250, 0.6);
      transform: translateY(-2px);
    }
    .effort-card.active.low {
      border-color: var(--green);
      background: rgba(16, 185, 129, 0.08);
      box-shadow: 0 0 16px rgba(16, 185, 129, 0.25);
    }
    .effort-card.active.medium {
      border-color: var(--yellow);
      background: rgba(245, 158, 11, 0.08);
      box-shadow: 0 0 16px rgba(245, 158, 11, 0.25);
    }
    .effort-card.active.high {
      border-color: var(--purple);
      background: rgba(139, 92, 246, 0.08);
      box-shadow: 0 0 16px rgba(139, 92, 246, 0.25);
    }
    .effort-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
    .effort-title { font-weight: 700; font-size: 0.95rem; display: flex; align-items: center; gap: 6px; }
    .effort-model { font-family: monospace; font-size: 0.74rem; color: var(--muted); margin-bottom: 8px; }
    .effort-desc { font-size: 0.75rem; color: var(--muted); line-height: 1.4; }

    .effort-tag {
      font-size: 0.68rem; font-weight: 700; padding: 2px 8px; border-radius: 4px;
    }
    .effort-card.active.low .effort-tag { background: var(--green); color: #fff; }
    .effort-card.active.medium .effort-tag { background: var(--yellow); color: #000; }
    .effort-card.active.high .effort-tag { background: var(--purple); color: #fff; }
    .effort-card:not(.active) .effort-tag { background: var(--surface); color: var(--muted); border: 1px solid var(--border); }

    .feedback-toast {
      margin-top: 16px;
      padding: 10px 16px;
      border-radius: 8px;
      font-size: 0.85rem; font-weight: 600;
      display: none; align-items: center; gap: 8px;
    }
    .feedback-toast.show { display: flex; }
    .feedback-toast.success { background: rgba(16, 185, 129, 0.15); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.3); }
    .feedback-toast.error { background: rgba(239, 68, 68, 0.15); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.3); }

    /* ── SUMMARY STATS ── */
    .summary-bar {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px; margin-bottom: 32px;
    }
    .summary-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 18px 20px;
    }
    .summary-label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
    .summary-value { font-size: 1.7rem; font-weight: 800; }
    .c-blue   { color: var(--accent2); }
    .c-green  { color: var(--green); }
    .c-purple { color: var(--purple); }
    .c-red    { color: var(--red); }
    .c-orange { color: var(--yellow); }

    /* ── TOKEN TRACKING SECTION ── */
    .token-section {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 32px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.2);
    }
    .token-grid-cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .token-card {
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: transform .2s, border-color .2s;
    }
    .token-card:hover {
      transform: translateY(-2px);
      border-color: rgba(96, 165, 250, 0.5);
    }
    .token-card-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 12px;
    }
    .token-card-title {
      font-size: 0.85rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.04em; color: var(--muted);
    }
    .token-card-badge {
      font-size: 0.7rem; font-weight: 700; padding: 3px 8px; border-radius: 6px;
      background: rgba(59, 130, 246, 0.15); color: var(--accent2);
      border: 1px solid rgba(59, 130, 246, 0.25);
    }
    .token-card-total {
      font-size: 1.85rem; font-weight: 800; margin-bottom: 12px; font-family: monospace;
    }
    .token-card-detail {
      font-size: 0.78rem; color: var(--muted); display: flex; flex-direction: column; gap: 6px;
      background: var(--surface); padding: 10px 12px; border-radius: 6px; border: 1px solid var(--border);
    }
    .token-detail-row {
      display: flex; justify-content: space-between; align-items: center;
    }
    .token-detail-row span.val {
      font-weight: 700; color: var(--text); font-family: monospace;
    }

    /* Tabs & Tables */
    .token-tabs {
      display: flex; gap: 8px; margin-bottom: 16px; border-bottom: 1px solid var(--border);
      padding-bottom: 12px; flex-wrap: wrap;
    }
    .token-tab-btn {
      background: var(--surface2); color: var(--muted); border: 1px solid var(--border);
      padding: 8px 16px; border-radius: 8px; font-size: 0.82rem; font-weight: 600;
      cursor: pointer; transition: all 0.2s;
    }
    .token-tab-btn:hover {
      color: var(--text); border-color: var(--accent2);
    }
    .token-tab-btn.active {
      background: var(--accent); color: #fff; border-color: var(--accent);
      box-shadow: 0 2px 8px rgba(59, 130, 246, 0.3);
    }

    .token-table-wrap {
      overflow-x: auto;
      max-height: 400px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface2);
    }
    .token-table {
      width: 100%; border-collapse: collapse; font-size: 0.82rem;
    }
    .token-table th {
      background: #191c2b; color: var(--muted); font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.04em; padding: 11px 16px;
      text-align: left; border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 1;
    }
    .token-table td {
      padding: 10px 16px; border-bottom: 1px solid var(--border); color: var(--text);
    }
    .token-table tr:last-child td {
      border-bottom: none;
    }
    .token-table tr:hover td {
      background: rgba(255, 255, 255, 0.03);
    }
    .token-table td.num {
      text-align: right; font-family: monospace; font-size: 0.85rem; font-weight: 600;
    }
    .token-table th.num {
      text-align: right;
    }

    /* ── USAGE MONITOR SECTION ── */
    .section-title {
      font-size: 1.15rem; font-weight: 700; color: var(--text);
      margin-bottom: 16px; display: flex; align-items: center; gap: 10px;
    }
    .section-title .pill {
      font-size: .72rem; background: var(--surface2); border: 1px solid var(--border);
      color: var(--muted); padding: 3px 10px; border-radius: 20px; font-weight: 500;
    }

    .usage-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
      gap: 20px; margin-bottom: 40px;
    }
    .usage-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
      transition: border-color .2s;
    }
    .usage-card.active-card {
      border-color: var(--accent);
      box-shadow: 0 0 16px rgba(59, 130, 246, 0.15);
    }
    .usage-card-header {
      padding: 16px 20px; background: var(--surface2);
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
    }
    .usage-card-left { display: flex; align-items: center; gap: 12px; }
    .usage-avatar {
      width: 38px; height: 38px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: .95rem; font-weight: 800; color: #fff;
    }
    .usage-card-name { font-weight: 700; font-size: .95rem; }
    .usage-card-email { font-size: .75rem; color: var(--muted); }

    .usage-groups { padding: 16px 20px 20px 20px; }
    .usage-group { margin-bottom: 20px; }
    .usage-group:last-child { margin-bottom: 0; }
    .usage-group-name { font-size: .8rem; font-weight: 700; margin-bottom: 4px; display: flex; align-items: center; gap: 6px; }

    .bucket { margin-bottom: 14px; }
    .bucket:last-child { margin-bottom: 0; }
    .bucket-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
    .bucket-name { font-size: .78rem; color: var(--muted); }
    .bucket-pct { font-size: .85rem; font-weight: 700; }

    .bar-bg { height: 9px; background: var(--border); border-radius: 5px; overflow: hidden; margin-bottom: 5px; }
    .bar-fill { height: 100%; border-radius: 5px; transition: width .6s ease; }
    .bar-fill.hi { background: linear-gradient(90deg, #10b981, #059669); }
    .bar-fill.med { background: linear-gradient(90deg, #f59e0b, #d97706); }
    .bar-fill.lo { background: linear-gradient(90deg, #ef4444, #dc2626); }
    .bar-fill.zero { background: var(--border); }

    .bucket-desc { font-size: .72rem; color: var(--muted); line-height: 1.4; }
    .bucket-reset { font-size: .68rem; color: var(--muted); margin-top: 2px; }

    .token-badge {
      font-size: .72rem; padding: 5px 10px; border-radius: 6px; margin-top: 12px;
      display: flex; align-items: center; gap: 6px;
    }
    .token-badge.valid { background: rgba(16, 185, 129, 0.1); color: var(--green); border: 1px solid rgba(16, 185, 129, 0.2); }
    .token-badge.expiring { background: rgba(245, 158, 11, 0.1); color: var(--yellow); border: 1px solid rgba(245, 158, 11, 0.2); }
    .token-badge.expired { background: rgba(239, 68, 68, 0.1); color: var(--red); border: 1px solid rgba(239, 68, 68, 0.2); }

    footer {
      margin-top: 40px; text-align: center;
      font-size: .78rem; color: var(--muted); padding-top: 20px;
      border-top: 1px solid var(--border);
    }
    footer a { color: var(--accent2); text-decoration: none; }
  </style>
</head>
<body>

  <header>
    <div class="logo">
      <div class="logo-icon">⚡</div>
      <div>
        <h1>AGY — Control Panel</h1>
        <div class="subtitle">OpenAI API at <code style="color:var(--accent2)">http://10.0.0.108:8080/v1</code> · Gemini 3.8 Flash</div>
      </div>
    </div>
    <div class="header-actions">
      <button class="btn-privacy active" id="privacyBtn" onclick="togglePrivacy()" title="Toggle sensitive data masking">
        <span id="privacyIcon">🔒</span> <span id="privacyText">Privacy: ON</span>
      </button>
      <div class="auto-refresh">
        Auto refresh:
        <select id="autoRefreshSelect" onchange="setAutoRefresh(this.value)">
          <option value="0">Disabled</option>
          <option value="15">15s</option>
          <option value="30" selected>30s</option>
          <option value="60">1m</option>
        </select>
      </div>
      <div class="last-update" id="lastUpdate">—</div>
      <button class="btn-refresh" id="refreshBtn" onclick="loadAll(true)">
        <span class="spin">↻</span> Refresh
      </button>
    </div>
  </header>

  <!-- ══════════════════════════════════════════════
       CONFIGURATION PANEL (DIRECT ROUTING)
  ══════════════════════════════════════════════ -->
  <div class="config-panel">
    <div class="config-header">
      <div class="config-title">
        ⚙️ API Configuration
      </div>
      <div class="status-badge-active" id="activeStatusBadge">
        Loading status...
      </div>
    </div>

    <!-- 1. Active Account -->
    <div class="config-group">
      <div class="group-label">
        👤 1. Select Active Account (Direct API Routing)
      </div>
      <div class="profile-select-grid" id="profileSelectGrid">
        <!-- Rendered by JS -->
      </div>
    </div>

    <!-- 2. Reasoning Effort (Gemini 3.8 Flash) -->
    <div class="config-group">
      <div class="group-label">
        🧠 2. Select Reasoning Effort — Gemini 3.8 Flash
      </div>
      <div class="effort-select-grid" id="effortSelectGrid">
        <div class="effort-card" id="card-effort-low" onclick="selectEffort('low')">
          <div class="effort-top">
            <span class="effort-title">🟢 Low</span>
            <span class="effort-tag" id="tag-effort-low">SELECT</span>
          </div>
          <div class="effort-model">gemini-3.8-flash-low</div>
          <div class="effort-desc">Lowest latency and near-instant response. Ideal for maximum speed and straightforward tasks.</div>
        </div>

        <div class="effort-card" id="card-effort-medium" onclick="selectEffort('medium')">
          <div class="effort-top">
            <span class="effort-title">🟡 Medium</span>
            <span class="effort-tag" id="tag-effort-medium">SELECT</span>
          </div>
          <div class="effort-model">gemini-3.8-flash-medium</div>
          <div class="effort-desc">Recommended balance between response speed and structured analytical reasoning.</div>
        </div>

        <div class="effort-card" id="card-effort-high" onclick="selectEffort('high')">
          <div class="effort-top">
            <span class="effort-title">🟣 High</span>
            <span class="effort-tag" id="tag-effort-high">SELECT</span>
          </div>
          <div class="effort-model">gemini-3.8-flash-high</div>
          <div class="effort-desc">Deep and reflective reasoning. Ideal for complex coding, architectural design, and subtle logic.</div>
        </div>
      </div>
    </div>

    <div class="feedback-toast" id="configToast"></div>
  </div>

  <!-- Summary Stats -->
  <div class="summary-bar">
    <div class="summary-card">
      <div class="summary-label">Total Requests</div>
      <div class="summary-value c-blue" id="totalRequests">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Input Tokens</div>
      <div class="summary-value c-green" id="totalTokensIn">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Output Tokens</div>
      <div class="summary-value c-purple" id="totalTokensOut">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">API Errors</div>
      <div class="summary-value c-red" id="totalErrors">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Active Profile</div>
      <div class="summary-value c-orange" id="activeProfileSummary">—</div>
    </div>
  </div>

  <!-- ══════════════════════════════════════════════
       ACCUMULATED TOKEN USAGE (HOUR, DAY, MONTH)
  ══════════════════════════════════════════════ -->
  <div class="token-section">
    <div class="config-header">
      <div class="config-title">
        📈 Accumulated Token Usage
      </div>
      <div class="status-badge-active" id="tokenLastUpdated">
        Checking tokens...
      </div>
    </div>

    <!-- 4 Metric Cards -->
    <div class="token-grid-cards">
      <!-- This Hour -->
      <div class="token-card">
        <div class="token-card-header">
          <span class="token-card-title">⏰ This Hour</span>
          <span class="token-card-badge" id="cardHourPeriod">—</span>
        </div>
        <div class="token-card-total c-blue" id="cardHourTotal">0</div>
        <div class="token-card-detail">
          <div class="token-detail-row"><span>Input (Prompt):</span> <span class="val" id="cardHourIn">0</span></div>
          <div class="token-detail-row"><span>Output (Completion):</span> <span class="val" id="cardHourOut">0</span></div>
          <div class="token-detail-row"><span>Requests:</span> <span class="val" id="cardHourReqs">0</span></div>
        </div>
      </div>

      <!-- Today -->
      <div class="token-card">
        <div class="token-card-header">
          <span class="token-card-title">📅 Today</span>
          <span class="token-card-badge" id="cardDayPeriod">—</span>
        </div>
        <div class="token-card-total c-green" id="cardDayTotal">0</div>
        <div class="token-card-detail">
          <div class="token-detail-row"><span>Input (Prompt):</span> <span class="val" id="cardDayIn">0</span></div>
          <div class="token-detail-row"><span>Output (Completion):</span> <span class="val" id="cardDayOut">0</span></div>
          <div class="token-detail-row"><span>Requests:</span> <span class="val" id="cardDayReqs">0</span></div>
        </div>
      </div>

      <!-- This Month -->
      <div class="token-card">
        <div class="token-card-header">
          <span class="token-card-title">🗓️ This Month</span>
          <span class="token-card-badge" id="cardMonthPeriod">—</span>
        </div>
        <div class="token-card-total c-purple" id="cardMonthTotal">0</div>
        <div class="token-card-detail">
          <div class="token-detail-row"><span>Input (Prompt):</span> <span class="val" id="cardMonthIn">0</span></div>
          <div class="token-detail-row"><span>Output (Completion):</span> <span class="val" id="cardMonthOut">0</span></div>
          <div class="token-detail-row"><span>Requests:</span> <span class="val" id="cardMonthReqs">0</span></div>
        </div>
      </div>

      <!-- All-Time Total -->
      <div class="token-card">
        <div class="token-card-header">
          <span class="token-card-title">🌐 All-Time Total</span>
          <span class="token-card-badge" style="background:rgba(245,158,11,0.15);color:var(--yellow);border-color:rgba(245,158,11,0.3)">TOTAL</span>
        </div>
        <div class="token-card-total c-orange" id="cardAllTotal">0</div>
        <div class="token-card-detail">
          <div class="token-detail-row"><span>Input (Prompt):</span> <span class="val" id="cardAllIn">0</span></div>
          <div class="token-detail-row"><span>Output (Completion):</span> <span class="val" id="cardAllOut">0</span></div>
          <div class="token-detail-row"><span>Requests:</span> <span class="val" id="cardAllReqs">0</span></div>
        </div>
      </div>
    </div>

    <!-- Detail Tabs -->
    <div class="token-tabs">
      <button class="token-tab-btn active" id="tabBtn-hour" onclick="switchTokenTab('hour')">⏰ Hourly History</button>
      <button class="token-tab-btn" id="tabBtn-day" onclick="switchTokenTab('day')">📅 Daily History</button>
      <button class="token-tab-btn" id="tabBtn-month" onclick="switchTokenTab('month')">🗓️ Monthly History</button>
      <button class="token-tab-btn" id="tabBtn-profile" onclick="switchTokenTab('profile')">👤 Usage by Account</button>
      <button class="token-tab-btn" id="tabBtn-recent" onclick="switchTokenTab('recent')">⚡ Recent Requests</button>
    </div>

    <!-- Selected Tab Table -->
    <div class="token-table-wrap" id="tokenTableWrap">
      <div style="padding:20px;text-align:center;color:var(--muted)">Loading consumption data...</div>
    </div>
  </div>

  <!-- Quota / Usage Section -->
  <div class="section-title">
    📊 Google Accounts Quota Monitor
    <span class="pill" id="usageCachedLabel">Checking...</span>
  </div>
  <div class="usage-grid" id="usageGrid">
    <!-- Populated by JS -->
  </div>

  <footer>
    Antigravity API Service &bull; Endpoints: <code>/v1/chat/completions</code> &bull; <code>/v1/models</code> &bull; <code>/v1/health</code>
  </footer>

  <script>
    let privacyMode = true; // Enabled by default to protect sensitive emails
    let currentConfig = {
      active_profile: 'p1',
      active_effort: 'low',
      active_model: 'gemini-3.8-flash-low'
    };
    let profilesList = [];
    let autoRefreshTimer = null;

    function togglePrivacy() {
      privacyMode = !privacyMode;
      const btn = document.getElementById('privacyBtn');
      const text = document.getElementById('privacyText');
      if (privacyMode) {
        btn.classList.add('active');
        text.textContent = 'Privacy: ON';
      } else {
        btn.classList.remove('active');
        text.textContent = 'Privacy: OFF';
      }
      renderConfigUI();
      if (tokenData) renderTokenTable();
      loadUsage(false);
    }

    function maskEmail(email) {
      if (!email) return '';
      if (!privacyMode) return email;
      const parts = email.split('@');
      if (parts.length < 2) return '••••••••';
      const user = parts[0];
      const domain = parts[1];
      if (user.length <= 3) return '••••@' + domain;
      const masked = user.substring(0, 2) + '••••' + user.substring(user.length - 1);
      return masked + '@' + domain;
    }

    function formatProfileName(name, id) {
      if (!name) return `Profile ${id ? id.toUpperCase() : ''}`;
      return name.replace(/^Perfil\\s+/i, 'Profile ');
    }

    function fmt(n) {
      if (n == null) return '—';
      if (n >= 1e6) return (n/1e6).toFixed(1)+'M';
      if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
      return String(n);
    }

    function pct(frac) {
      if (frac == null) return null;
      return Math.round(frac * 100);
    }

    function barClass(p) {
      if (p == null || p === 0) return 'zero';
      if (p >= 50) return 'hi';
      if (p >= 20) return 'med';
      return 'lo';
    }

    function resetLabel(isoStr) {
      if (!isoStr) return '';
      const d = new Date(isoStr);
      const now = new Date();
      const diffMs = d - now;
      if (diffMs <= 0) return 'Reset imminent';
      const totalMin = Math.floor(diffMs / 60000);
      const days = Math.floor(totalMin / 1440);
      const hrs  = Math.floor((totalMin % 1440) / 60);
      const mins = totalMin % 60;
      let parts = [];
      if (days) parts.push(`${days}d`);
      if (hrs)  parts.push(`${hrs}h`);
      if (mins && !days) parts.push(`${mins}m`);
      return `Reset in ${parts.join(' ')}`;
    }

    function showToast(msg, isSuccess = true) {
      const toast = document.getElementById('configToast');
      toast.className = `feedback-toast show ${isSuccess ? 'success' : 'error'}`;
      toast.innerHTML = (isSuccess ? '✓ ' : '✗ ') + msg;
      setTimeout(() => {
        toast.className = 'feedback-toast';
      }, 4000);
    }

    /* ── LOAD CONFIG ── */
    async function loadConfig() {
      try {
        const resp = await fetch('/api/config');
        const data = await resp.json();
        currentConfig = data;
        renderConfigUI();
      } catch (e) {
        console.error('Error loading config:', e);
      }
    }

    /* ── RENDER CONFIG UI ── */
    function renderConfigUI() {
      const activePid = currentConfig.active_profile || 'p1';
      const activeEff = currentConfig.active_effort || 'low';

      const effortNames = {
        low: 'Low',
        medium: 'Medium',
        high: 'High',
      };

      // Get profile meta dynamically from profilesList or defaults
      const pMap = {};
      profilesList.forEach(p => {
        pMap[p.id] = {
          name: formatProfileName(p.name, p.id),
          email: p.email
        };
      });

      const activeProfileMeta = pMap[activePid] || {
        name: `Profile ${activePid.toUpperCase()}`,
        email: ''
      };

      // Header Badge
      document.getElementById('activeStatusBadge').innerHTML =
        `★ Active API: <b>${activeProfileMeta.name}</b> &bull; <b>${effortNames[activeEff] || activeEff}</b>`;
      document.getElementById('activeProfileSummary').textContent =
        `${activeProfileMeta.name} (${activeEff.toUpperCase()})`;

      // Profile grid
      const pGrid = document.getElementById('profileSelectGrid');
      const profileIds = profilesList.length ? profilesList.map(p => p.id) : ['p1', 'p2', 'p3'];

      pGrid.innerHTML = profileIds.map(pid => {
        const isActive = pid === activePid;
        const meta = pMap[pid] || { name: `Profile ${pid.toUpperCase()}`, email: '' };
        return `
          <div class="profile-select-card ${isActive ? 'active' : ''}" onclick="selectProfile('${pid}')">
            <div class="card-left">
              <div class="p-avatar ${pid}">${pid.toUpperCase()}</div>
              <div>
                <div class="p-meta-name">${meta.name}</div>
                <div class="p-meta-email">${maskEmail(meta.email)}</div>
              </div>
            </div>
            <div class="active-tag">${isActive ? '★ ACTIVE' : 'ACTIVATE'}</div>
          </div>
        `;
      }).join('');

      // Effort cards
      ['low', 'medium', 'high'].forEach(eff => {
        const card = document.getElementById(`card-effort-${eff}`);
        const tag = document.getElementById(`tag-effort-${eff}`);
        const isEffActive = eff === activeEff;
        card.className = `effort-card ${isEffActive ? 'active ' + eff : ''}`;
        tag.textContent = isEffActive ? '★ ACTIVE' : 'SELECT';
      });
    }

    /* ── SAVE CONFIG ACTIONS ── */
    async function selectProfile(pid) {
      if (currentConfig.active_profile === pid) return;
      try {
        const resp = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ active_profile: pid })
        });
        const res = await resp.json();
        if (res.ok) {
          currentConfig.active_profile = pid;
          renderConfigUI();
          showToast(`Account activated: Profile ${pid.toUpperCase()} is now serving all API requests.`);
          loadProfiles();
        } else {
          showToast('Error switching profile: ' + (res.error || 'Failed'), false);
        }
      } catch (e) {
        showToast('Connection error saving profile: ' + e.message, false);
      }
    }

    async function selectEffort(effort) {
      if (currentConfig.active_effort === effort) return;
      try {
        const resp = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ active_effort: effort })
        });
        const res = await resp.json();
        if (res.ok) {
          currentConfig.active_effort = effort;
          currentConfig.active_model = `gemini-3.8-flash-${effort}`;
          renderConfigUI();
          showToast(`Reasoning effort changed to Gemini 3.8 Flash (${effort.toUpperCase()}).`);
        } else {
          showToast('Error switching reasoning effort: ' + (res.error || 'Failed'), false);
        }
      } catch (e) {
        showToast('Connection error saving model: ' + e.message, false);
      }
    }

    /* ── LOAD PROFILES STATS ── */
    async function loadProfiles() {
      try {
        const resp = await fetch('/api/profiles');
        const data = await resp.json();
        profilesList = data.profiles || [];
        const totalReqs = profilesList.reduce((a,p) => a + (p.stats.requests||0), 0);
        const totalIn   = profilesList.reduce((a,p) => a + (p.stats.tokens_in||0), 0);
        const totalOut  = profilesList.reduce((a,p) => a + (p.stats.tokens_out||0), 0);
        const totalErr  = profilesList.reduce((a,p) => a + (p.stats.errors||0), 0);

        document.getElementById('totalRequests').textContent = fmt(totalReqs);
        document.getElementById('totalTokensIn').textContent = fmt(totalIn);
        document.getElementById('totalTokensOut').textContent = fmt(totalOut);
        document.getElementById('totalErrors').textContent = fmt(totalErr);

        renderConfigUI();
      } catch (e) {
        console.error('Error loading profiles:', e);
      }
    }

    /* ── LOAD USAGE QUOTAS ── */
    async function loadUsage(force = false) {
      const grid = document.getElementById('usageGrid');
      const cachedLabel = document.getElementById('usageCachedLabel');
      try {
        const resp = await fetch(`/api/usage?force=${force}`);
        const data = await resp.json();
        const items = data.profiles_usage || [];

        cachedLabel.textContent = `Updated at ${new Date().toLocaleTimeString('en-US')}`;

        grid.innerHTML = items.map(p => {
          const pid = p.id;
          const isActive = pid === currentConfig.active_profile;
          const groups = p.groups || [];

          const groupsHtml = groups.map(g => {
            const bucketsHtml = (g.buckets || []).map(b => {
              const pVal = pct(b.remaining_fraction);
              const bCls = barClass(pVal);
              const rLabel = resetLabel(b.reset_time);
              return `
                <div class="bucket">
                  <div class="bucket-header">
                    <span class="bucket-name">${b.name}</span>
                    <span class="bucket-pct" style="color:var(--${bCls === 'hi' ? 'green' : bCls === 'med' ? 'yellow' : 'red'})">
                      ${pVal != null ? pVal + '%' : '—'}
                    </span>
                  </div>
                  <div class="bar-bg">
                    <div class="bar-fill ${bCls}" style="width:${pVal != null ? pVal : 0}%"></div>
                  </div>
                  <div class="bucket-desc">${b.description || ''}</div>
                  ${rLabel ? `<div class="bucket-reset">${rLabel}</div>` : ''}
                </div>
              `;
            }).join('');

            return `
              <div class="usage-group">
                <div class="usage-group-name">
                  ${g.name === 'Gemini Models' ? '🔵' : '🟣'} ${g.name}
                </div>
                ${bucketsHtml}
              </div>
            `;
          }).join('');

          return `
            <div class="usage-card ${isActive ? 'active-card' : ''}">
              <div class="usage-card-header">
                <div class="usage-card-left">
                  <div class="usage-avatar p-avatar ${pid}">${pid.toUpperCase()}</div>
                  <div>
                    <div class="usage-card-name">${formatProfileName(p.name, pid)} ${isActive ? '<span style="color:var(--green);font-size:.7rem">(ACTIVE IN API)</span>' : ''}</div>
                    <div class="usage-card-email">${maskEmail(p.email)}</div>
                  </div>
                </div>
                ${!isActive ? `<button class="btn-refresh" style="padding:6px 12px;font-size:.75rem;" onclick="selectProfile('${pid}')">Activate</button>` : '<span style="color:var(--green);font-size:.8rem;font-weight:700">✓ In Use</span>'}
              </div>
              <div class="usage-groups">
                ${groupsHtml || '<div style="padding:20px;color:var(--muted)">No quota data available</div>'}
              </div>
            </div>
          `;
        }).join('');

      } catch (e) {
        console.error('Error loading usage:', e);
      }
    }

    /* ── TOKEN TRACKING LOGIC ── */
    let currentTokenTab = 'hour';
    let tokenData = null;

    function fmtNum(n) {
      if (n == null || isNaN(n)) return '0';
      return Number(n).toLocaleString('en-US');
    }

    async function loadTokenUsage() {
      const updatedBadge = document.getElementById('tokenLastUpdated');
      try {
        const resp = await fetch('/api/token_usage');
        const data = await resp.json();
        if (!data || !data.ok) return;
        tokenData = data;

        updatedBadge.innerHTML = `✓ Updated: ${new Date().toLocaleTimeString('en-US')}`;

        // 1. Hour card
        const h = data.current_hour || {};
        document.getElementById('cardHourTotal').textContent = fmtNum(h.total_tokens);
        document.getElementById('cardHourIn').textContent = fmtNum(h.prompt_tokens);
        document.getElementById('cardHourOut').textContent = fmtNum(h.completion_tokens);
        document.getElementById('cardHourReqs').textContent = fmtNum(h.requests);
        document.getElementById('cardHourPeriod').textContent = h.period ? h.period.replace(' UTC', '') : 'Now';

        // 2. Day card
        const d = data.today || {};
        document.getElementById('cardDayTotal').textContent = fmtNum(d.total_tokens);
        document.getElementById('cardDayIn').textContent = fmtNum(d.prompt_tokens);
        document.getElementById('cardDayOut').textContent = fmtNum(d.completion_tokens);
        document.getElementById('cardDayReqs').textContent = fmtNum(d.requests);
        document.getElementById('cardDayPeriod').textContent = d.period || 'Today';

        // 3. Month card
        const m = data.this_month || {};
        document.getElementById('cardMonthTotal').textContent = fmtNum(m.total_tokens);
        document.getElementById('cardMonthIn').textContent = fmtNum(m.prompt_tokens);
        document.getElementById('cardMonthOut').textContent = fmtNum(m.completion_tokens);
        document.getElementById('cardMonthReqs').textContent = fmtNum(m.requests);
        document.getElementById('cardMonthPeriod').textContent = m.period || 'Month';

        // 4. All-time card
        const at = data.all_time || {};
        document.getElementById('cardAllTotal').textContent = fmtNum(at.total_tokens);
        document.getElementById('cardAllIn').textContent = fmtNum(at.prompt_tokens);
        document.getElementById('cardAllOut').textContent = fmtNum(at.completion_tokens);
        document.getElementById('cardAllReqs').textContent = fmtNum(at.requests);

        // Keep Summary Bar in sync with accumulated token_tracker numbers
        if (at.requests) {
          document.getElementById('totalRequests').textContent = fmt(at.requests);
          document.getElementById('totalTokensIn').textContent = fmt(at.prompt_tokens);
          document.getElementById('totalTokensOut').textContent = fmt(at.completion_tokens);
        }

        renderTokenTable();
      } catch (e) {
        console.error('Error loading token_usage:', e);
        if (updatedBadge) updatedBadge.textContent = 'Error querying';
      }
    }

    function switchTokenTab(tab) {
      currentTokenTab = tab;
      ['hour', 'day', 'month', 'profile', 'recent'].forEach(t => {
        const btn = document.getElementById(`tabBtn-${t}`);
        if (btn) {
          if (t === tab) btn.classList.add('active');
          else btn.classList.remove('active');
        }
      });
      renderTokenTable();
    }

    function renderTokenTable() {
      const wrap = document.getElementById('tokenTableWrap');
      if (!tokenData) {
        wrap.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No consumption data recorded yet.</div>';
        return;
      }

      if (currentTokenTab === 'hour') {
        const list = tokenData.by_hour_list || [];
        if (!list.length) {
          wrap.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No hourly history recorded.</div>';
          return;
        }
        wrap.innerHTML = `
          <table class="token-table">
            <thead>
              <tr>
                <th>Time (UTC)</th>
                <th class="num">Requests</th>
                <th class="num">Input Tokens</th>
                <th class="num">Output Tokens</th>
                <th class="num">Total Tokens</th>
              </tr>
            </thead>
            <tbody>
              ${list.map(r => `
                <tr>
                  <td><b>${r.period}</b></td>
                  <td class="num">${fmtNum(r.requests)}</td>
                  <td class="num" style="color:var(--green)">${fmtNum(r.prompt_tokens)}</td>
                  <td class="num" style="color:var(--purple)">${fmtNum(r.completion_tokens)}</td>
                  <td class="num c-blue"><b>${fmtNum(r.total_tokens)}</b></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      } else if (currentTokenTab === 'day') {
        const list = tokenData.by_day_list || [];
        if (!list.length) {
          wrap.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No daily history recorded.</div>';
          return;
        }
        wrap.innerHTML = `
          <table class="token-table">
            <thead>
              <tr>
                <th>Date</th>
                <th class="num">Requests</th>
                <th class="num">Input Tokens</th>
                <th class="num">Output Tokens</th>
                <th class="num">Total Tokens</th>
              </tr>
            </thead>
            <tbody>
              ${list.map(r => `
                <tr>
                  <td><b>${r.period}</b></td>
                  <td class="num">${fmtNum(r.requests)}</td>
                  <td class="num" style="color:var(--green)">${fmtNum(r.prompt_tokens)}</td>
                  <td class="num" style="color:var(--purple)">${fmtNum(r.completion_tokens)}</td>
                  <td class="num c-green"><b>${fmtNum(r.total_tokens)}</b></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      } else if (currentTokenTab === 'month') {
        const list = tokenData.by_month_list || [];
        if (!list.length) {
          wrap.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No monthly history recorded.</div>';
          return;
        }
        wrap.innerHTML = `
          <table class="token-table">
            <thead>
              <tr>
                <th>Month</th>
                <th class="num">Requests</th>
                <th class="num">Input Tokens</th>
                <th class="num">Output Tokens</th>
                <th class="num">Total Tokens</th>
              </tr>
            </thead>
            <tbody>
              ${list.map(r => `
                <tr>
                  <td><b>${r.period}</b></td>
                  <td class="num">${fmtNum(r.requests)}</td>
                  <td class="num" style="color:var(--green)">${fmtNum(r.prompt_tokens)}</td>
                  <td class="num" style="color:var(--purple)">${fmtNum(r.completion_tokens)}</td>
                  <td class="num c-purple"><b>${fmtNum(r.total_tokens)}</b></td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      } else if (currentTokenTab === 'profile') {
        const prof = tokenData.by_profile || {};
        const pids = profilesList.length ? profilesList.map(p => p.id) : ['p1', 'p2', 'p3'];
        const pMap = {};
        profilesList.forEach(p => { pMap[p.id] = p; });

        wrap.innerHTML = `
          <table class="token-table">
            <thead>
              <tr>
                <th>Account / Profile</th>
                <th>Email</th>
                <th class="num">Requests</th>
                <th class="num">Input Tokens</th>
                <th class="num">Output Tokens</th>
                <th class="num">Total Tokens</th>
              </tr>
            </thead>
            <tbody>
              ${pids.map(pid => {
                const p = prof[pid] || {};
                const m = pMap[pid] || { name: `Profile ${pid.toUpperCase()}`, email: '' };
                return `
                  <tr>
                    <td><b>${formatProfileName(m.name, pid)} (${pid.toUpperCase()})</b></td>
                    <td style="color:var(--muted);font-size:0.8rem">${maskEmail(m.email)}</td>
                    <td class="num">${fmtNum(p.requests)}</td>
                    <td class="num" style="color:var(--green)">${fmtNum(p.prompt_tokens)}</td>
                    <td class="num" style="color:var(--purple)">${fmtNum(p.completion_tokens)}</td>
                    <td class="num c-orange"><b>${fmtNum(p.total_tokens)}</b></td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
      } else if (currentTokenTab === 'recent') {
        const recent = tokenData.recent_requests || [];
        if (!recent.length) {
          wrap.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">No recent requests.</div>';
          return;
        }
        wrap.innerHTML = `
          <table class="token-table">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Account</th>
                <th>Model</th>
                <th class="num">Input</th>
                <th class="num">Output</th>
                <th class="num">Total</th>
              </tr>
            </thead>
            <tbody>
              ${recent.map(r => {
                const dt = new Date(r.timestamp);
                const timeStr = !isNaN(dt.getTime()) ? dt.toLocaleTimeString('en-US') : r.timestamp;
                return `
                  <tr>
                    <td>${timeStr}</td>
                    <td><b>${(r.profile || 'P1').toUpperCase()}</b></td>
                    <td style="font-family:monospace;font-size:0.75rem">${r.model || 'gemini-3.8-flash'}</td>
                    <td class="num" style="color:var(--green)">${fmtNum(r.prompt_tokens)}</td>
                    <td class="num" style="color:var(--purple)">${fmtNum(r.completion_tokens)}</td>
                    <td class="num"><b>${fmtNum(r.total_tokens)}</b></td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
      }
    }

    async function loadAll(force = false) {
      const btn = document.getElementById('refreshBtn');
      btn.classList.add('loading');
      document.getElementById('lastUpdate').textContent = 'Updating...';
      await Promise.all([loadConfig(), loadProfiles(), loadUsage(force), loadTokenUsage()]);
      document.getElementById('lastUpdate').textContent = `Updated at ${new Date().toLocaleTimeString('en-US')}`;
      btn.classList.remove('loading');
    }

    function setAutoRefresh(sec) {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      const s = parseInt(sec);
      if (s > 0) {
        autoRefreshTimer = setInterval(() => loadAll(false), s * 1000);
      }
    }

    // Initial load
    loadAll(false);
    setAutoRefresh(30);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Proxy Helpers
# ---------------------------------------------------------------------------

def _proxy(endpoint: str, query: str = ""):
    url = f"{API_BASE}{endpoint}{query}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        return None, err_body.get("error", str(e))
    except Exception as e:
        return None, str(e)


def _proxy_post(endpoint: str, body: dict):
    url = f"{API_BASE}{endpoint}"
    try:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        return None, err_body.get("error", str(e))
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/config", methods=["GET"])
def api_get_config():
    data, err = _proxy("/v1/config")
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/config", methods=["POST"])
def api_set_config():
    body = request.get_json(force=True, silent=True) or {}
    data, err = _proxy_post("/v1/config", body)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/profiles")
def api_profiles():
    data, err = _proxy("/v1/profiles")
    if err:
        return jsonify({"error": err, "profiles": []}), 502
    return jsonify(data)


@app.route("/api/usage")
def api_usage():
    force = request.args.get("force", "false")
    data, err = _proxy("/v1/profiles/usage", f"?force={force}")
    if err:
        return jsonify({"error": err, "profiles_usage": []}), 502
    return jsonify(data)


@app.route("/api/token_usage")
def api_token_usage():
    data, err = _proxy("/v1/token_usage")
    if err:
        return jsonify({"error": err, "ok": False}), 502
    return jsonify(data)


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


if __name__ == "__main__":
    port = int(os.getenv("PORT", "80"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    print(f"Starting AGY Dashboard on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
