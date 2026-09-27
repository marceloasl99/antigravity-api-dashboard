"""
Dashboard web server — porta 80.

Exibe o uso das 3 contas Google (perfis AGY) em tempo real,
incluindo a seção /usage idêntica ao AGY CLI com todos os detalhes,
E uma seção de gerenciamento para selecionar quais perfis estão
disponíveis para uso na API (porta 8080).

Depende do api_service_multi.py rodando na porta 8080.
"""

import json
import os
import time
import urllib.request
import urllib.error

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8080")

# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AGY — Dashboard de Contas Google</title>
  <style>
    :root {
      --bg: #0f1117;
      --surface: #1a1d27;
      --surface2: #252836;
      --border: #2e3250;
      --accent: #4f8ef7;
      --accent2: #38bdf8;
      --green: #22c55e;
      --red: #ef4444;
      --orange: #f97316;
      --yellow: #eab308;
      --purple: #a78bfa;
      --text: #e2e8f0;
      --muted: #64748b;
      --radius: 12px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
      min-height: 100vh;
      padding: 24px 16px;
    }

    /* ── HEADER ── */
    header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 12px; margin-bottom: 28px;
    }
    .logo { display: flex; align-items: center; gap: 12px; }
    .logo-icon {
      width: 40px; height: 40px;
      background: linear-gradient(135deg, #4285f4, #34a853, #fbbc05, #ea4335);
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center; font-size: 20px;
    }
    h1 { font-size: 1.5rem; font-weight: 700; }
    .subtitle { color: var(--muted); font-size: 0.875rem; margin-top: 2px; }

    .header-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .last-update {
      font-size: 0.8rem; color: var(--muted);
      background: var(--surface); padding: 6px 12px;
      border-radius: 20px; border: 1px solid var(--border);
    }
    .btn-refresh {
      background: var(--accent); color: #fff; border: none;
      padding: 10px 20px; border-radius: 8px; cursor: pointer;
      font-size: 0.9rem; font-weight: 600;
      display: flex; align-items: center; gap: 8px;
      transition: background .2s, transform .1s;
    }
    .btn-refresh:hover { background: #3b7de8; }
    .btn-refresh:active { transform: scale(.97); }
    .btn-refresh:disabled { background: var(--muted); cursor: not-allowed; }
    .btn-refresh .spin { transition: transform .4s; display: inline-block; }
    .btn-refresh.loading .spin { animation: spin .8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .auto-refresh { display: flex; align-items: center; gap: 8px; font-size: .8rem; color: var(--muted); }
    .auto-refresh select {
      background: var(--surface); color: var(--text);
      border: 1px solid var(--border); border-radius: 6px;
      padding: 4px 8px; font-size: .8rem; cursor: pointer;
    }

    /* ── SECTION TITLE ── */
    .section-title {
      font-size: 1rem; font-weight: 700; color: var(--text);
      margin-bottom: 14px; display: flex; align-items: center; gap: 8px;
    }
    .section-title .pill {
      font-size: .7rem; background: var(--surface2); border: 1px solid var(--border);
      color: var(--muted); padding: 2px 8px; border-radius: 20px; font-weight: 400;
    }

    /* ── SUMMARY BAR ── */
    .summary-bar {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
      gap: 12px; margin-bottom: 28px;
    }
    .summary-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 16px 20px;
    }
    .summary-label { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; margin-bottom: 6px; }
    .summary-value { font-size: 1.75rem; font-weight: 700; }
    .c-blue   { color: var(--accent); }
    .c-green  { color: var(--green); }
    .c-purple { color: var(--purple); }
    .c-red    { color: var(--red); }
    .c-orange { color: var(--orange); }

    /* ── PROFILE MANAGER ── */
    .manager-section {
      margin-bottom: 36px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
    }
    .manager-header {
      display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 12px;
      padding: 16px 20px;
      background: var(--surface2);
      border-bottom: 1px solid var(--border);
    }
    .manager-header-left {
      display: flex; align-items: center; gap: 10px;
    }
    .manager-title { font-weight: 700; font-size: 1rem; }
    .manager-subtitle { font-size: .78rem; color: var(--muted); margin-top: 2px; }
    .manager-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .btn-sm {
      padding: 7px 14px; border-radius: 7px; border: 1px solid var(--border);
      font-size: .8rem; font-weight: 600; cursor: pointer;
      transition: background .15s, border-color .15s, transform .1s;
    }
    .btn-sm:active { transform: scale(.96); }
    .btn-all  { background: rgba(34,197,94,.12); color: var(--green); border-color: rgba(34,197,94,.3); }
    .btn-all:hover  { background: rgba(34,197,94,.22); }
    .btn-none { background: rgba(239,68,68,.12); color: var(--red); border-color: rgba(239,68,68,.3); }
    .btn-none:hover { background: rgba(239,68,68,.22); }
    .btn-save {
      background: var(--accent); color: #fff; border-color: var(--accent);
    }
    .btn-save:hover { background: #3b7de8; }
    .btn-save:disabled { background: var(--muted); border-color: var(--muted); cursor: not-allowed; opacity: .6; }

    .manager-profiles {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 0;
    }
    .manager-profile-row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 18px 20px;
      border-right: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      transition: background .15s;
      cursor: pointer;
      user-select: none;
    }
    .manager-profile-row:last-child { border-right: none; }
    .manager-profile-row:hover { background: rgba(79,142,247,.05); }
    .manager-profile-row.enabled-row { border-top: 2px solid transparent; }
    .manager-profile-row.enabled-row.p1-active { border-top-color: #4285f4; }
    .manager-profile-row.enabled-row.p2-active { border-top-color: #fbbc05; }
    .manager-profile-row.enabled-row.p3-active { border-top-color: #9c27b0; }

    .manager-profile-info { display: flex; align-items: center; gap: 12px; }
    .mgr-avatar {
      width: 38px; height: 38px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: .9rem; font-weight: 700; flex-shrink: 0;
      transition: opacity .2s;
    }
    .mgr-avatar.p1 { background: linear-gradient(135deg,#4285f4,#34a853); }
    .mgr-avatar.p2 { background: linear-gradient(135deg,#fbbc05,#ea4335); }
    .mgr-avatar.p3 { background: linear-gradient(135deg,#9c27b0,#3f51b5); }
    .mgr-avatar.disabled-avatar { filter: grayscale(1); opacity: .4; }

    .mgr-name { font-weight: 700; font-size: .9rem; transition: color .2s; }
    .mgr-email { font-size: .73rem; color: var(--muted); margin-top: 2px; }
    .mgr-status-tag {
      font-size: .65rem; padding: 2px 7px; border-radius: 4px;
      font-weight: 600; margin-top: 4px; display: inline-block;
      transition: all .2s;
    }
    .mgr-status-tag.tag-on  { background: rgba(34,197,94,.15); color: var(--green); border: 1px solid rgba(34,197,94,.3); }
    .mgr-status-tag.tag-off { background: rgba(100,116,139,.12); color: var(--muted); border: 1px solid var(--border); }

    /* Toggle switch */
    .toggle-wrap { display: flex; align-items: center; gap: 10px; }
    .toggle {
      position: relative; width: 46px; height: 24px;
      flex-shrink: 0;
    }
    .toggle input { opacity: 0; width: 0; height: 0; position: absolute; }
    .toggle-slider {
      position: absolute; inset: 0; border-radius: 24px;
      background: var(--border); cursor: pointer;
      transition: background .25s;
    }
    .toggle-slider::before {
      content: ''; position: absolute;
      width: 18px; height: 18px; border-radius: 50%;
      background: #fff; top: 3px; left: 3px;
      transition: transform .25s;
      box-shadow: 0 1px 3px rgba(0,0,0,.3);
    }
    .toggle input:checked + .toggle-slider { background: var(--green); }
    .toggle input:checked + .toggle-slider::before { transform: translateX(22px); }

    /* Warning bar when 0 profiles enabled */
    .no-profiles-warn {
      margin: 12px 20px;
      background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3);
      border-radius: 8px; padding: 10px 14px;
      color: var(--red); font-size: .82rem;
      display: none; align-items: center; gap: 8px;
    }
    .no-profiles-warn.visible { display: flex; }

    .save-feedback {
      padding: 10px 20px;
      font-size: .82rem; color: var(--green);
      display: none; align-items: center; gap: 6px;
      border-top: 1px solid var(--border);
      background: rgba(34,197,94,.05);
    }
    .save-feedback.visible { display: flex; }
    .save-feedback.err { color: var(--red); }

    /* ── PROFILE CARDS (top section) ── */
    .profiles-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 20px; margin-bottom: 40px;
    }
    .profile-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden; transition: border-color .2s;
    }
    .profile-card:hover { border-color: var(--accent); }
    .card-header {
      padding: 16px 20px; display: flex; align-items: center; gap: 14px;
      border-bottom: 1px solid var(--border); background: var(--surface2);
    }
    .avatar {
      width: 42px; height: 42px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.1rem; font-weight: 700; flex-shrink: 0;
    }
    .avatar.p1 { background: linear-gradient(135deg,#4285f4,#34a853); }
    .avatar.p2 { background: linear-gradient(135deg,#fbbc05,#ea4335); }
    .avatar.p3 { background: linear-gradient(135deg,#9c27b0,#3f51b5); }
    .card-title { flex: 1; min-width: 0; }
    .profile-name  { font-weight: 700; font-size: .95rem; }
    .profile-email { font-size: .78rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .status-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
    .status-dot.ok   { background: var(--green);  box-shadow: 0 0 6px var(--green); }
    .status-dot.warn { background: var(--yellow); }
    .status-dot.err  { background: var(--red); }
    .card-body { padding: 18px 20px; }
    .stat-row {
      display: flex; align-items: center; justify-content: space-between;
      padding: 7px 0; border-bottom: 1px solid var(--border);
    }
    .stat-row:last-child { border-bottom: none; }
    .stat-label { font-size: .8rem; color: var(--muted); display: flex; align-items: center; gap: 5px; }
    .stat-value { font-size: .88rem; font-weight: 600; }
    .sv-blue   { color: var(--accent2); }
    .sv-tok    { color: var(--accent); }
    .sv-err    { color: var(--red); }

    .mini-bar-wrap { margin-top: 14px; }
    .mini-bar-labels { display: flex; justify-content: space-between; font-size: .72rem; color: var(--muted); margin-bottom: 5px; }
    .mini-bar-bg { background: var(--border); height: 5px; border-radius: 3px; overflow: hidden; }
    .mini-bar-fill { height: 100%; border-radius: 3px; transition: width .5s; }
    .mini-bar-fill.p1 { background: linear-gradient(90deg,#4285f4,#34a853); }
    .mini-bar-fill.p2 { background: linear-gradient(90deg,#fbbc05,#ea4335); }
    .mini-bar-fill.p3 { background: linear-gradient(90deg,#9c27b0,#3f51b5); }

    .token-badge {
      margin-top: 12px; font-size: .72rem; padding: 7px 11px;
      border-radius: 6px; display: flex; align-items: center; gap: 6px;
    }
    .token-badge.valid    { background: rgba(34,197,94,.1);  color: var(--green);  border: 1px solid rgba(34,197,94,.2); }
    .token-badge.expiring { background: rgba(234,179,8,.1);  color: var(--yellow); border: 1px solid rgba(234,179,8,.2); }
    .token-badge.expired  { background: rgba(239,68,68,.1);  color: var(--red);    border: 1px solid rgba(239,68,68,.2); }

    /* ── USAGE SECTION ── */
    .usage-section { margin-bottom: 40px; }

    .usage-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
      gap: 20px;
    }

    .usage-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: var(--radius); overflow: hidden;
    }

    .usage-card-header {
      padding: 14px 20px; background: var(--surface2);
      border-bottom: 1px solid var(--border);
      display: flex; align-items: center; gap: 12px;
    }
    .usage-avatar {
      width: 36px; height: 36px; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: .9rem; font-weight: 700; flex-shrink: 0;
    }
    .usage-card-meta { flex: 1; min-width: 0; }
    .usage-card-name  { font-weight: 700; font-size: .9rem; }
    .usage-card-email { font-size: .73rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

    .usage-loading {
      padding: 32px; text-align: center;
      color: var(--muted); font-size: .85rem;
    }
    .usage-spinner {
      display: inline-block; width: 18px; height: 18px;
      border: 2px solid var(--border); border-top-color: var(--accent);
      border-radius: 50%; animation: spin .7s linear infinite;
      margin-right: 8px; vertical-align: middle;
    }

    .usage-desc {
      font-size: .72rem; color: var(--muted);
      padding: 10px 20px 0 20px; line-height: 1.5;
    }

    .usage-groups { padding: 12px 20px 20px 20px; }

    .usage-group { margin-bottom: 22px; }
    .usage-group:last-child { margin-bottom: 0; }

    .usage-group-name {
      font-size: .78rem; font-weight: 700;
      color: var(--text); margin-bottom: 2px;
      display: flex; align-items: center; gap: 6px;
    }
    .usage-group-models { font-size: .68rem; color: var(--muted); margin-bottom: 12px; }

    .bucket { margin-bottom: 14px; }
    .bucket:last-child { margin-bottom: 0; }

    .bucket-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: 4px;
    }
    .bucket-name { font-size: .78rem; color: var(--muted); }
    .bucket-pct  { font-size: .88rem; font-weight: 700; }

    .bar-bg {
      height: 10px; background: var(--border); border-radius: 5px;
      overflow: hidden; margin-bottom: 5px;
    }
    .bar-fill {
      height: 100%; border-radius: 5px;
      transition: width .6s cubic-bezier(.4,0,.2,1);
    }

    /* colour by percentage */
    .bar-fill.hi   { background: linear-gradient(90deg, #22c55e, #16a34a); }
    .bar-fill.med  { background: linear-gradient(90deg, #eab308, #ca8a04); }
    .bar-fill.lo   { background: linear-gradient(90deg, #ef4444, #dc2626); }
    .bar-fill.zero { background: var(--border); }
    .bar-fill.dis  { background: repeating-linear-gradient(45deg,#2e3250,#2e3250 4px,#252836 4px,#252836 8px); }

    .bucket-desc {
      font-size: .71rem; color: var(--muted);
      line-height: 1.45;
    }

    .bucket-reset {
      font-size: .68rem; color: var(--muted);
      margin-top: 2px;
    }

    .tag-disabled {
      font-size: .65rem; background: rgba(100,116,139,.15);
      color: var(--muted); border: 1px solid var(--border);
      border-radius: 4px; padding: 1px 6px; margin-left: 6px;
      vertical-align: middle;
    }
    .tag-exhausted {
      font-size: .65rem; background: rgba(239,68,68,.12);
      color: var(--red); border: 1px solid rgba(239,68,68,.25);
      border-radius: 4px; padding: 1px 6px; margin-left: 6px;
      vertical-align: middle;
    }

    .usage-error {
      padding: 20px; font-size: .82rem; color: var(--red);
      display: flex; align-items: center; gap: 8px;
    }

    /* ── ERROR BANNER ── */
    .error-banner {
      background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3);
      border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;
      color: var(--red); font-size: .875rem; display: none;
    }

    footer {
      margin-top: 32px; text-align: center;
      font-size: .72rem; color: var(--muted);
    }
    footer a { color: var(--accent); text-decoration: none; }
    footer a:hover { text-decoration: underline; }
  </style>
</head>
<body>

  <header>
    <div class="logo">
      <div class="logo-icon">🤖</div>
      <div>
        <h1>AGY — Dashboard</h1>
        <div class="subtitle">3 contas Google · uso em tempo real</div>
      </div>
    </div>
    <div class="header-actions">
      <div class="auto-refresh">
        Auto-atualizar:
        <select id="autoRefreshSelect" onchange="setAutoRefresh(this.value)">
          <option value="0">Desativado</option>
          <option value="10">10s</option>
          <option value="30" selected>30s</option>
          <option value="60">1min</option>
          <option value="300">5min</option>
        </select>
      </div>
      <div class="last-update" id="lastUpdate">—</div>
      <button class="btn-refresh" id="refreshBtn" onclick="loadAll(true)">
        <span class="spin">↻</span> Atualizar
      </button>
    </div>
  </header>

  <div class="error-banner" id="errorBanner"></div>

  <!-- ══════════════════════════════════════════════
       GERENCIAR PERFIS — seção nova
  ══════════════════════════════════════════════ -->
  <div class="section-title">
    ⚙️ Gerenciar Perfis Ativos
    <span class="pill" id="managerPill">carregando…</span>
  </div>

  <div class="manager-section" id="managerSection">
    <div class="manager-header">
      <div class="manager-header-left">
        <div>
          <div class="manager-title">Perfis disponíveis para a API</div>
          <div class="manager-subtitle">
            Escolha quais perfis o <code style="font-size:.78rem;background:var(--bg);padding:1px 5px;border-radius:4px;">:8080/v1</code> usará no round-robin.
            Requisições com prefixo <code style="font-size:.78rem;background:var(--bg);padding:1px 5px;border-radius:4px;">p1/</code> <code style="font-size:.78rem;background:var(--bg);padding:1px 5px;border-radius:4px;">p2/</code> <code style="font-size:.78rem;background:var(--bg);padding:1px 5px;border-radius:4px;">p3/</code> ignoram esta seleção.
          </div>
        </div>
      </div>
      <div class="manager-actions">
        <button class="btn-sm btn-all"  onclick="selectAll()">✅ Todos</button>
        <button class="btn-sm btn-none" onclick="selectNone()">🚫 Nenhum</button>
        <button class="btn-sm btn-save" id="saveBtn" onclick="saveEnabled()">💾 Salvar</button>
      </div>
    </div>

    <div class="no-profiles-warn" id="noProfilesWarn">
      ⚠️ Nenhum perfil selecionado! A API usará o Perfil 1 como fallback mas ficará sem round-robin.
    </div>

    <div class="manager-profiles" id="managerProfiles">
      <!-- populated by JS -->
    </div>

    <div class="save-feedback" id="saveFeedback"></div>
  </div>

  <!-- Summary -->
  <div class="summary-bar">
    <div class="summary-card">
      <div class="summary-label">Total Requisições</div>
      <div class="summary-value c-blue" id="totalRequests">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Tokens Entrada</div>
      <div class="summary-value c-green" id="totalTokensIn">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Tokens Saída</div>
      <div class="summary-value c-purple" id="totalTokensOut">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Erros Totais</div>
      <div class="summary-value c-red" id="totalErrors">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Perfis Ativos</div>
      <div class="summary-value c-orange" id="profilesEnabled">—</div>
    </div>
    <div class="summary-card">
      <div class="summary-label">Perfis Online</div>
      <div class="summary-value c-green" id="profilesOnline">—</div>
    </div>
  </div>

  <!-- Profile cards -->
  <div class="section-title">
    👤 Perfis &amp; Estatísticas de Requisições
    <span class="pill">Round-robin automático</span>
  </div>
  <div class="profiles-grid" id="profilesGrid"></div>

  <!-- Usage section -->
  <div class="usage-section">
    <div class="section-title">
      📊 Quota AGY — idêntico ao <code style="font-size:.8rem;background:var(--surface2);padding:2px 6px;border-radius:4px;">/usage</code>
      <span class="pill" id="usageCachedLabel">—</span>
    </div>
    <div class="usage-grid" id="usageGrid">
      <!-- populated by JS -->
    </div>
  </div>

  <footer>
    AGY Multi-Profile Dashboard &mdash; porta 80 &bull;
    API: <a href="http://127.0.0.1:8080/v1/profiles/enabled" target="_blank">/v1/profiles/enabled</a>
    &bull; cache TTL 60s
  </footer>

  <script>
    let autoRefreshTimer = null;

    // Tracks the current toggle state (before saving)
    // { p1: true, p2: false, p3: true }
    let pendingEnabled = {};
    let savedEnabled   = {};
    let isDirty = false;

    /* ── helpers ── */
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

    function tokenExpiryHtml(expiry) {
      if (!expiry) return '<div class="token-badge expired">⚠ Sem token</div>';
      const exp = new Date(expiry), now = new Date();
      const diffMin = Math.floor((exp - now) / 60000);
      if (diffMin < 0)
        return `<div class="token-badge expired">✗ Token expirado (${Math.abs(diffMin)}min atrás)</div>`;
      if (diffMin < 10)
        return `<div class="token-badge expiring">⚠ Expira em ${diffMin}min</div>`;
      const h = Math.floor(diffMin/60), m = diffMin%60;
      const label = h > 0 ? `${h}h ${m}min` : `${m}min`;
      return `<div class="token-badge valid">✓ Token válido — expira em ${label}</div>`;
    }

    function resetLabel(isoStr) {
      if (!isoStr) return '';
      const d = new Date(isoStr);
      const now = new Date();
      const diffMs = d - now;
      if (diffMs <= 0) return 'Reset já passou';
      const totalMin = Math.floor(diffMs / 60000);
      const days = Math.floor(totalMin / 1440);
      const hrs  = Math.floor((totalMin % 1440) / 60);
      const mins = totalMin % 60;
      let parts = [];
      if (days) parts.push(`${days}d`);
      if (hrs)  parts.push(`${hrs}h`);
      if (mins && !days) parts.push(`${mins}min`);
      return `Reset em ${parts.join(' ')} (${d.toLocaleString('pt-BR')})`;
    }

    /* ════════════════════════════════════════════
       PROFILE MANAGER
    ════════════════════════════════════════════ */

    function renderManager(data) {
      const profiles = data.profiles || [];

      // Sync saved state
      savedEnabled = {};
      profiles.forEach(p => { savedEnabled[p.id] = p.enabled; });

      // Only init pending if not dirty (don't overwrite unsaved changes)
      if (!isDirty) {
        pendingEnabled = { ...savedEnabled };
      }

      updateManagerUI();
    }

    let dynamicProfileMeta = {
      p1: { name: 'Perfil 1', email: 'perfil1@exemplo.com' },
      p2: { name: 'Perfil 2', email: 'perfil2@exemplo.com' },
      p3: { name: 'Perfil 3', email: 'perfil3@exemplo.com' },
    };

    function updateManagerUI() {
      const profiles = Object.keys(pendingEnabled).length ? Object.keys(pendingEnabled) : ['p1','p2','p3'];
      const profileMeta = dynamicProfileMeta;

      const enabledCount = Object.values(pendingEnabled).filter(Boolean).length;

      // Pill
      document.getElementById('managerPill').textContent =
        enabledCount === 0 ? '⚠ nenhum ativo'
        : enabledCount === 3 ? 'todos ativos'
        : `${enabledCount}/3 ativos`;

      // Warning
      const warn = document.getElementById('noProfilesWarn');
      warn.classList.toggle('visible', enabledCount === 0);

      // Profiles grid
      const container = document.getElementById('managerProfiles');
      container.innerHTML = profiles.map(pid => {
        const meta    = profileMeta[pid];
        const enabled = !!pendingEnabled[pid];
        const activeClass = enabled ? `${pid}-active` : '';
        return `
          <div class="manager-profile-row enabled-row ${activeClass}" onclick="togglePending('${pid}')">
            <div class="manager-profile-info">
              <div class="mgr-avatar ${pid} ${!enabled ? 'disabled-avatar' : ''}">${pid.toUpperCase()}</div>
              <div>
                <div class="mgr-name" style="color:${enabled ? 'var(--text)' : 'var(--muted)'}">${meta.name}</div>
                <div class="mgr-email">${meta.email}</div>
                <span class="mgr-status-tag ${enabled ? 'tag-on' : 'tag-off'}">
                  ${enabled ? '● ATIVO no round-robin' : '○ INATIVO'}
                </span>
              </div>
            </div>
            <label class="toggle" onclick="event.stopPropagation()">
              <input type="checkbox" id="toggle-${pid}" ${enabled ? 'checked' : ''}
                     onchange="togglePendingDirect('${pid}', this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>`;
      }).join('');

      // Dirty state → save button glow
      checkDirty();

      // Update summary
      document.getElementById('profilesEnabled').textContent =
        `${enabledCount} / 3`;
    }

    function togglePending(pid) {
      pendingEnabled[pid] = !pendingEnabled[pid];
      isDirty = true;
      updateManagerUI();
    }

    function togglePendingDirect(pid, val) {
      pendingEnabled[pid] = val;
      isDirty = true;
      updateManagerUI();
    }

    function selectAll() {
      ['p1','p2','p3'].forEach(p => { pendingEnabled[p] = true; });
      isDirty = true;
      updateManagerUI();
    }

    function selectNone() {
      ['p1','p2','p3'].forEach(p => { pendingEnabled[p] = false; });
      isDirty = true;
      updateManagerUI();
    }

    function checkDirty() {
      const changed = JSON.stringify(pendingEnabled) !== JSON.stringify(savedEnabled);
      isDirty = changed;
      const saveBtn = document.getElementById('saveBtn');
      if (changed) {
        saveBtn.style.boxShadow = '0 0 0 2px rgba(79,142,247,.5)';
        saveBtn.textContent = '💾 Salvar alterações';
      } else {
        saveBtn.style.boxShadow = '';
        saveBtn.textContent = '💾 Salvar';
      }
    }

    async function saveEnabled() {
      const saveBtn = document.getElementById('saveBtn');
      const feedback = document.getElementById('saveFeedback');
      saveBtn.disabled = true;
      feedback.className = 'save-feedback';
      feedback.style.display = 'none';

      const enabledIds = Object.entries(pendingEnabled)
        .filter(([,v]) => v)
        .map(([k]) => k);

      try {
        const resp = await fetch('/api/profiles/enabled', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: enabledIds }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        // Confirm saved
        savedEnabled = { ...pendingEnabled };
        isDirty = false;
        checkDirty();

        const n = data.total_enabled;
        feedback.className = 'save-feedback visible';
        feedback.innerHTML = `✓ Salvo! ${n === 0 ? 'Nenhum perfil ativo (usando fallback p1)' : `${n} perfil(is) ativo(s): ${data.enabled_ids.join(', ')}`}`;
        setTimeout(() => { feedback.className = 'save-feedback'; }, 4000);
      } catch(e) {
        feedback.className = 'save-feedback visible err';
        feedback.innerHTML = `✗ Erro ao salvar: ${e.message}`;
        setTimeout(() => { feedback.className = 'save-feedback'; }, 5000);
      } finally {
        saveBtn.disabled = false;
      }
    }

    /* ── RENDER PROFILE CARDS ── */
    function renderProfiles(data) {
      const profiles = data.profiles || [];
      if (profiles.length) {
        profiles.forEach(p => {
          dynamicProfileMeta[p.id] = { name: p.name, email: p.email };
        });
        updateManagerUI();
      }
      const totalReqs = profiles.reduce((a,p) => a + (p.stats.requests||0), 0);
      const totalIn   = profiles.reduce((a,p) => a + (p.stats.tokens_in||0), 0);
      const totalOut  = profiles.reduce((a,p) => a + (p.stats.tokens_out||0), 0);
      const totalErr  = profiles.reduce((a,p) => a + (p.stats.errors||0), 0);
      const online    = profiles.filter(p => p.token?.token_valid).length;

      document.getElementById('totalRequests').textContent = fmt(totalReqs);
      document.getElementById('totalTokensIn').textContent = fmt(totalIn);
      document.getElementById('totalTokensOut').textContent = fmt(totalOut);
      document.getElementById('totalErrors').textContent = fmt(totalErr);
      document.getElementById('profilesOnline').textContent = `${online} / ${profiles.length}`;

      const grid = document.getElementById('profilesGrid');
      grid.innerHTML = profiles.map(p => {
        const pid = p.id;
        const s   = p.stats || {};
        const tok = p.token || {};
        const reqs = s.requests || 0;
        const errPct = reqs > 0 ? Math.round(((s.errors||0)/reqs)*100) : 0;
        const sharePct = totalReqs > 0 ? Math.min(100, Math.round((reqs/totalReqs)*100)) : 0;
        const statusCls = !tok.token_valid ? 'err'
          : (tok.expiry && (new Date(tok.expiry)-new Date() < 600000) ? 'warn' : 'ok');
        const isEnabled = !!savedEnabled[pid];
        return `
          <div class="profile-card" style="${!isEnabled ? 'opacity:.55;' : ''}">
            <div class="card-header">
              <div class="avatar ${pid}">${pid.toUpperCase()}</div>
              <div class="card-title">
                <div class="profile-name">${p.name} ${!isEnabled ? '<span style="font-size:.65rem;color:var(--muted);font-weight:400">(inativo no round-robin)</span>' : ''}</div>
                <div class="profile-email" title="${p.email}">${p.email}</div>
              </div>
              <div class="status-dot ${statusCls}"></div>
            </div>
            <div class="card-body">
              <div class="stat-row">
                <span class="stat-label">🔢 Requisições</span>
                <span class="stat-value sv-blue">${fmt(reqs)}</span>
              </div>
              <div class="stat-row">
                <span class="stat-label">📥 Tokens entrada</span>
                <span class="stat-value sv-tok">${fmt(s.tokens_in||0)}</span>
              </div>
              <div class="stat-row">
                <span class="stat-label">📤 Tokens saída</span>
                <span class="stat-value sv-tok">${fmt(s.tokens_out||0)}</span>
              </div>
              <div class="stat-row">
                <span class="stat-label">⚠ Erros</span>
                <span class="stat-value sv-err">${fmt(s.errors||0)}${reqs>0?' <small style="color:var(--muted)">('+errPct+'%)</small>':''}</span>
              </div>
              <div class="stat-row" style="border:none">
                <span class="stat-label">🕐 Último uso</span>
                <span class="stat-value" style="font-size:.73rem;color:var(--muted)">${s.last_used ? new Date(s.last_used).toLocaleTimeString('pt-BR') : '—'}</span>
              </div>
              <div class="mini-bar-wrap">
                <div class="mini-bar-labels"><span>Share de uso</span><span>${sharePct}%</span></div>
                <div class="mini-bar-bg"><div class="mini-bar-fill ${pid}" style="width:${sharePct}%"></div></div>
              </div>
              ${tokenExpiryHtml(tok.expiry)}
            </div>
          </div>`;
      }).join('');
    }

    /* ── RENDER USAGE SECTION ── */
    function renderUsage(data) {
      const pu = data.profiles_usage || [];
      const grid = document.getElementById('usageGrid');

      // update cache label
      const anyFresh = pu.some(p => !p.cached);
      document.getElementById('usageCachedLabel').textContent =
        anyFresh ? 'dados frescos' : 'cache 60s';

      grid.innerHTML = pu.map(p => {
        const pid = p.id || '';
        const groups = p.groups || [];

        if (!p.ok && !groups.length) {
          return `
            <div class="usage-card">
              <div class="usage-card-header">
                <div class="usage-avatar avatar ${pid}">${pid.toUpperCase()}</div>
                <div class="usage-card-meta">
                  <div class="usage-card-name">${p.name || pid}</div>
                  <div class="usage-card-email">${p.email || ''}</div>
                </div>
              </div>
              <div class="usage-error">❌ ${p.error || 'Erro ao buscar quota'}</div>
            </div>`;
        }

        const groupsHtml = groups.map(g => {
          const bucketsHtml = (g.buckets || []).map(b => {
            const frac = b.remaining_fraction ?? 0;
            const disabled = b.disabled === true;
            const p100 = disabled ? 100 : Math.round(frac * 100);
            const displayPct = disabled ? 'desativado' : `${p100}%`;
            const cls = disabled ? 'dis' : barClass(p100);
            const exhausted = !disabled && p100 === 0;

            return `
              <div class="bucket">
                <div class="bucket-header">
                  <span class="bucket-name">${b.name}${disabled ? '<span class="tag-disabled">DISABLED</span>' : ''}${exhausted ? '<span class="tag-exhausted">ESGOTADO</span>' : ''}</span>
                  <span class="bucket-pct" style="color:${disabled?'var(--muted)':exhausted?'var(--red)':p100>=50?'var(--green)':p100>=20?'var(--yellow)':'var(--red)'}">${displayPct}</span>
                </div>
                <div class="bar-bg">
                  <div class="bar-fill ${cls}" style="width:${disabled?100:p100}%"></div>
                </div>
                <div class="bucket-desc">${b.description || ''}</div>
                ${b.reset_time ? `<div class="bucket-reset">🕐 ${resetLabel(b.reset_time)}</div>` : ''}
              </div>`;
          }).join('');

          return `
            <div class="usage-group">
              <div class="usage-group-name">
                ${g.name === 'Gemini Models' ? '🔵' : '🟣'} ${g.name}
              </div>
              <div class="usage-group-models">${g.description || ''}</div>
              ${bucketsHtml}
            </div>`;
        }).join('<hr style="border:none;border-top:1px solid var(--border);margin:0 0 16px 0">');

        const descHtml = p.description
          ? `<div class="usage-desc">${p.description}</div>`
          : '';

        return `
          <div class="usage-card">
            <div class="usage-card-header">
              <div class="usage-avatar avatar ${pid}">${pid.toUpperCase()}</div>
              <div class="usage-card-meta">
                <div class="usage-card-name">${p.name || pid}</div>
                <div class="usage-card-email">${p.email || ''}</div>
              </div>
            </div>
            ${descHtml}
            <div class="usage-groups">${groupsHtml}</div>
          </div>`;
      }).join('');
    }

    /* ── LOADING SKELETON FOR USAGE ── */
    function showUsageLoading() {
      const grid = document.getElementById('usageGrid');
      grid.innerHTML = ['p1','p2','p3'].map(pid => `
        <div class="usage-card">
          <div class="usage-card-header">
            <div class="usage-avatar avatar ${pid}">${pid.toUpperCase()}</div>
            <div class="usage-card-meta">
              <div class="usage-card-name">Carregando...</div>
            </div>
          </div>
          <div class="usage-loading">
            <span class="usage-spinner"></span> Buscando quota no AGY CLI…
          </div>
        </div>`).join('');
    }

    /* ── MAIN LOAD ── */
    async function loadAll(force = false) {
      const btn = document.getElementById('refreshBtn');
      const banner = document.getElementById('errorBanner');
      btn.classList.add('loading');
      btn.disabled = true;
      banner.style.display = 'none';

      if (force) showUsageLoading();

      try {
        const forceQ = force ? '?force=true' : '';
        const [profResp, usageResp, enabledResp] = await Promise.all([
          fetch('/api/profiles',         { cache: 'no-store' }),
          fetch('/api/usage' + forceQ,   { cache: 'no-store' }),
          fetch('/api/profiles/enabled', { cache: 'no-store' }),
        ]);

        if (enabledResp.ok) {
          const enabledData = await enabledResp.json();
          renderManager(enabledData);
        }

        if (profResp.ok) {
          const profData = await profResp.json();
          renderProfiles(profData);
        }

        if (usageResp.ok) {
          const usageData = await usageResp.json();
          renderUsage(usageData);
        } else {
          throw new Error(`Usage HTTP ${usageResp.status}`);
        }

        document.getElementById('lastUpdate').textContent =
          'Atualizado: ' + new Date().toLocaleTimeString('pt-BR');

      } catch(err) {
        banner.textContent = '❌ Erro: ' + err.message;
        banner.style.display = 'block';
      } finally {
        btn.classList.remove('loading');
        btn.disabled = false;
      }
    }

    function setAutoRefresh(seconds) {
      if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
      const s = parseInt(seconds);
      if (s > 0) autoRefreshTimer = setInterval(() => loadAll(false), s * 1000);
    }

    // init
    showUsageLoading();
    loadAll(true);
    setAutoRefresh(30);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _proxy(path, params=''):
    url = f"{API_BASE}{path}{params}"
    try:
        with urllib.request.urlopen(url, timeout=40) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.URLError as e:
        return None, str(e)
    except Exception as e:
        return None, str(e)


def _proxy_post(path, body: dict):
    url = f"{API_BASE}{path}"
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


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/profiles")
def api_profiles():
    data, err = _proxy("/v1/profiles")
    if err:
        return jsonify({"error": err, "profiles": []}), 502
    return jsonify(data)


@app.route("/api/profiles/enabled", methods=["GET"])
def api_get_enabled():
    data, err = _proxy("/v1/profiles/enabled")
    if err:
        return jsonify({"error": err, "profiles": []}), 502
    return jsonify(data)


@app.route("/api/profiles/enabled", methods=["POST"])
def api_set_enabled():
    body = request.get_json(force=True, silent=True) or {}
    data, err = _proxy_post("/v1/profiles/enabled", body)
    if err:
        return jsonify({"error": err}), 502
    return jsonify(data)


@app.route("/api/usage")
def api_usage():
    force = request.args.get("force", "false")
    data, err = _proxy("/v1/profiles/usage", f"?force={force}")
    if err:
        return jsonify({"error": err, "profiles_usage": []}), 502
    return jsonify(data)


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port  = int(os.getenv("PORT", "80"))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    print(f"Starting AGY Dashboard on 0.0.0.0:{port}")
    print(f"Proxying to API at {API_BASE}")
    app.run(host="0.0.0.0", port=port, debug=debug)
