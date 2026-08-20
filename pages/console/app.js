/* 云信互联 控制台 · 单页控制台（概览 / 工作流 / 预设 / 设置） */
(() => {
  const bridge = window.AstrBotPluginPage;

  // ---------- API 封装（插件页走 bridge，预览模式走 /api/* mock） ----------
  async function apiGet(path, query) {
    if (bridge) return await bridge.apiGet(path, query || {});
    const qs = query ? "?" + new URLSearchParams(query) : "";
    const r = await fetch("/api/" + path + qs);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).message || "HTTP " + r.status);
    return await r.json();
  }
  async function apiPost(path, body) {
    if (bridge) return await bridge.apiPost(path, body || {});
    const r = await fetch("/api/" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).message || "HTTP " + r.status);
    return await r.json();
  }

  // ---------- 通用工具 ----------
  const $ = (id) => document.getElementById(id);

  let toastTimer = null;
  function toast(text) {
    const t = $("toast");
    if (!t) return;
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }
  function fmtSize(size) {
    if (!size) return "—";
    return size > 1024 ? (size / 1024).toFixed(1) + " KB" : size + " B";
  }
  function fmtDate(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return `${d.getMonth() + 1}/${d.getDate()} ${d.toTimeString().slice(0, 5)}`;
  }

  // ---------- 顶部返回按钮 ----------
  function goBack() {
    // 插件页通常嵌在 AstrBot 面板里：跳转顶层回主面板；预览模式回导航页
    try {
      if (window.top && window.top !== window) {
        window.top.location.href = "/";
        return;
      }
    } catch (_) {}
    if (history.length > 1) history.back();
    else location.href = "/";
  }

  // ---------- Tab 切换 ----------
  const TABS = ["overview", "gallery", "queue", "workflows", "enhance", "settings"];
  const inited = {};
  let activeTab = null;

  function renderActions(tab) {
    const box = $("tab-actions");
    const btns = {
      overview: '<button id="btn-refresh" class="ghost">🔄 刷新</button>',
      gallery: '<button id="btn-refresh" class="ghost">🔄 刷新</button>',
      queue: '<button id="btn-refresh" class="ghost">🔄 刷新</button>',
      workflows:
        '<button id="btn-upload" class="ghost">⬆️ 上传工作流</button> ' +
        '<button id="btn-refresh" class="ghost">🔄 刷新列表</button> ' +
        '<button id="btn-analyze" class="primary">🤖 LLM 识别工作流</button>',
      settings: '<button id="btn-settings-save" class="primary">💾 保存全部设置</button>',
    };
    box.innerHTML = btns[tab] || "";
    bindActions(tab);
  }

  function bindActions(tab) {
    if (tab === "overview") {
      const b = $("btn-refresh");
      if (b) b.onclick = () => overview.refresh();
    } else if (tab === "gallery") {
      const b = $("btn-refresh");
      if (b) b.onclick = () => gallery.load();
    } else if (tab === "queue") {
      const b = $("btn-refresh");
      if (b) b.onclick = () => overview.fetchQueue();
    } else if (tab === "workflows") {
      const up = $("btn-upload"), rf = $("btn-refresh"), an = $("btn-analyze");
      if (up) up.onclick = () => $("file-input").click();
      if (rf) rf.onclick = () => wf.loadList();
      if (an) an.onclick = async () => {
        an.disabled = true;
        an.textContent = "🤖 识别中…（约 10-30 秒）";
        try {
          await apiPost("workflows/analyze", {});
          toast("✅ 识别完成！工作流已打标签");
          wf.loadList();
          if (inited.settings) settings.renderSubtypes();
        } catch (e) {
          toast("❌ 识别失败：" + (e.message || e));
        } finally {
          an.disabled = false;
          an.textContent = "🤖 LLM 识别工作流";
        }
      };
    } else if (tab === "settings") {
      const s = $("btn-settings-save");
      if (s) s.onclick = () => settings.save();
    }
  }

  const PAGE_TITLES = { overview: "概览", gallery: "产物库", queue: "任务队列", workflows: "工作流", enhance: "增强对话", settings: "设置" };

  function showTab(name) {
    if (!TABS.includes(name)) name = "overview";
    const leaving = activeTab;
    activeTab = name;
    TABS.forEach((t) => {
      $(`tab-${t}`).hidden = t !== name;
      const btn = document.querySelector(`.nav-item[data-tab="${t}"]`);
      if (btn) btn.classList.toggle("active", t === name);
    });
    const pt = $("page-title");
    if (pt) pt.textContent = PAGE_TITLES[name] || name;
    // 离开工作流页暂停 LiteGraph 持续渲染（否则空转重绘拖垮整个页面）
    if (leaving === "workflows" && name !== "workflows") wf.pauseRender();
    if (name === "workflows" && leaving !== "workflows") wf.resumeRender();
    renderActions(name);
    // 懒初始化
    if (!inited[name]) {
      inited[name] = true;
      if (name === "workflows") wf.init();
      else if (name === "settings") settings.init();
      else if (name === "enhance") enhance.init();
      else if (name === "gallery") gallery.init();
    }
    // 生命周期
    if (name === "overview") {
      overview.start();
    } else {
      overview.stop();
      if (name === "workflows" && inited.workflows) {
        wf.refit();
        wf.startAutoRefresh(); // 工作流页激活：定时自动同步列表
      }
    }
    if (name === "enhance") {
      if (inited.enhance) enhance.startPolling(); // 对话页激活：4s 轮询 + 打字机
    } else {
      enhance.stopPolling();
    }
    if (name === "gallery") {
      if (inited.gallery) gallery.load(); // 产物库激活：刷新
    }
    if (name === "queue") {
      overview.fetchQueue(); // 任务队列页激活：刷新
    }
    if (name !== "workflows") wf.stopAutoRefresh();
  }

  // ============================================================
  // 概览
  // ============================================================
  const overview = {
    timer: null,
    sseOff: null,

    renderSnapshot(snap) {
      const agent = (snap.agents || [])[0] || {};
      const connected = !!agent.connected;

      overview.renderProgress(snap.progress || null);

      const connEl = $("conn-value");
      if (connected) connEl.innerHTML = '<span class="dot success"></span>已连接';
      else connEl.innerHTML = '<span class="dot danger"></span>未连接';
      $("conn-meta").textContent = connected
        ? `已连接 ${agent.connected_seconds || 0}s · 等待请求 ${snap.pending_requests || 0} 个`
        : "本地代理离线，请检查 YunxinAgent 是否在运行";

      $("host-value").textContent = agent.hostname || "—";
      $("host-meta").textContent = agent.platform || "";

      const comfy = agent.comfyui || {};
      const devs = comfy.devices || [];
      if (comfy.ok && devs.length) {
        const d = devs[0];
        $("gpu-value").innerHTML = `${d.vram_free_gb}<small> / ${d.vram_total_gb} GB</small>`;
        $("gpu-meta").textContent = `${d.name} · ComfyUI 在线`;
      } else {
        $("gpu-value").textContent = "—";
        $("gpu-meta").textContent = comfy.error || "ComfyUI 未检测";
      }

      const oai = agent.openai || {};
      if (oai.ok) {
        const models = oai.models || [];
        $("llm-value").textContent = `${models.length} 个模型`;
        $("llm-meta").textContent = models.slice(0, 4).join(", ") + (models.length > 4 ? "…" : "");
      } else {
        $("llm-value").textContent = "离线";
        $("llm-meta").textContent = oai.error || "未检测";
      }

      if (snap.version) {
        $("version-badge").textContent = `v${snap.version}`;
      }

      const caps = snap.capabilities || {};
      if (caps.workflows !== undefined) {
        const q = caps.queue || {};
        $("cap-row").innerHTML =
          `<span class="badge info">📊 工作流 ${caps.workflows} 个</span>` +
          `<span class="badge info">🧩 checkpoint ${caps.checkpoints} 个</span>` +
          `<span class="badge info">🤖 本地 LLM 模型 ${caps.llm_models} 个</span>` +
          `<span class="badge ${q.running ? "warning" : "success"}">🎨 ComfyUI 队列：运行 ${q.running ?? 0} / 排队 ${q.pending ?? 0}</span>` +
          `<span class="dim">新增的工作流/模型会被自动发现，无需改配置</span>`;
      }

      overview.fetchQueue();
    },

    async fetchQueue() {
      try {
        const data = await apiGet("overview/queue");
        const queue = (data.queue || []).slice(0, 15);
        const box = $("task-queue");
        if (!box) return;
        if (!queue.length) {
          box.innerHTML = '<span class="dim">暂无任务记录</span>';
          return;
        }
        const STATUS = {
          queued: ["⏳", "排队中", "info"],
          running: ["▶️", "运行中", "warning"],
          waiting_confirm: ["🛡️", "待审批", "warning"],
          success: ["✅", "成功", "success"],
          failed: ["❌", "失败", "danger"],
        };
        box.innerHTML = queue.map((t) => {
          const [ico, label, cls] = STATUS[t.status] || ["❔", t.status, "info"];
          const uid = "t" + (t.task_id || "").replace(/[^0-9a-zA-Z]/g, "");
          // 审批按钮（仅 waiting_confirm 状态显示）
          const approveBtns = t.status === "waiting_confirm"
            ? `<div style="margin-top:6px">
                <button class="q-btn approve" data-task="${escapeHtml(t.task_id)}" data-act="approve">✅ 通过并生成</button>
                <button class="q-btn reject" data-task="${escapeHtml(t.task_id)}" data-act="reject">🚫 拒绝</button>
              </div>`
            : "";
          // 产物预览（折叠）
          const files = (t.files || []).map((f) => overview.mediaPreview(f)).join("") || '<span class="dim">无产物</span>';
          const filesFold = (t.files || []).length
            ? `<details class="fold"><summary>📎 产物（${t.files.length}）</summary>${files}</details>`
            : '<div class="dim">无产物</div>';
          const promptText = t.prompt || t.intent || "";
          const promptFold = promptText
            ? `<details class="fold"><summary>📝 提示词（${promptText.length} 字）</summary><div class="fold-body">${escapeHtml(promptText)}</div></details>`
            : "";
          const err = t.error ? `<details class="fold"><summary style="color:#f87171">⚠️ 错误</summary><div class="fold-body" style="color:#f87171">${escapeHtml(t.error)}</div></details>` : "";
          // 产物操作按钮：下载 / 发送到QQ（产物就绪且来源会话可发时显示）
          const dlBtns = (t.files || []).length
            ? `<div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">` +
                (t.files || []).map((f) =>
                  `<a class="q-btn" href="${bridge ? "/api/plug/astrbot_plugin_remote_link/media" : "/api/media"}?filename=${encodeURIComponent(f.filename || "")}&download=1" target="_blank" rel="noopener">📥 ${escapeHtml((f.filename || "?").slice(0, 18))}</a>`
                ).join("") +
                (t.status === "success" && t.origin && !String(t.origin).startsWith("web:")
                  ? `<button class="q-btn approve" data-task="${escapeHtml(t.task_id)}" data-act="send">📤 发送到QQ</button>`
                  : "") +
              `</div>`
            : "";
          return `<div class="queue-item">
            <div><span class="badge ${cls}">${ico} ${label}</span>
            <b>${escapeHtml(t.workflow || t.subtype || "智能调度")}</b>
            <span class="dim">${escapeHtml(t.task_id)}</span>
            <span class="dim">${fmtTime(t.created_at)}</span></div>
            ${promptFold}
            ${filesFold}
            ${dlBtns}
            ${err}
            ${approveBtns}
          </div>`;
        }).join("");
        // 绑定审批/发送按钮
        box.querySelectorAll(".q-btn").forEach((btn) => {
          if (btn.dataset.act) btn.onclick = () => overview.queueAction(btn.dataset.task, btn.dataset.act, btn);
        });
      } catch (e) {
        const box = $("task-queue");
        if (box) box.innerHTML = `<span class="dim">⚠️ 队列获取失败：${escapeHtml(e.message || e)}</span>`;
      }
    },

    mediaPreview(f) {
      const name = escapeHtml(f.filename || "?");
      // 产物文件走插件 API（bridge 模式下完整路径；预览模式相对 /api/media）
      const base = bridge ? "/api/plug/astrbot_plugin_remote_link/media" : "/api/media";
      const url = base + "?filename=" + encodeURIComponent(f.filename || "");
      if (f.kind === "image") {
        return `<div class="media-preview"><img src="${url}" alt="${name}" loading="lazy" onclick="window.open(this.src)"><div class="dim">${name}</div></div>`;
      }
      if (f.kind === "video") {
        return `<div class="media-preview"><video src="${url}" controls preload="none"></video><div class="dim">${name}</div></div>`;
      }
      if (f.kind === "audio") {
        return `<div class="media-preview"><audio src="${url}" controls preload="none"></audio><div class="dim">${name}</div></div>`;
      }
      return `<div class="dim">${name}</div>`;
    },

    async queueAction(taskId, action, btn) {
      if (action === "send") {
        // 发送产物到来源会话（QQ 群）
        if (btn) { btn.disabled = true; btn.textContent = "发送中…"; }
        try {
          const r = await apiPost("task/send", { task_id: taskId });
          toast("📤 " + (r.ok ? (r.sent ? "已发送到会话" : "发送失败（见日志）") : (r.message || "操作失败")));
        } catch (e) {
          toast("❌ 发送失败：" + (e.message || e));
        }
        if (btn) { btn.disabled = false; btn.textContent = "📤 发送到QQ"; }
        return;
      }
      if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
      try {
        const r = await apiPost("overview/queue/action", { task_id: taskId, action });
        toast("✅ " + (r.ok ? (action === "approve" ? "已通过，开始生成" : "已拒绝") : "操作失败"));
        overview.fetchQueue();
      } catch (e) {
        toast("❌ 审批失败：" + (e.message || e));
        if (btn) { btn.disabled = false; btn.textContent = action === "approve" ? "✅ 通过并生成" : "🚫 拒绝"; }
      }
    },

    addEventLine(text) {
      const box = $("events");
      if (!box) return;
      const line = document.createElement("div");
      line.className = "line";
      line.innerHTML = `<span class="t">${fmtTime(Date.now() / 1000)}</span>${text}`;
      box.prepend(line);
      while (box.children.length > 100) box.removeChild(box.lastChild);
    },

    renderProgress(p) {
      const panel = $("progress-panel");
      if (!panel) return;
      if (!p) {
        panel.hidden = true;
        return;
      }
      panel.hidden = false;
      const pct = typeof p.percent === "number" ? p.percent : null;
      const fill = $("progress-fill");
      if (pct !== null) {
        fill.style.width = Math.max(2, Math.min(100, pct)) + "%";
        fill.classList.add("determinate");
      } else {
        fill.style.width = "100%";
        fill.classList.remove("determinate");
      }
      const base = p.text || "执行中…";
      const nodeName = p.node_type || "";
      $("progress-text").textContent = nodeName ? `${base} · 当前节点 ${nodeName}` : base;
    },

    async refresh() {
      try {
        const snap = await apiGet("overview/snapshot");
        overview.renderSnapshot(snap);
      } catch (e) {
        overview.addEventLine(`⚠️ 快照获取失败：${e.message || e}`);
      }
    },

    start() {
      if (overview.timer) return;
      overview.refresh();
      overview.timer = setInterval(() => {
        // 页面隐藏时跳过轮询（减少隧道占用，缓解卡顿）
        if (document.hidden) return;
        overview.refresh();
      }, 10000);
    },

    startSSE() {
      if (overview.sseOff || noSSE) return;
      try {
        overview.sseOff = subscribeEvents((d) => {
          if (d.connected !== undefined) {
            overview.addEventLine(`心跳 · 连接=${d.connected ? "在线" : "离线"} · 等待请求 ${d.pending_requests ?? 0}`);
          }
          if (d.progress !== undefined) {
            overview.renderProgress(d.progress);
            wf.onProgress(d.progress);
          }
          if (d.enhance_streams !== undefined && activeTab === "enhance") {
            enhance.onStreams(d.enhance_streams); // 子 LLM 增强流式实时更新（打字机）
          }
        });
      } catch (e) {
        overview.addEventLine(`⚠️ 事件流不可用：${e.message || e}`);
      }
    },

    stop() {
      if (overview.timer) {
        clearInterval(overview.timer);
        overview.timer = null;
      }
    },

    stopSSE() {
      if (overview.sseOff) {
        try { overview.sseOff(); } catch (_) {}
        overview.sseOff = null;
      }
    },
  };

  function subscribeEvents(onMsg) {
    if (bridge) {
      return bridge.subscribeSSE("overview/events", {
        onMessage(e) {
          try {
            const d = e.parsed || (e.raw ? JSON.parse(String(e.raw).replace(/^data:\s*/, "").trim()) : null);
            if (d) onMsg(d);
          } catch (_) {}
        },
        onError() {},
      });
    }
    const es = new EventSource("/api/overview/events");
    es.onmessage = (e) => {
      try { onMsg(JSON.parse(e.data)); } catch (_) {}
    };
    es.onerror = () => {};
    return () => es.close();
  }

  // ============================================================
  // 工作流（LiteGraph 只读渲染）
  // ============================================================
  const WIDGET_TYPES = ["INT", "FLOAT", "STRING", "BOOLEAN"];

  function isLinkSpec(spec) {
    if (typeof spec === "string") return true;
    if (Array.isArray(spec) && spec.length) {
      const head = spec[0];
      if (typeof head === "string") return !WIDGET_TYPES.includes(head);
      return false; // 首元素是选项列表 → widget 型
    }
    return false;
  }
  function flattenInputs(input) {
    const out = [];
    for (const key of ["required", "optional"]) {
      for (const [name, spec] of Object.entries((input || {})[key] || {})) {
        out.push([name, spec]);
      }
    }
    return out;
  }

  /** UI 格式 / API 格式 → 统一的 LiteGraph 展示格式 */
  function toDisplayFormat(wf, objectInfo) {
    if (wf && Array.isArray(wf.nodes) && wf.links) {
      const nodes = (wf.nodes || []).map((n) => ({
        id: n.id,
        type: n.type,
        pos: n.pos || [Math.random() * 600, Math.random() * 400],
        widgets_values: n.widgets_values || [],
        inputs: [],
        outputs: [],
      }));
      const links = (wf.links || []).map((l) => ({
        id: l[0],
        origin_id: l[1],
        origin_slot: l[2],
        target_id: l[3],
        target_slot: l[4],
        type: l[5],
      }));
      return { data: { nodes, links }, typesNeeded: [...new Set(nodes.map((n) => n.type))] };
    }
    const nodes = [];
    const links = [];
    let idx = 0;
    for (const [nid, node] of Object.entries(wf || {})) {
      if (!node || !node.class_type) continue;
      const x = (idx % 4) * 340 + 60;
      const y = Math.floor(idx / 4) * 260 + 60;
      idx++;
      const inputs = flattenInputs(((objectInfo || {})[node.class_type] || {}).input);
      const widgets = [];
      let li = 0;
      for (const [name, spec] of inputs) {
        if (isLinkSpec(spec)) {
          const v = (node.inputs || {})[name];
          if (Array.isArray(v) && v.length === 2) {
            links.push({
              id: links.length + 1,
              origin_id: Number(v[0]),
              origin_slot: v[1],
              target_id: Number(nid),
              target_slot: li,
              type: "*",
            });
          }
          li++;
        } else {
          widgets.push((node.inputs || {})[name]);
        }
      }
      nodes.push({ id: Number(nid), type: node.class_type, pos: [x, y], widgets_values: widgets, inputs: [], outputs: [] });
    }
    return { data: { nodes, links }, typesNeeded: [...new Set(nodes.map((n) => n.type))] };
  }

  const registeredTypes = {};
  function registerDynamicTypes(types, objectInfo) {
    for (const t of types) {
      if (registeredTypes[t] || (LiteGraph.registered_node_types && LiteGraph.registered_node_types[t])) continue;
      const info = (objectInfo || {})[t] || {};
      const inputs = flattenInputs(info.input);
      const widgetNames = inputs.filter(([, s]) => !isLinkSpec(s)).map(([n]) => n);

      function DynNode() {
        this.title = t;
        this.title_color = "#e6e8ec";
        this.bgcolor = "#23262e";
        this.color = "#2e3440";
        this.boxcolor = "#2e3440";
        this._widgetNames = widgetNames;
        this.inputs = [];
        this.outputs = [];
        this.properties = {};
        this.flags = {};
        for (const [name, spec] of inputs) {
          if (isLinkSpec(spec)) {
            this.addInput(name, Array.isArray(spec) ? spec[0] : spec);
          }
        }
        for (const out of info.output || []) {
          const label = typeof out === "string" ? out : out[0] || "OUT";
          this.addOutput(label, label);
        }
        this.size = [210, Math.max(56, 26 * Math.max(this.inputs.length, widgetNames.length) + 44)];
      }
      DynNode.title = t;
      DynNode.prototype.onDrawForeground = function (ctx) {
        if (this.flags && this.flags.collapsed) return;
        const wv = this.widgets_values || [];
        let y = (LiteGraph.NODE_TITLE_HEIGHT || 20) + 8;
        ctx.font = "10px Consolas, monospace";
        ctx.fillStyle = "#c8ccd4";
        ctx.textAlign = "left";
        const names = this._widgetNames || [];
        for (let i = 0; i < wv.length && i < names.length; i++) {
          let v = JSON.stringify(wv[i]);
          if (v === undefined) v = "—";
          if (v.length > 24) v = v.slice(0, 24) + "…";
          ctx.fillText(names[i] + ": " + v, 6, y);
          y += 13;
        }
      };
      LiteGraph.registerNodeType(t, DynNode);
      registeredTypes[t] = true;
    }
  }

  let wfCanvas = null;
  let wfGraph = null;
  let currentWf = null;
  let objectInfoCache = null;

  function fitGraph(cv, g, margin) {
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const n of g._nodes || []) {
      const sz = n.size || [140, 60];
      minx = Math.min(minx, n.pos[0]);
      miny = Math.min(miny, n.pos[1]);
      maxx = Math.max(maxx, n.pos[0] + sz[0]);
      maxy = Math.max(maxy, n.pos[1] + sz[1]);
    }
    if (!isFinite(minx)) return;
    const w = maxx - minx, h = maxy - miny;
    const cw = cv.canvas.width || 800, ch = cv.canvas.height || 500;
    const scale = Math.min(cw / (w + margin * 2), ch / (h + margin * 2), 1.2);
    const ds = cv.ds;
    ds.scale = scale;
    ds.offset[0] = (cw - w * scale) / 2 - minx * scale;
    ds.offset[1] = (ch - h * scale) / 2 - miny * scale;
    cv.setDirty(true, true);
  }

  function destroyGraph() {
    if (wfCanvas) {
      try { wfCanvas.setRendering(false); } catch (_) {}
      wfCanvas = null;
    }
    wfGraph = null;
  }

  function renderGraph(workflow, objectInfo) {
    destroyGraph();
    const container = $("graph-container");
    container.querySelectorAll("canvas").forEach((c) => c.remove());
    const display = toDisplayFormat(workflow, objectInfo);
    if (!display.data.nodes.length) {
      $("graph-tip").textContent = "工作流中没有可渲染的节点";
      return;
    }
    try {
      registerDynamicTypes(display.typesNeeded, objectInfo);
      const cv = document.createElement("canvas");
      cv.style.width = "100%";
      cv.style.height = "100%";
      container.appendChild(cv);
      wfGraph = new LiteGraph.LGraph();
      wfCanvas = new LiteGraph.LGraphCanvas(cv, wfGraph);
      wfCanvas.background_image = null;
      wfCanvas.clear_background = true;
      wfGraph.configure(display.data, true);
      // 显式重建连线：LiteGraph 的 configure 只创建 link 对象不连接节点，
      // 靠 node.connect 建立真实连接，连线才会画出来（否则只显示一个个板块）。
      try {
        if (wfGraph._links) wfGraph._links = {};
        const nodesById = {};
        for (const n of wfGraph._nodes || []) nodesById[String(n.id)] = n;
        for (const l of display.data.links || []) {
          const from = nodesById[String(l.origin_id)];
          const to = nodesById[String(l.target_id)];
          if (from && to && typeof from.connect === "function") {
            try {
              from.connect(l.origin_slot, to, l.target_slot, l.type || "*");
            } catch (_) {}
          }
        }
      } catch (e) {
        console.warn("连线重建失败:", e);
      }
      wfCanvas.allow_dragnodes = false;
      wfCanvas.allow_interaction = false;
      wfCanvas.allow_searchbox = false;
      wfCanvas.allow_reconnect_links = false;
      if (wfCanvas.resize) wfCanvas.resize();
      if (wfCanvas.setRendering) wfCanvas.setRendering(true);
      else if (wfCanvas.startRendering) wfCanvas.startRendering();
      setTimeout(() => {
        try {
          if (wfCanvas.resize) wfCanvas.resize();
          fitGraph(wfCanvas, wfGraph, 60);
        } catch (_) {}
      }, 120);
      $("graph-tip").textContent =
        `🖱️ 滚轮缩放 · 拖拽平移（只读展示）· ${display.data.nodes.length} 个节点 / ${display.data.links.length} 条连线`;
    } catch (e) {
      console.error("renderGraph failed:", e);
      $("graph-tip").textContent =
        "图形渲染失败（可能是复杂自定义节点），请用「查看 JSON」：" + (e && e.message ? e.message : String(e));
    }
  }

  const wf = {
    _hlNode: null,
    _hlColors: null,

    /** 运行时节点高亮：云端进度上报当前执行节点时，让图上对应节点发光。 */
    onProgress(p) {
      if (!wfGraph || !wfCanvas) return;
      const nodes = wfGraph._nodes || [];
      // 先恢复上一个高亮节点
      if (wf._hlNode) {
        if (wf._hlColors) {
          wf._hlNode.color = wf._hlColors.color;
          wf._hlNode.bgcolor = wf._hlColors.bgcolor;
        }
        wf._hlNode = null;
        wf._hlColors = null;
      }
      if (p && p.node != null) {
        const target = nodes.find((n) => String(n.id) === String(p.node));
        if (target) {
          wf._hlColors = { color: target.color, bgcolor: target.bgcolor };
          target.color = "#0d1017";
          target.bgcolor = "#5b8cff";
          wf._hlNode = target;
        }
      }
      try { wfCanvas.setDirty(true, true); } catch (_) {}
    },

    async loadList() {
      $("wf-list").textContent = "加载中…";
      try {
        let result;
        try {
          result = await apiGet("workflows/analysis"); // 含 LLM 识别结果与默认工作流
        } catch (_) {
          result = await apiGet("workflows/list"); // 老版本后端回退
        }
        const wfs = result.workflows || [];
        wf.defaultsCache = result.defaults || {};
        $("wf-count").textContent = `（${wfs.length} 个）`;
        if (!wfs.length) {
          $("wf-list").textContent = "本地没有读到工作流：请检查代理的 comfyui.userdata_dir 配置。";
          return [];
        }
        const box = $("wf-list");
        box.innerHTML = "";
        wfs.forEach((w) => {
          const row = document.createElement("div");
          row.className = "row";
          row.style.cssText = "padding:7px 4px;border-bottom:1px dashed #2b2f36;cursor:pointer;";
          const fmt = w.format === "api" ? "API" : w.format === "ui" ? "UI" : "?";
          const outIcon = (w.outputs || []).includes("video") ? "🎬" : (w.outputs || []).includes("image") ? "🖼️" : "";
          const inj = w.injects || {};
          const injectChips =
            (inj.images ? `<span class="badge info" style="margin-right:3px">🖼️ 入口×${inj.images}</span>` : "") +
            (inj.texts ? `<span class="badge info" style="margin-right:3px">📝 入口×${inj.texts}</span>` : "") +
            (inj.videos ? `<span class="badge warning" style="margin-right:3px">🎬 入口×${inj.videos}</span>` : "");
          const tokenChips = (w.tokens || []).slice(0, 6)
            .map((t) => `<span class="badge" style="margin-right:3px">${escapeHtml(t)}</span>`)
            .join("");
          // LLM 识别结果：类别/子类 + 标签
          const an = w.analysis || null;
          const analysisChips = an
            ? `<span class="badge ${an.category === "image" ? "success" : an.category === "video" ? "warning" : an.category === "audio" ? "info" : ""}" style="margin-right:3px">${escapeHtml(an.subtype || an.category)}</span>` +
              (an.tags || []).slice(0, 4).map((t) => `<span class="badge" style="margin-right:3px">${escapeHtml(t)}</span>`).join("")
            : '<span class="badge danger" style="margin-right:3px">未识别</span>';
          const summaryLine = an && an.summary ? `<br><span class="dim">${escapeHtml(an.summary)}</span>` : "";
          const defChips = [
            wf.defaultsCache.image === w.name ? "⭐默认图" : "",
            wf.defaultsCache.video === w.name ? "⭐默认视频" : "",
            wf.defaultsCache.audio === w.name ? "⭐默认音频" : "",
          ].filter(Boolean).map((t) => `<span class="badge warning" style="margin-right:3px">${t}</span>`).join("");
          row.innerHTML =
            `<span class="spacer"><b>${outIcon} ${escapeHtml(w.name)}</b><br><span class="dim">${escapeHtml(w.relpath || "")} · ${fmtSize(w.size)} · ${fmtDate(w.modified)}</span>${summaryLine}<br>${analysisChips}${defChips}${injectChips}${tokenChips}</span>` +
            `<button class="btn-del" data-name="${escapeHtml(w.name)}" title="编辑标签">🏷️</button>` +
            `<button class="btn-del" data-name="${escapeHtml(w.name)}" title="删除（会先备份）">✕</button>` +
            `<span class="badge ${w.format === "api" ? "info" : "success"}">${fmt}</span>`;
          row.onclick = () => wf.openWorkflow(w.name, fmt);
          const btns = row.querySelectorAll(".btn-del");
          // 第一个按钮 = 🏷️ 编辑标签；第二个 = ✕ 删除
          btns[0].onclick = (e) => {
            e.stopPropagation();
            wf.editTags(w, e.currentTarget);
          };
          btns[1].onclick = async (e) => {
            e.stopPropagation();
            if (!confirm(`确定删除工作流「${w.name}」？\n旧文件会保留为 .del.<时间戳>.json 备份。`)) return;
            try {
              await apiPost("workflows/delete", { name: w.name });
              toast("🗑️ 已删除（可手动恢复备份）");
              wf.loadList();
            } catch (err) {
              toast("❌ 删除失败：" + (err.message || err));
            }
          };
          box.appendChild(row);
        });
        return wfs;
      } catch (e) {
        $("wf-list").textContent = "加载失败：" + (e.message || e);
        return [];
      }
    },

    async openWorkflow(name, fmt) {
      $("wf-title").textContent = name;
      $("wf-fmt").textContent = fmt;
      $("wf-fmt").className = "badge " + (fmt === "api" ? "info" : "success");
      $("json-panel").style.display = "none";
      $("graph-tip").textContent = "加载中…";
      try {
        const [wfObj, oi] = await Promise.all([
          apiGet("workflows/read", { name }),
          objectInfoCache ? Promise.resolve(objectInfoCache) : apiGet("object_info"),
        ]);
        objectInfoCache = oi;
        currentWf = JSON.parse(wfObj.content);
        $("json-view").value = JSON.stringify(currentWf, null, 2);
        renderGraph(currentWf, oi);
      } catch (e) {
        $("graph-tip").textContent = "加载失败：" + (e.message || e);
      }
    },

    refit() {
      if (!wfCanvas) return;
      try {
        if (wfCanvas.resize) wfCanvas.resize();
        fitGraph(wfCanvas, wfGraph, 60);
      } catch (_) {}
    },

    /** 暂停 LiteGraph 持续渲染（切出工作流页时调用，避免空转重绘卡页面）。 */
    pauseRender() {
      if (wfCanvas && wfCanvas.setRendering) {
        try { wfCanvas.setRendering(false); } catch (_) {}
      }
    },

    resumeRender() {
      if (wfCanvas) {
        try {
          if (wfCanvas.setRendering) wfCanvas.setRendering(true);
          wf.refit();
        } catch (_) {}
      }
    },

    /** 工作流页定时自动同步（30s 轮询，页面隐藏时暂停；离开页面停止）。 */
    startAutoRefresh() {
      if (wf._refreshTimer) return;
      wf._refreshTimer = setInterval(() => {
        if (document.hidden) return;
        wf.loadList().catch(() => {});
      }, 30000);
    },
    stopAutoRefresh() {
      if (wf._refreshTimer) {
        clearInterval(wf._refreshTimer);
        wf._refreshTimer = null;
      }
    },

    /** 手动编辑工作流标签（分类/子类/标签），手动标记优先于 LLM 识别。 */
    async editTags(w, btnEl) {
      const an = w.analysis || {};
      const cat = an.category || "";
      const sub = an.subtype || "";
      const tags = (an.tags || []).join(", ");
      // 用简单 prompt 表单（分类 + 子类 + 标签）
      const catInput = prompt(
        `为工作流「${w.name}」设置分类：\n输入 image / video / audio / other（留空清除）`,
        cat
      );
      if (catInput === null) return;
      const subInput = prompt(`设置子类名称（如 text2image / 文生图，留空清除）：`, sub);
      if (subInput === null) return;
      const tagsInput = prompt(`设置标签（逗号分隔，最多 8 个）：`, tags);
      if (tagsInput === null) return;
      const body = { name: w.name };
      if (catInput.trim()) body.category = catInput.trim().toLowerCase();
      if (subInput.trim()) body.subtype = subInput.trim();
      body.tags = tagsInput.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      try {
        const r = await apiPost("workflows/tag", body);
        toast(`🏷️ 已保存「${w.name}」的标签`);
        wf.loadList();
      } catch (err) {
        toast("❌ 保存失败：" + (err.message || err));
      }
    },

    init() {
      $("btn-json").onclick = () => {
        if (!currentWf) return;
        const panel = $("json-panel");
        panel.style.display = panel.style.display === "none" ? "" : "none";
      };
      $("file-input").onchange = async (e) => {
        const file = e.target.files[0];
        e.target.value = "";
        if (!file) return;
        if (!file.name.endsWith(".json")) {
          toast("❌ 只支持 .json 工作流文件");
          return;
        }
        try {
          const content = await file.text();
          await apiPost("workflows/upload", { name: file.name.replace(/\.json$/, ""), content });
          toast("✅ 已上传：" + file.name);
          wf.loadList();
        } catch (err) {
          toast("❌ 上传失败：" + (err.message || err));
        }
      };
      wf.loadList().then((wfs) => {
        const openParam = new URLSearchParams(location.search).get("open");
        const target = wfs.find((w) => w.name === openParam) || wfs[0];
        if (target) wf.openWorkflow(target.name, target.format === "api" ? "API" : "UI");
      });
    },
  };

  // ============================================================
  // 预设
  // ============================================================
  const TYPE_BADGE = { string: "info", int: "success", float: "success", bool: "warning" };

  // ============================================================
  // 设置
  // ============================================================
  const DEFAULTS = {
    router_enabled: true,
    router_provider: "",
    router_temperature: 0.1,
    router_timeout: 60,
    server_host: "0.0.0.0",
    server_port: 8468,
    request_timeout: 300,
    comfyui_timeout: 3600,
    enable_shell: false,
    nsfw_enabled: false,
    notify_on_start: true,
    prompt_confirm_mode: "auto",
  };

  const settings = {
    fullToken: "",
    defaultPrompts: {},
    providersCache: [],

    /** 渲染子分类配置卡片（7 行：工作流 / LLM / 模型 / 提示词模板）。 */
    async renderSubtypes() {
      const box = $("subtype-configs");
      if (!box) return;
      try {
        const [cfg, wf] = await Promise.all([
          apiGet("settings/subtype_configs"),
          apiGet("workflows/analysis"),
        ]);
        const subtypes = cfg.subtypes || {};
        const labels = cfg.labels || {};
        const cats = cfg.categories || {};
        const workflows = wf.workflows || [];
        const providers = settings.providersCache || [];
        let html = "";
        for (const st of Object.keys(subtypes)) {
          const item = subtypes[st] || {};
          const cat = cats[st] || "";
          const wfOpts = ['<option value="">（未配置）</option>'].concat(
            workflows
              .filter((w) => !cat || (w.analysis || {}).category === cat)
              .map((w) => `<option value="${escapeHtml(w.name)}"${w.name === item.workflow ? " selected" : ""}>${escapeHtml(w.name)}</option>`)
          );
          const provOpts = ['<option value="">（跟随全局）</option>'].concat(
            providers.map((p) => `<option value="${escapeHtml(p.id)}"${p.id === item.llm_provider ? " selected" : ""}>${escapeHtml(p.name || p.id)}</option>`)
          );
          html += `
          <div class="st-row" data-st="${st}">
            <div class="st-head"><b>${escapeHtml(labels[st] || st)}</b><span class="badge ${cat === "image" ? "success" : cat === "video" ? "warning" : cat === "audio" ? "info" : ""}">${cat}</span>
              <span class="spacer"></span><button class="ghost st-reset" data-st="${st}" title="清空本行">↩ 清空</button></div>
            <div class="st-grid">
              <select class="st-wf" data-st="${st}" title="默认工作流">${wfOpts.join("")}</select>
              <select class="st-prov" data-st="${st}" title="子分类 LLM 提供商（已含模型）">${provOpts.join("")}</select>
              <textarea class="st-tpl" data-st="${st}" rows="3" placeholder="提示词模板（留空默认，支持 {policy}）">${escapeHtml(item.prompt_template)}</textarea>
            </div>
          </div>`;
        }
        html += `<div class="row" style="margin-top:10px"><button id="btn-save-subtypes" class="primary">💾 保存子分类配置</button></div>`;
        box.innerHTML = html;
        box.classList.remove("dim");
        box.querySelectorAll(".st-reset").forEach((b) => {
          b.onclick = () => {
            const st = b.dataset.st;
            box.querySelector(`.st-wf[data-st="${st}"]`).value = "";
            box.querySelector(`.st-prov[data-st="${st}"]`).value = "";
            box.querySelector(`.st-tpl[data-st="${st}"]`).value = "";
          };
        });
        const saveBtn = $("btn-save-subtypes");
        if (saveBtn) saveBtn.onclick = () => settings.saveSubtypes();
      } catch (e) {
        box.textContent = "加载失败：" + (e.message || e);
      }
    },

    async saveSubtypes() {
      const box = $("subtype-configs");
      const payload = {};
      box.querySelectorAll(".st-row").forEach((row) => {
        const st = row.dataset.st;
        payload[st] = {
          workflow: row.querySelector(".st-wf").value,
          llm_provider: row.querySelector(".st-prov").value,
          llm_model: "",
          prompt_template: row.querySelector(".st-tpl").value,
        };
      });
      const btn = $("btn-save-subtypes");
      if (btn) btn.disabled = true;
      try {
        await apiPost("settings/subtype_configs", payload);
        toast("✅ 子分类配置已保存");
      } catch (e) {
        toast("❌ 保存失败：" + (e.message || e));
      } finally {
        if (btn) btn.disabled = false;
      }
    },

    fillProviders(providers, selected) {
      const sel = $("router_provider");
      sel.innerHTML = '<option value="">默认 · 跟随 AstrBot 当前提供商</option>';
      for (const p of providers || []) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name || p.id;
        if (p.id === selected) opt.selected = true;
        sel.appendChild(opt);
      }
    },

    renderTools(tools) {
      const box = $("tools-list");
      box.innerHTML = "";
      if (!tools || !tools.length) {
        box.textContent = "未读取到本插件的工具（可能版本不支持工具开关 API）";
        return;
      }
      for (const t of tools) {
        const row = document.createElement("div");
        row.className = "field";
        row.style.gridTemplateColumns = "1fr auto";
        row.innerHTML =
          `<div><b class="mono">${escapeHtml(t.name)}</b><br><span class="dim" style="font-size:11.5px">${escapeHtml(t.description || "")}</span></div>` +
          `<label class="switch"><input type="checkbox" data-tool="${escapeHtml(t.name)}" ${t.active ? "checked" : ""}><span class="slider"></span></label>`;
        box.appendChild(row);
      }
    },

    async load() {
      try {
        const d = await apiGet("settings/get");
        const c = d.config || {};
        $("router_enabled").checked = !!c.router_enabled;
        $("router_temperature").value = c.router_temperature ?? 0.1;
        $("router_timeout").value = c.router_timeout ?? 60;
        $("server_host").value = c.server_host ?? "0.0.0.0";
        $("server_port").value = c.server_port ?? 8468;
        $("request_timeout").value = c.request_timeout ?? 300;
        $("comfyui_timeout").value = c.comfyui_timeout ?? 3600;
        $("enable_shell").checked = !!c.enable_shell;
        $("nsfw_enabled").checked = !!c.nsfw_enabled;
        $("notify_on_start").checked = c.notify_on_start === undefined ? true : !!c.notify_on_start;
        $("prompt_confirm_mode").value = c.prompt_confirm_mode || "auto";
        settings.fullToken = c.auth_token || "";
        settings.defaultPrompts = d.defaults || {};
        $("router_system_prompt").value = c.router_system_prompt || settings.defaultPrompts.router_system_prompt || "";
        $("auth_token_view").value = settings.fullToken || c.auth_token_masked || "未设置";
        settings.providersCache = d.providers || [];
        settings.fillProviders(d.providers || [], c.router_provider || "");
        settings.renderTools(d.tools || []);
        const m = d.media || {};
        $("media-info").textContent = `${m.count ?? 0} 个文件 · ${m.size_mb ?? 0} MB`;
      } catch (e) {
        toast("❌ 设置加载失败：" + (e.message || e));
      }
    },

    async save() {
      const payload = {
        router_enabled: $("router_enabled").checked,
        router_provider: $("router_provider").value,
        router_temperature: parseFloat($("router_temperature").value || "0.1"),
        router_timeout: parseInt($("router_timeout").value || "60", 10),
        server_host: $("server_host").value.trim(),
        server_port: parseInt($("server_port").value || "8468", 10),
        request_timeout: parseInt($("request_timeout").value || "300", 10),
        comfyui_timeout: parseInt($("comfyui_timeout").value || "3600", 10),
        enable_shell: $("enable_shell").checked,
        nsfw_enabled: $("nsfw_enabled").checked,
        notify_on_start: $("notify_on_start").checked,
        prompt_confirm_mode: $("prompt_confirm_mode").value || "auto",
        router_system_prompt: $("router_system_prompt").value,
      };
      if (payload.server_port < 1 || payload.server_port > 65535) {
        toast("❌ 端口需在 1~65535 之间");
        return;
      }
      const btn = $("btn-settings-save");
      if (btn) btn.disabled = true;
      try {
        const r = await apiPost("settings/save", payload);
        toast("✅ 已保存" + (r.note ? " · " + r.note : ""));
      } catch (e) {
        toast("❌ 保存失败：" + (e.message || e));
      } finally {
        if (btn) btn.disabled = false;
      }
    },

    async genToken() {
      try {
        const r = await apiPost("settings/gen_token");
        if (r.token) {
          settings.fullToken = r.token;
          $("auth_token_view").value = r.token;
          toast("🔑 已生成新 token：" + r.hint);
        }
      } catch (e) {
        toast("❌ 生成失败：" + (e.message || e));
      }
    },

    async clearMedia() {
      try {
        const r = await apiPost("settings/clear_media");
        toast(`🗑️ 已清理 ${r.removed} 个文件（${r.freed_mb} MB）`);
        settings.load();
      } catch (e) {
        toast("❌ 清理失败：" + (e.message || e));
      }
    },

    reset() {
      $("router_enabled").checked = DEFAULTS.router_enabled;
      $("router_provider").value = DEFAULTS.router_provider;
      $("router_temperature").value = DEFAULTS.router_temperature;
      $("router_timeout").value = DEFAULTS.router_timeout;
      $("server_host").value = DEFAULTS.server_host;
      $("server_port").value = DEFAULTS.server_port;
      $("request_timeout").value = DEFAULTS.request_timeout;
      $("comfyui_timeout").value = DEFAULTS.comfyui_timeout;
      $("enable_shell").checked = DEFAULTS.enable_shell;
      $("nsfw_enabled").checked = DEFAULTS.nsfw_enabled;
      $("notify_on_start").checked = DEFAULTS.notify_on_start;
      $("prompt_confirm_mode").value = DEFAULTS.prompt_confirm_mode;
      toast("已填入默认值，记得点「保存全部设置」");
    },

    toggleTokenVisible() {
      const input = $("auth_token_view");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      $("btn-token-eye").textContent = show ? "🙈" : "👁";
    },

    // 旧的「默认工作流」下拉卡片已由「子分类配置」取代（reloadDefaults/saveDefaults 移除）

    init() {
      $("btn-token").onclick = () => settings.genToken();
      $("btn-token-eye").onclick = () => settings.toggleTokenVisible();
      $("btn-clear-media").onclick = () => settings.clearMedia();
      $("btn-reset").onclick = () => settings.reset();
      $("btn-reset-router").onclick = () => {
        $("router_system_prompt").value = settings.defaultPrompts.router_system_prompt || "";
        toast("已恢复调度决策提示词默认值，记得点「保存全部设置」");
      };
      $("tools-list").addEventListener("change", async (e) => {
        const input = e.target;
        if (!input.dataset.tool) return;
        try {
          await apiPost("settings/save", {});
          await apiPost("settings/toggle_tool", { name: input.dataset.tool, active: input.checked });
          toast(`${input.checked ? "✅ 已启用" : "⛔ 已停用"} ${input.dataset.tool}`);
        } catch (err) {
          toast("❌ 工具切换失败：" + (err.message || err));
          input.checked = !input.checked;
        }
      });
      settings.load();
      settings.renderSubtypes();
    },
  };

  // ============================================================
  // 增强对话（独立页面：聊天室式对话展示 + 打字机效果 + 初始化）
  // ============================================================
  const enhance = {
    _typedRounds: {}, // subtype -> 已打字轮数（避免重复打字动画）
    _live: {},        // subtype -> 是否正在流式实时生成中（SSE 更新，不打字机）
    _timer: null,

    /** SSE 实时推送：增强中的提示词逐 token 更新到对应窗口（打字机实时） */
    onStreams(streams) {
      if (!streams || typeof streams !== "object") return;
      Object.entries(streams).forEach(([sub, st]) => {
        if (!st || st.done) {
          // 完成：下一轮 load() 会完整渲染该轮并打字机
          delete enhance._live[sub];
          return;
        }
        enhance._live[sub] = true;
        const el = document.getElementById("live-" + sub);
        if (el) {
          el.textContent = st.text || "";
          el.classList.add("typing");
        } else {
          // 窗口还没渲染（如刚初始化）→ 立即拉取并渲染
          enhance.load();
        }
      });
    },

    /** 打字机效果：一个字一个字蹦出来 */
    typewriter(el, text, speed) {
      if (!el) return;
      el.classList.add("typing");
      el.textContent = "";
      let i = 0;
      speed = speed || 15;
      const step = () => {
        if (i >= text.length) {
          clearInterval(timer);
          el.classList.remove("typing");
          return;
        }
        el.textContent = text.slice(0, ++i);
      };
      const timer = setInterval(step, speed);
    },

    async load() {
      const box = $("enhance-sessions");
      const info = $("enhance-session-info");
      if (!box) return;
      if (!enhance._inited) {
        box.innerHTML = "加载中…";
        enhance._inited = true;
      }
      try {
        const r = await apiGet("enhance/sessions");
        const sessions = r.sessions || {};
        if (info) info.textContent = `${r.count || 0} 个子分类对话`;
        const keys = Object.keys(sessions);
        if (!keys.length) {
          box.innerHTML = '<span class="dim">还没有已初始化的对话——点上方「⚡ 初始化全部对话」，为已配置模板的子分类预创建对话。</span>';
          return;
        }
        box.innerHTML = keys.map((sub) => {
          const s = sessions[sub] || {};
          const hist = (s.history || []);
          const bubbles = hist.map((h, i) => {
            const isLast = i === hist.length - 1;
            // 最后一条 AI 气泡：若正在流式生成，用 live-<sub>（SSE 实时更新）；否则固定气泡
            const bid = isLast ? "live-" + sub : "bubble-" + sub + "-" + i;
            const typing = isLast && enhance._live[sub] ? " typing" : "";
            const negHtml = h.negative ? `<div class="chat-bubble neg">🚫 负面提示词：${escapeHtml(h.negative)}</div>` : "";
            return `<div class="chat-row">
               <div class="chat-bubble user">${escapeHtml(h.user || "")}</div>
               <div class="chat-bubble ai${typing}" id="${bid}">${escapeHtml(h.assistant || "（生成中…）")}</div>
               ${negHtml}
             </div>`;
          }).join("") || '<div class="dim" style="padding:8px">对话已创建，还没有生成过</div>';
          return `<div class="chat-window">
            <div class="chat-head">
              <span class="badge info">${escapeHtml(s.label || sub)}</span>
              <b>${escapeHtml(sub)}</b>
              ${s.workflow ? `<span class="dim">→ ${escapeHtml(s.workflow)}</span>` : ""}
              <span class="dim" style="margin-left:auto">${hist.length} 轮</span>
            </div>
            <details class="fold"><summary>🧠 system prompt（${(s.system || "").length} 字，点击查看）</summary><div class="fold-body">${escapeHtml(s.system || "")}</div></details>
            <div class="chat-body">${bubbles}</div>
          </div>`;
        }).join("");
        // 打字机：对每个子分类「新增」且不在流式实时中的最新一轮助手回复逐字显示
        keys.forEach((sub) => {
          if (enhance._live[sub]) return; // SSE 实时更新中，不打字机
          const rounds = (sessions[sub] || {}).history || [];
          const typed = enhance._typedRounds[sub] || 0;
          if (rounds.length > typed) {
            const el = document.getElementById("live-" + sub) || document.getElementById("bubble-" + sub + "-" + (rounds.length - 1));
            if (el && !el.dataset.typed) {
              el.dataset.typed = "1";
              enhance.typewriter(el, rounds[rounds.length - 1].assistant || "", 15);
            }
            enhance._typedRounds[sub] = rounds.length;
          }
        });
        // 滚动到底部
        try { box.scrollTop = box.scrollHeight; } catch (_) {}
      } catch (e) {
        box.innerHTML = '<span class="dim">加载失败：' + escapeHtml(e.message || e) + "</span>";
      }
    },

    async initSessions() {
      // 注意：AstrBot 插件页 iframe 会拦截 window.confirm（返回 false 导致"没反应"），
      // 初始化是安全操作（重建对话），直接执行。
      try {
        const r = await apiPost("enhance/reset", {});
        toast("⚡ " + (r.message || "已初始化"));
        enhance._typedRounds = {};
        enhance._live = {};
        enhance.load();
      } catch (e) {
        toast("❌ 初始化失败：" + (e.message || e));
      }
    },

    startPolling() {
      if (enhance._timer) return;
      enhance.load();
      enhance._timer = setInterval(() => {
        if (document.hidden) return;
        enhance.load();
      }, 8000); // 8 秒轮询（生成中的流式由 SSE 实时推送；空闲降频省资源）
    },
    stopPolling() {
      if (enhance._timer) { clearInterval(enhance._timer); enhance._timer = null; }
    },

    init() {
      const b1 = $("btn-enhance-init");
      const b2 = $("btn-enhance-refresh");
      if (b1) b1.onclick = () => enhance.initSessions();
      if (b2) b2.onclick = () => enhance.load();
      enhance.load();
    },
  };

  // ============================================================
  // 产物库（所有历史产物：预览 / 下载 / 发送到群聊）
  // ============================================================
  const gallery = {
    async load() {
      const box = $("gallery-grid");
      if (!box) return;
      try {
        const r = await apiGet("overview/queue");
        const queue = r.queue || [];
        const items = [];
        for (const t of queue) {
          for (const f of (t.files || [])) {
            items.push({ task_id: t.task_id, origin: t.origin || "", kind: f.kind, filename: f.filename });
          }
        }
        if (!items.length) { box.innerHTML = '<span class="dim">暂无产物（先跑一个生成任务）</span>'; return; }
        const base = bridge ? "/api/plug/astrbot_plugin_remote_link/media" : "/api/media";
        box.innerHTML = items.map((f, i) => {
          const url = base + "?filename=" + encodeURIComponent(f.filename || "");
          const media = f.kind === "image"
            ? `<img src="${url}" loading="lazy" onclick="window.open(this.src)" alt="">`
            : f.kind === "video"
            ? `<video src="${url}" controls preload="none"></video>`
            : f.kind === "audio"
            ? `<audio src="${url}" controls preload="none"></audio>`
            : `<div class="dim" style="padding:8px">${escapeHtml(f.filename || "?")}</div>`;
          const canSend = f.origin && !String(f.origin).startsWith("web:");
          return `<div class="media-card">${media}
            <div class="name">${escapeHtml(f.filename || "?")}</div>
            <div class="ops">
              <a class="q-btn" href="${url}&download=1" target="_blank" rel="noopener">📥 下载</a>
              ${canSend ? `<button class="q-btn" data-task="${escapeHtml(f.task_id)}">📤 群聊</button>` : ""}
            </div></div>`;
        }).join("");
        box.querySelectorAll("[data-task]").forEach((b) => {
          b.onclick = () => gallery.sendToQq(b.dataset.task, b);
        });
      } catch (e) {
        box.innerHTML = '<span class="dim">加载失败：' + escapeHtml(e.message || e) + "</span>";
      }
    },
    async sendToQq(taskId, btn) {
      if (btn) { btn.disabled = true; btn.textContent = "发送中…"; }
      try {
        const r = await apiPost("task/send", { task_id: taskId });
        toast("📤 " + (r.ok ? (r.sent ? "已发送到会话" : "发送失败（见日志）") : (r.message || "操作失败")));
      } catch (e) {
        toast("❌ 发送失败：" + (e.message || e));
      }
      if (btn) { btn.disabled = false; btn.textContent = "📤 群聊"; }
    },
    init() { gallery.load(); },
  };

  // ============================================================
  // 入口
  // ============================================================
  const noSSE = new URLSearchParams(location.search).has("nosse");

  async function main() {
    $("btn-back").onclick = goBack;
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.onclick = () => showTab(btn.dataset.tab);
    });

    if (bridge) {
      try { await bridge.ready(); } catch (_) {}
      $("mode-badge").textContent = "AstrBot 插件页";
      $("mode-badge").className = "badge success";
    } else {
      const m = $("mode-badge");
      if (m) m.textContent = "预览模式";
    }

    window.addEventListener("beforeunload", () => {
      overview.stop();
      overview.stopSSE();
      wf.stopAutoRefresh();
    });

    // 提交生成任务（Web 控制台入口，与 QQ 群同一套内生调度；类型按钮=明确指令）
    const intentInput = $("task-intent");
    const submitBtn = $("btn-submit-task");
    let taskKind = ""; // 当前选中的类型（图片/视频/音频/空=智能）
    let taskImages = []; // 上传参考图 base64 列表（图生图/图生视频）
    const kindBtns = document.querySelectorAll("#task-kind-btns .kbtn");
    if (kindBtns.length) {
      kindBtns.forEach((b) => {
        b.onclick = () => {
          taskKind = b.dataset.kind || "";
          kindBtns.forEach((x) => x.classList.toggle("active", x === b));
        };
      });
    }
    const pickBtn = $("btn-pick-image");
    const imgInput = $("task-image");
    if (pickBtn && imgInput) {
      pickBtn.onclick = () => imgInput.click();
      imgInput.onchange = async () => {
        taskImages = [];
        const names = [];
        for (const f of imgInput.files || []) {
          if (f.size > 20 * 1024 * 1024) continue;
          taskImages.push(await fileToBase64(f));
          names.push(f.name);
        }
        const lbl = $("task-image-names");
        if (lbl) lbl.textContent = names.length ? "🖼️ " + names.join(", ") : "";
      };
    }
    function fileToBase64(file) {
      return new Promise((res, rej) => {
        const rd = new FileReader();
        rd.onload = () => res(String(rd.result || "").split(",")[1] || "");
        rd.onerror = rej;
        rd.readAsDataURL(file);
      });
    }
    if (intentInput && submitBtn) {
      const doSubmit = async () => {
        let intent = (intentInput.value || "").trim();
        if (!intent) { toast("❌ 先输入要生成的内容描述"); return; }
        // 选了类型 → 加明确前缀（路由关键词直接命中，不靠 LLM 猜分类）
        if (taskKind === "图片" && !/图|照片|插画|头像|壁纸|海报/.test(intent)) intent = "生成一张图片：" + intent;
        else if (taskKind === "视频" && !/视频|动画|短片/.test(intent)) intent = "生成一段视频：" + intent;
        else if (taskKind === "音频" && !/音乐|音频|歌曲/.test(intent)) intent = "生成一段音频：" + intent;
        submitBtn.disabled = true;
        submitBtn.textContent = "提交中…";
        try {
          const body = { intent };
          if (taskImages.length) body.images = taskImages;
          const r = await apiPost("task/submit", body);
          toast("🚀 已提交任务 " + r.task_id + (r.images ? `（含 ${r.images} 张参考图）` : "") + "，正在本地调度生成…");
          intentInput.value = "";
          taskImages = [];
          if (imgInput) imgInput.value = "";
          const lbl = $("task-image-names");
          if (lbl) lbl.textContent = "";
          overview.fetchQueue();
        } catch (e) {
          toast("❌ 提交失败：" + (e.message || e));
        }
        submitBtn.disabled = false;
        submitBtn.textContent = "🚀 生成";
      };
      submitBtn.onclick = doSubmit;
      intentInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSubmit(); });
    }

    overview.startSSE();

    const fromUrl = new URLSearchParams(location.search).get("tab");
    showTab(fromUrl && TABS.includes(fromUrl) ? fromUrl : "overview");
  }

  main().catch((e) => toast("❌ 页面初始化失败：" + (e.message || e)));
})();
