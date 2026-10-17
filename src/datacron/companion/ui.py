# Copyright 2026 Julien Bombled
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Dracula-themed single-page application renderer for Datacron Companion."""

from __future__ import annotations

import json
from typing import Final

from datacron.companion.theme import DARK_THEME_COLORS, LAYOUT_TOKENS

__all__ = ["render_companion_html"]

_CSS_TEMPLATE: Final[str] = """
:root {
  --bg: __background__;
  --surface: __surface__;
  --card: __card__;
  --highlight: __highlight__;
  --accent: __accent__;
  --accent-hover: __accent_hover__;
  --accent-pressed: __accent_pressed__;
  --text-primary: __text_primary__;
  --text-secondary: __text_secondary__;
  --border: __border__;
  --focus: __focus__;
  --success: __success__;
  --warning: __warning__;
  --error: __error__;
  --info: __info__;
  --rail-width: __nav_rail_width__;
  --card-radius: __card_radius__;
  --control-radius: __control_radius__;
  --card-padding: __card_padding__;
  --page-padding: __page_padding__;
  --control-height: __control_min_height__;
  --font-sans: __font_sans__;
  --font-mono: __font_mono__;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 13px;
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.shell { display: flex; flex: 1; overflow: hidden; }
.nav-rail {
  width: var(--rail-width);
  background: var(--surface);
  border-right: 1px solid var(--card);
  display: flex;
  flex-direction: column;
  padding: 20px 12px;
  gap: 6px;
}
.brand-title { font-size: 18px; font-weight: 700; color: var(--text-primary); padding: 0 8px; }
.brand-sub {
  font-size: 11px;
  color: var(--text-secondary);
  padding: 2px 8px 16px 8px;
  line-height: 1.35;
}
.nav-btn {
  background: transparent;
  color: var(--text-secondary);
  border: none;
  border-left: 3px solid transparent;
  border-radius: var(--control-radius);
  min-height: var(--control-height);
  padding: 8px 12px;
  text-align: left;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.nav-btn:hover { background: var(--highlight); color: var(--text-primary); }
.nav-btn.active {
  background: var(--highlight);
  color: var(--text-primary);
  border-left-color: var(--accent);
}
.nav-btn:focus-visible, button:focus-visible, input:focus-visible,
select:focus-visible, textarea:focus-visible, .list-item:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.rail-footer { margin-top: auto; padding-top: 12px; border-top: 1px solid var(--card); }
.main-area {
  flex: 1;
  padding: var(--page-padding);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.page { display: none; flex-direction: column; gap: 18px; }
.page.active { display: flex; }
.headline { font-size: 22px; font-weight: 700; color: var(--text-primary); }
.card {
  background: var(--card);
  border-radius: var(--card-radius);
  padding: var(--card-padding);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.stat-box {
  background: var(--highlight);
  border-radius: var(--control-radius);
  padding: 14px;
}
.stat-label { font-size: 11px; color: var(--text-secondary); text-transform: uppercase; }
.stat-value { font-size: 20px; font-weight: 700; margin-top: 4px; color: var(--text-primary); }
label { font-size: 12px; font-weight: 600; color: var(--text-secondary); display: block; }
input[type="text"], input[type="number"], select, textarea {
  width: 100%;
  min-height: var(--control-height);
  background: var(--surface);
  color: var(--text-primary);
  border: 1px solid var(--border);
  border-radius: var(--control-radius);
  padding: 8px 12px;
  font-family: var(--font-sans);
  font-size: 13px;
}
textarea { font-family: var(--font-mono); min-height: 90px; resize: vertical; }
.row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.btn {
  min-height: var(--control-height);
  padding: 8px 16px;
  border-radius: var(--control-radius);
  border: 1px solid transparent;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  background: var(--highlight);
  color: var(--text-primary);
}
.btn:hover { border-color: var(--border); }
.btn-primary { background: var(--accent); color: var(--surface); }
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:active { background: var(--accent-pressed); }
.badge {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 700;
  background: var(--highlight);
  color: var(--info);
}
.badge-ok { color: var(--success); }
.badge-warn { color: var(--warning); }
.badge-err { color: var(--error); }
.mono-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--control-radius);
  padding: 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  max-height: 320px;
  overflow-y: auto;
  white-space: pre-wrap;
}
.status-bar {
  background: var(--surface);
  border-top: 1px solid var(--card);
  padding: 8px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: var(--text-secondary);
}
.list-item {
  background: var(--highlight);
  border: 1px solid transparent;
  border-radius: var(--control-radius);
  padding: 10px 12px;
  margin-bottom: 8px;
  cursor: pointer;
}
.list-item:hover { border-color: var(--border); }
"""

_HTML_BODY: Final[str] = """
<div class="shell">
  <nav class="nav-rail" aria-label="Primary">
    <div class="brand-title" data-i18n="app.title"></div>
    <div class="brand-sub" data-i18n="app.subtitle"></div>
    <button class="nav-btn active" aria-current="page" data-tab="home"
      data-i18n="nav.home"></button>
    <button class="nav-btn" data-tab="vaults" data-i18n="nav.vaults"></button>
    <button class="nav-btn" data-tab="search" data-i18n="nav.search"></button>
    <button class="nav-btn" data-tab="graph" data-i18n="nav.graph"></button>
    <button class="nav-btn" data-tab="mcp" data-i18n="nav.mcp"></button>
    <button class="nav-btn" data-tab="history" data-i18n="nav.history"></button>
    <button class="nav-btn" data-tab="settings" data-i18n="nav.settings"></button>
    <div class="rail-footer">
      <label for="lang-select" data-i18n="app.language_label"></label>
      <select id="lang-select">
        <option value="fr">FR</option>
        <option value="en">EN</option>
      </select>
    </div>
  </nav>

  <main class="main-area">
    <section id="tab-home" class="page active">
      <h1 class="headline" data-i18n="home.headline"></h1>
      <div class="card">
        <div class="row">
          <span id="home-status-badge" class="badge badge-ok"></span>
          <button class="btn btn-primary" id="home-cta-sync" data-i18n="home.cta_sync"></button>
          <button class="btn" id="home-cta-vaults" data-i18n="home.cta_configure"></button>
          <button class="btn" id="home-cta-search" data-i18n="home.cta_search"></button>
        </div>
        <div class="grid-4">
          <div class="stat-box">
            <div class="stat-label" data-i18n="home.card_notes"></div>
            <div class="stat-value" id="stat-notes">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label" data-i18n="home.card_chunks"></div>
            <div class="stat-value" id="stat-chunks">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label" data-i18n="home.card_generation"></div>
            <div class="stat-value" id="stat-gen">0</div>
          </div>
          <div class="stat-box">
            <div class="stat-label" data-i18n="home.card_mode"></div>
            <div class="stat-value" id="stat-mode">-</div>
          </div>
        </div>
      </div>
      <div class="card">
        <h2 data-i18n="home.getting_started_title"></h2>
        <div class="list-item" role="button" tabindex="0" data-jump="vaults"
          data-i18n="home.step1_title"></div>
        <div class="list-item" role="button" tabindex="0" data-jump="vaults"
          data-i18n="home.step2_title"></div>
        <div class="list-item" role="button" tabindex="0" data-jump="vaults"
          data-i18n="home.step3_title"></div>
        <div class="list-item" role="button" tabindex="0" data-jump="mcp"
          data-i18n="home.step4_title"></div>
      </div>
    </section>

    <section id="tab-vaults" class="page">
      <h1 class="headline" data-i18n="vaults.headline"></h1>
      <div class="card">
        <label for="inp-vault" data-i18n="vaults.docs_vault_label"></label>
        <input type="text" id="inp-vault" />
        <label for="inp-mem-vault" data-i18n="vaults.memory_vault_label"></label>
        <input type="text" id="inp-mem-vault" />
        <label for="inp-extra-vaults" data-i18n="vaults.extra_vaults_label"></label>
        <textarea id="inp-extra-vaults"></textarea>
        <div class="row">
          <button class="btn btn-primary" id="btn-save-topology"
            data-i18n="vaults.save_topology"></button>
          <button class="btn" id="btn-sync-inc" data-i18n="vaults.sync_now"></button>
          <button class="btn" id="btn-sync-rebuild" data-i18n="vaults.rebuild_now"></button>
        </div>
      </div>
      <div class="card">
        <h2 data-i18n="vaults.migrate_title"></h2>
        <p data-i18n="vaults.migrate_desc"></p>
        <label class="row">
          <input type="checkbox" id="chk-keep-source" />
          <span data-i18n="vaults.migrate_keep_source"></span>
        </label>
        <div class="row">
          <button class="btn btn-primary" id="btn-migrate-dual"
            data-i18n="vaults.migrate_button"></button>
        </div>
        <div id="vaults-output" class="mono-box"></div>
      </div>
    </section>

    <section id="tab-search" class="page">
      <h1 class="headline" data-i18n="search.headline"></h1>
      <div class="card">
        <div class="grid-2">
          <input type="text" id="inp-search-query" aria-label="Search query"
            data-i18n-aria="search.query_placeholder"
            data-i18n-placeholder="search.query_placeholder" />
          <input type="text" id="inp-search-folder" aria-label="Folder filter"
            data-i18n-aria="search.folder_placeholder"
            data-i18n-placeholder="search.folder_placeholder" />
        </div>
        <div class="row">
          <select id="sel-search-mode" aria-label="Search mode"
            data-i18n-aria="search.headline" style="width:180px;">
            <option value="fts" data-i18n="search.mode_fts"></option>
            <option value="regex" data-i18n="search.mode_regex"></option>
          </select>
          <button class="btn btn-primary" id="btn-search-run"
            data-i18n="search.run_button"></button>
          <button class="btn" id="btn-search-clear" data-i18n="search.clear_button"></button>
        </div>
      </div>
      <div class="grid-2">
        <div class="card" id="search-results-list"></div>
        <div class="card">
          <h3 data-i18n="search.preview_title"></h3>
          <div id="search-preview-box" class="mono-box"></div>
        </div>
      </div>
    </section>

    <section id="tab-graph" class="page">
      <h1 class="headline" data-i18n="graph.headline"></h1>
      <div class="card">
        <div class="row">
          <button class="btn btn-primary" id="btn-graph-analyze"
            data-i18n="graph.analyze_button"></button>
        </div>
        <div id="graph-output" class="mono-box"></div>
      </div>
      <div class="card">
        <h2 data-i18n="graph.compaction_title"></h2>
        <label for="inp-compact-days" data-i18n="graph.compaction_days_label"></label>
        <input type="number" id="inp-compact-days" value="30" min="0" style="width:160px;" />
        <div class="row">
          <button class="btn" id="btn-compact-preview"
            data-i18n="graph.compaction_preview_btn"></button>
          <button class="btn btn-primary" id="btn-compact-apply"
            data-i18n="graph.compaction_apply_btn"></button>
        </div>
        <div id="compact-output" class="mono-box"></div>
      </div>
    </section>

    <section id="tab-mcp" class="page">
      <h1 class="headline" data-i18n="mcp.headline"></h1>
      <div class="card">
        <div class="grid-2">
          <div>
            <label for="sel-mcp-client" data-i18n="mcp.client_label"></label>
            <select id="sel-mcp-client"></select>
          </div>
          <div>
            <label for="sel-mcp-scope" data-i18n="mcp.scope_label"></label>
            <select id="sel-mcp-scope"></select>
          </div>
        </div>
        <label class="row">
          <input type="checkbox" id="chk-mcp-write" checked />
          <span data-i18n="mcp.enable_write_label"></span>
        </label>
        <div class="row">
          <button class="btn btn-primary" id="btn-mcp-configure"
            data-i18n="mcp.configure_button"></button>
        </div>
        <div id="mcp-targets-box" class="mono-box"></div>
      </div>
    </section>

    <section id="tab-history" class="page">
      <h1 class="headline" data-i18n="history.headline"></h1>
      <div class="card">
        <div class="row">
          <button class="btn btn-primary" id="btn-history-reload"
            data-i18n="history.reload_button"></button>
        </div>
        <div id="history-output" class="mono-box"></div>
      </div>
    </section>

    <section id="tab-settings" class="page">
      <h1 class="headline" data-i18n="settings.headline"></h1>
      <div class="card">
        <label for="txt-excluded" data-i18n="settings.excluded_folders_label"></label>
        <textarea id="txt-excluded"></textarea>
        <label for="txt-archive-tags" data-i18n="settings.archive_tags_label"></label>
        <textarea id="txt-archive-tags"></textarea>
        <label for="txt-query-exp" data-i18n="settings.query_expansion_label"></label>
        <textarea id="txt-query-exp"></textarea>
        <div class="grid-2">
          <div>
            <label for="sel-durability" data-i18n="settings.durability_label"></label>
            <select id="sel-durability"></select>
          </div>
          <div>
            <label for="sel-timeout" data-i18n="settings.timeout_label"></label>
            <select id="sel-timeout"></select>
          </div>
        </div>
        <div class="row">
          <button class="btn btn-primary" id="btn-save-vault-cfg"
            data-i18n="settings.save_vault_config_btn"></button>
          <span class="badge" id="lbl-cas-hash"></span>
        </div>
      </div>
    </section>
  </main>
</div>
<footer class="status-bar" role="status" aria-live="polite">
  <span id="status-message"></span>
  <button class="btn" id="btn-global-refresh" data-i18n="app.refresh"></button>
</footer>
"""

_JS_APP: Final[str] = """
let APP_STATE = null;

async function apiCall(path, method = "GET", body = null) {
  const headers = { "X-Datacron-Token": SESSION_TOKEN };
  if (body !== null) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body !== null ? JSON.stringify(body) : null
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

async function runAction(btn, fn) {
  if (btn) btn.disabled = true;
  try {
    await fn();
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    const statusEl = document.getElementById("status-message");
    if (statusEl) statusEl.textContent = msg;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function resolveI18n(i18n, dotted) {
  const parts = dotted.split(".");
  let cur = i18n;
  for (const p of parts) {
    if (!cur || typeof cur !== "object") return "";
    cur = cur[p];
  }
  return typeof cur === "string" ? cur : "";
}

function applyTranslations(i18n, currentLang) {
  document.documentElement.lang = currentLang || "en";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const val = resolveI18n(i18n, el.getAttribute("data-i18n"));
    if (val) el.textContent = val;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const val = resolveI18n(i18n, el.getAttribute("data-i18n-placeholder"));
    if (val) el.setAttribute("placeholder", val);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    const val = resolveI18n(i18n, el.getAttribute("data-i18n-aria"));
    if (val) el.setAttribute("aria-label", val);
  });
  const titleVal = resolveI18n(i18n, "app.title");
  if (titleVal) document.title = titleVal;
}

function activateTab(tabName) {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    const isActive = btn.getAttribute("data-tab") === tabName;
    btn.classList.toggle("active", isActive);
    if (isActive) {
      btn.setAttribute("aria-current", "page");
    } else {
      btn.removeAttribute("aria-current");
    }
  });
  document.querySelectorAll(".page").forEach((sec) => {
    sec.classList.toggle("active", sec.id === "tab-" + tabName);
  });
}

function populateSelect(id, items, selectedVal) {
  const sel = document.getElementById(id);
  sel.innerHTML = "";
  items.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = String(item);
    opt.textContent = String(item);
    if (String(item) === String(selectedVal)) opt.selected = true;
    sel.appendChild(opt);
  });
}

function renderState(state) {
  APP_STATE = state;
  const prof = state.profile_snapshot.profile;
  applyTranslations(state.i18n, prof.ui_language);
  const vcfg = state.vault_config_snapshot;
  const rdy = state.readiness;

  document.getElementById("lang-select").value = prof.ui_language;
  document.getElementById("status-message").textContent = state.i18n.app.connected;
  document.getElementById("stat-notes").textContent = String(rdy.index_stats.note_count);
  document.getElementById("stat-chunks").textContent = String(rdy.index_stats.chunk_count);
  document.getElementById("stat-gen").textContent = String(rdy.index_stats.generation);
  document.getElementById("stat-mode").textContent = rdy.vault_mode;

  const badge = document.getElementById("home-status-badge");
  if (rdy.vault_mode === "dual") {
    badge.textContent = state.i18n.home.status_dual;
    badge.className = "badge badge-ok";
  } else if (rdy.vault_ready) {
    badge.textContent = state.i18n.home.status_single;
    badge.className = "badge badge-ok";
  } else {
    badge.textContent = state.i18n.home.status_unconfigured;
    badge.className = "badge badge-warn";
  }

  document.getElementById("inp-vault").value = prof.vault_path || "";
  document.getElementById("inp-mem-vault").value = prof.memory_vault_path || "";
  const extraLines = Object.entries(prof.extra_read_vaults || {}).map(([k, v]) => k + "=" + v);
  document.getElementById("inp-extra-vaults").value = extraLines.join("\\n");

  document.getElementById("txt-excluded").value = (vcfg.values.excluded_folders || []).join("\\n");
  document.getElementById("txt-archive-tags").value = (vcfg.values.archive_tags || []).join("\\n");
  document.getElementById("txt-query-exp").value = JSON.stringify(
    vcfg.values.query_expansion || {}, null, 2
  );
  document.getElementById("lbl-cas-hash").textContent = vcfg.content_hash;

  populateSelect("sel-mcp-client", state.choices.clients, "all");
  populateSelect("sel-mcp-scope", state.choices.scopes, "both");
  populateSelect("sel-durability", state.choices.durability_modes, prof.durability);
  populateSelect("sel-timeout", state.choices.timeouts, prof.cli_timeout_seconds);
  document.getElementById("chk-mcp-write").checked = Boolean(prof.enable_write);
  document.getElementById("mcp-targets-box").textContent = JSON.stringify(
    state.mcp_targets, null, 2
  );
}

async function refreshState() {
  const st = await apiCall("/api/state");
  renderState(st);
}

function collectProfileDraft() {
  const extraRaw = document.getElementById("inp-extra-vaults").value.split("\\n");
  const extraMap = {};
  extraRaw.forEach((line) => {
    const t = line.trim();
    if (t && t.includes("=")) {
      const idx = t.indexOf("=");
      extraMap[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
    }
  });
  return {
    vault_path: document.getElementById("inp-vault").value.trim(),
    memory_vault_path: document.getElementById("inp-mem-vault").value.trim(),
    extra_read_vaults: extraMap,
    ui_language: document.getElementById("lang-select").value,
    cli_timeout_seconds: parseInt(document.getElementById("sel-timeout").value || "30", 10),
    enable_write: document.getElementById("chk-mcp-write").checked,
    durability: document.getElementById("sel-durability").value || "best-effort"
  };
}

async function saveTopology() {
  const st = await apiCall("/api/topology", "POST", {
    expected_hash: APP_STATE ? APP_STATE.profile_snapshot.content_hash : null,
    profile: collectProfileDraft()
  });
  renderState(st);
}

async function saveVaultConfig() {
  const excluded = document.getElementById("txt-excluded").value
    .split("\\n").map((s) => s.trim()).filter(Boolean);
  const archive = document.getElementById("txt-archive-tags").value
    .split("\\n").map((s) => s.trim()).filter(Boolean);
  const qexp = JSON.parse(document.getElementById("txt-query-exp").value || "{}");
  await saveTopology();
  const res = await apiCall("/api/vault-config", "POST", {
    expected_hash: APP_STATE.vault_config_snapshot.content_hash,
    values: {
      excluded_folders: excluded,
      archive_tags: archive,
      query_expansion: qexp
    }
  });
  APP_STATE.vault_config_snapshot = res.vault_config_snapshot;
  document.getElementById("lbl-cas-hash").textContent = res.vault_config_snapshot.content_hash;
}

async function executeSearch() {
  const res = await apiCall("/api/search", "POST", {
    query: document.getElementById("inp-search-query").value,
    folder: document.getElementById("inp-search-folder").value,
    mode: document.getElementById("sel-search-mode").value
  });
  const listEl = document.getElementById("search-results-list");
  listEl.innerHTML = "";
  const items = res.results || [];
  if (!items.length) {
    listEl.textContent = APP_STATE ? APP_STATE.i18n.search.empty_state : "";
    return;
  }
  items.forEach((item) => {
    const div = document.createElement("div");
    div.className = "list-item";
    div.setAttribute("role", "button");
    div.setAttribute("tabindex", "0");
    const src = item.source_vault || "docs";
    const sec = item.section_title || "-";
    div.textContent = `[${src}] ${item.note_rel_path} (${sec})`;
    const selectItem = () => {
      document.getElementById("search-preview-box").textContent = JSON.stringify(item, null, 2);
    };
    div.addEventListener("click", selectItem);
    div.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        selectItem();
      }
    });
    listEl.appendChild(div);
  });
  document.getElementById("search-preview-box").textContent = JSON.stringify(items[0], null, 2);
}

function clearSearch() {
  document.getElementById("inp-search-query").value = "";
  document.getElementById("search-results-list").innerHTML = "";
  document.getElementById("search-preview-box").textContent = "";
}

window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.getAttribute("data-tab")));
  });
  document.querySelectorAll("[data-jump]").forEach((el) => {
    const target = el.getAttribute("data-jump");
    el.addEventListener("click", () => activateTab(target));
    el.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        activateTab(target);
      }
    });
  });
  document.getElementById("home-cta-vaults").addEventListener("click", () => activateTab("vaults"));
  document.getElementById("home-cta-search").addEventListener("click", () => activateTab("search"));
  const homeSyncBtn = document.getElementById("home-cta-sync");
  homeSyncBtn.addEventListener("click", () => runAction(homeSyncBtn, async () => {
    const out = await apiCall("/api/sync", "POST", { rebuild: false });
    document.getElementById("vaults-output").textContent = JSON.stringify(out, null, 2);
    await refreshState();
  }));
  const globalRefreshBtn = document.getElementById("btn-global-refresh");
  globalRefreshBtn.addEventListener("click", () => runAction(globalRefreshBtn, refreshState));
  document.getElementById("lang-select").addEventListener("change", () =>
    runAction(null, saveTopology)
  );
  const saveTopoBtn = document.getElementById("btn-save-topology");
  saveTopoBtn.addEventListener("click", () => runAction(saveTopoBtn, saveTopology));
  const syncIncBtn = document.getElementById("btn-sync-inc");
  syncIncBtn.addEventListener("click", () => runAction(syncIncBtn, async () => {
    const out = await apiCall("/api/sync", "POST", { rebuild: false });
    document.getElementById("vaults-output").textContent = JSON.stringify(out, null, 2);
    await refreshState();
  }));
  const syncRebuildBtn = document.getElementById("btn-sync-rebuild");
  syncRebuildBtn.addEventListener("click", () => runAction(syncRebuildBtn, async () => {
    const out = await apiCall("/api/sync", "POST", { rebuild: true });
    document.getElementById("vaults-output").textContent = JSON.stringify(out, null, 2);
    await refreshState();
  }));
  const migrateBtn = document.getElementById("btn-migrate-dual");
  migrateBtn.addEventListener("click", () => runAction(migrateBtn, async () => {
    const out = await apiCall("/api/migrate", "POST", {
      vault_path: document.getElementById("inp-vault").value.trim(),
      memory_vault_path: document.getElementById("inp-mem-vault").value.trim(),
      keep_source: document.getElementById("chk-keep-source").checked
    });
    document.getElementById("vaults-output").textContent = JSON.stringify(out, null, 2);
    await refreshState();
  }));
  const searchRunBtn = document.getElementById("btn-search-run");
  searchRunBtn.addEventListener("click", () => runAction(searchRunBtn, executeSearch));
  document.getElementById("btn-search-clear").addEventListener("click", clearSearch);
  ["inp-search-query", "inp-search-folder"].forEach((inputId) => {
    const inp = document.getElementById(inputId);
    inp.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        runAction(searchRunBtn, executeSearch);
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        clearSearch();
      }
    });
  });
  const graphBtn = document.getElementById("btn-graph-analyze");
  graphBtn.addEventListener("click", () => runAction(graphBtn, async () => {
    const out = await apiCall("/api/graph-health", "POST", {});
    document.getElementById("graph-output").textContent = JSON.stringify(out, null, 2);
  }));
  const compactPrevBtn = document.getElementById("btn-compact-preview");
  compactPrevBtn.addEventListener("click", () => runAction(compactPrevBtn, async () => {
    const out = await apiCall("/api/compact-memory", "POST", {
      older_than_days: parseInt(document.getElementById("inp-compact-days").value || "30", 10),
      dry_run: true
    });
    document.getElementById("compact-output").textContent = JSON.stringify(out, null, 2);
  }));
  const compactApplyBtn = document.getElementById("btn-compact-apply");
  compactApplyBtn.addEventListener("click", () => runAction(compactApplyBtn, async () => {
    const out = await apiCall("/api/compact-memory", "POST", {
      older_than_days: parseInt(document.getElementById("inp-compact-days").value || "30", 10),
      dry_run: false
    });
    document.getElementById("compact-output").textContent = JSON.stringify(out, null, 2);
    await refreshState();
  }));
  const mcpBtn = document.getElementById("btn-mcp-configure");
  mcpBtn.addEventListener("click", () => runAction(mcpBtn, async () => {
    const out = await apiCall("/api/mcp-setup", "POST", {
      client: document.getElementById("sel-mcp-client").value,
      install_scope: document.getElementById("sel-mcp-scope").value,
      enable_write: document.getElementById("chk-mcp-write").checked
    });
    document.getElementById("mcp-targets-box").textContent = JSON.stringify(out, null, 2);
  }));
  const historyBtn = document.getElementById("btn-history-reload");
  historyBtn.addEventListener("click", () => runAction(historyBtn, async () => {
    const out = await apiCall("/api/history", "POST", {});
    document.getElementById("history-output").textContent =
      (out.operations && out.operations.length)
        ? JSON.stringify(out.operations, null, 2)
        : APP_STATE.i18n.history.empty_log;
  }));
  const saveVaultCfgBtn = document.getElementById("btn-save-vault-cfg");
  saveVaultCfgBtn.addEventListener("click", () => runAction(saveVaultCfgBtn, saveVaultConfig));
  window.addEventListener("keydown", (ev) => {
    if (ev.key === "F5") { ev.preventDefault(); runAction(globalRefreshBtn, refreshState); }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      runAction(saveVaultCfgBtn, saveVaultConfig);
    }
  });
  runAction(null, refreshState);
});
"""


def _render_css() -> str:
    css = _CSS_TEMPLATE
    for key, val in DARK_THEME_COLORS.items():
        css = css.replace(f"__{key}__", val)
    for key, val in LAYOUT_TOKENS.items():
        css = css.replace(f"__{key}__", val)
    return css


def render_companion_html(session_token: str) -> str:
    """Render the self-contained Dracula-themed Companion HTML shell."""
    css = _render_css()
    token_literal = json.dumps(session_token)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="fr">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        f"  <style>{css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{_HTML_BODY}\n"
        f"  <script>const SESSION_TOKEN = {token_literal};\n{_JS_APP}</script>\n"
        "</body>\n"
        "</html>\n"
    )
