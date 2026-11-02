// AI 量化交易系统 -- 通用前端工具
// =============================================================
// 提供:  请求封装 / 数字格式化 / 时间格式化 / 通知

window.App = (function () {
  'use strict';

  /** REST 请求 (GET) */
  async function request(url, opts = {}) {
    opts.headers = Object.assign(
      { 'Content-Type': 'application/json' },
      opts.headers || {}
    );
    if (opts.body && typeof opts.body !== 'string') {
      opts.body = JSON.stringify(opts.body);
    }
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      return await r.json();
    }
    return await r.text();
  }

  const get = (url) => request(url, { method: 'GET' });

  /**
   * POST：HTTP 非 2xx 时统一成 { ok: false, message }，避免业务里 typeof r === 'object' && r.ok 误判
   */
  async function post(url, body) {
    const opts = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    };
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const data = await r.json();
      if (!r.ok) {
        let msg = 'HTTP ' + r.status;
        if (data && data.detail !== undefined) {
          msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
        } else if (data && data.message) {
          msg = data.message;
        }
        return { ok: false, message: msg };
      }
      return data;
    }
    const text = await r.text();
    if (!r.ok) {
      return {
        ok: false,
        message: 'HTTP ' + r.status + (text ? ': ' + text.slice(0, 160) : ''),
      };
    }
    return text;
  }

  /** 数字格式化 */
  function fmtNum(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return '-';
    return Number(v).toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }
  function fmtPct(v, digits = 2) {
    if (v === null || v === undefined || isNaN(v)) return '-';
    return (Number(v) * 100).toFixed(digits) + '%';
  }
  function fmtSign(v, digits = 2, suffix = '') {
    if (v === null || v === undefined || isNaN(v)) return '-';
    const n = Number(v);
    const sign = n > 0 ? '+' : '';
    return sign + n.toFixed(digits) + suffix;
  }
  function pnlClass(v) {
    if (v === null || v === undefined || isNaN(v)) return '';
    return Number(v) > 0 ? 'pos' : (Number(v) < 0 ? 'neg' : '');
  }

  /** 信号 / 订单时间戳格式化:
   *  ISO 输入 (YYYY-MM-DDTHH:MM:SS) -> 短显示
   *  - 日 K 回放钉在 15:00: 只显示日期 (09-17), 避免看成盘中整点触发
   *  - 盘中真实信号: '09-18 10:09'
   */
  function fmtSignalTs(ts) {
    if (!ts || typeof ts !== 'string') return '-';
    const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T\s](\d{2}):(\d{2})(?::\d{2})?)?/);
    if (!m) return ts;
    const hm = (m[4] && m[5]) ? `${m[4]}:${m[5]}` : '';
    if (!hm || hm === '15:00') {
      return `${m[2]}-${m[3]}`;
    }
    return `${m[2]}-${m[3]} ${hm}`;
  }

  /** 简单 toast */
  function toast(msg, kind = 'info') {
    const el = document.createElement('div');
    const colors = {
      info:    'bg-indigo-600',
      success: 'bg-green-600',
      warn:    'bg-yellow-600',
      danger:  'bg-red-600',
    };
    el.className =
      'fixed top-4 right-4 z-[200] max-w-md px-4 py-3 rounded-lg text-white shadow-2xl text-sm font-medium leading-snug ring-2 ring-white/20 ' +
      (colors[kind] || colors.info);
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  // 事件流 / 告警 -- 把 events 数组里的元素映射成 {level, time, msg, badge_class}
  // 兼容两种来源:
  //   1) 主循环 loop_cycle 事件: {ts, type:"loop_cycle", signal_count, duration_ms}
  //   2) 控制/告警事件: {ts, level: "INFO|WARN|CRITICAL|FATAL", title, message?, source?}
  // 友好化: 事件流面向用户, 不显示原始 type/字段名, 翻译成中文
  const _LEVEL_TEXT = {
    INFO:     '提示',
    WARN:     '警告',
    CRITICAL: '严重',
    FATAL:    '致命',
    TICK:     '心跳',
  };
  function alertLevelText(e) {
    if (!e) return '';
    const lv = (e.level || (e.type === 'loop_cycle' ? 'TICK' : 'INFO')).toUpperCase();
    return _LEVEL_TEXT[lv] || lv;
  }
  function alertBadgeClass(e) {
    if (!e) return 'badge-gray';
    const lv = (e.level || (e.type === 'loop_cycle' ? 'TICK' : 'INFO')).toUpperCase();
    const map = {
      INFO:     'badge-info',
      WARN:     'badge-warning',
      CRITICAL: 'badge-danger',
      FATAL:    'badge-danger',
      TICK:     'badge-gray',
    };
    return map[lv] || 'badge-gray';
  }
  function alertTimeText(e) {
    if (!e || !e.ts) return '';
    const m = String(e.ts).match(/(\d{2}):(\d{2}):(\d{2})/);
    return m ? `${m[1]}:${m[2]}:${m[3]}` : String(e.ts).slice(-8);
  }
  function alertMsgText(e) {
    if (!e) return '';
    if (e.type === 'loop_cycle') {
      const sc = e.signal_count != null ? e.signal_count : 0;
      const ms = e.duration_ms != null ? e.duration_ms : 0;
      const tail = sc === 0 ? '本轮无新信号' : `本轮触发 ${sc} 个信号`;
      return `策略巡检 · ${tail} · 耗时 ${ms}ms`;
    }
    const head = e.title || e.message || '';
    const src  = e.source ? ` (来自 ${e.source})` : '';
    return head + src;
  }

  /** 简单移动平均 (前端画 MA 用) */
  function _sma(closes, win) {
    const out = new Array(closes.length).fill(null);
    let sum = 0;
    for (let i = 0; i < closes.length; i++) {
      sum += closes[i];
      if (i >= win) sum -= closes[i - win];
      if (i >= win - 1) out[i] = +(sum / win).toFixed(4);
    }
    return out;
  }

  /**
   * 统一日 K 图 (live 弹窗 / 回测页共用)
   * @param {string|HTMLElement} elOrId  容器 id 或 DOM
   * @param {object} opts
   *   bars:   [{date,open,high,low,close,volume}, ...]
   *   trades: [{date,side:'buy'|'sell',price}, ...]  可选, 买卖点
   *   cost:   number  可选, 成本参考线
   *   heightHint: 触发 resize 用, 无实际高度控制
   */
  function renderStockKline(elOrId, opts) {
    opts = opts || {};
    const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
    const bars = opts.bars || [];
    if (!el || !window.Plotly || !bars.length) return;

    const x = bars.map(b => b.date);
    const opens = bars.map(b => b.open);
    const highs = bars.map(b => b.high);
    const lows = bars.map(b => b.low);
    const closes = bars.map(b => b.close);
    const vols = bars.map(b => b.volume || 0);
    const volColors = bars.map(b =>
      b.close >= b.open ? 'rgba(239,68,68,0.55)' : 'rgba(16,185,129,0.55)'
    );
    const ma5 = _sma(closes, 5);
    const ma10 = _sma(closes, 10);
    const ma20 = _sma(closes, 20);

    const candle = {
      type: 'candlestick',
      x: x,
      open: opens, high: highs, low: lows, close: closes,
      increasing: { line: { color: '#ef4444', width: 1.2 }, fillcolor: '#ef4444' },
      decreasing: { line: { color: '#10b981', width: 1.2 }, fillcolor: '#10b981' },
      whiskerwidth: 0.4,
      name: 'K',
      xaxis: 'x', yaxis: 'y',
      hoverlabel: { bgcolor: '#1e293b', font: { color: '#f8fafc', size: 12 } },
      hovertext: bars.map(b => {
        const chg = b.open ? ((b.close - b.open) / b.open * 100) : 0;
        return b.date
          + '<br>开 ' + Number(b.open).toFixed(2)
          + '  高 ' + Number(b.high).toFixed(2)
          + '<br>低 ' + Number(b.low).toFixed(2)
          + '  收 ' + Number(b.close).toFixed(2)
          + '<br>振幅 ' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%'
          + '<br>量 ' + (b.volume || 0).toLocaleString('zh-CN');
      }),
      hoverinfo: 'text',
      showlegend: false,
    };

    const mkMa = (y, name, color) => ({
      type: 'scatter', mode: 'lines', x: x, y: y,
      name: name,
      line: { color: color, width: 1.3 },
      hovertemplate: name + ' %{y:.2f}<extra></extra>',
      xaxis: 'x', yaxis: 'y',
    });

    const volume = {
      type: 'bar', x: x, y: vols,
      marker: { color: volColors, line: { width: 0 } },
      name: '成交量',
      xaxis: 'x', yaxis: 'y2',
      hovertemplate: '%{x}<br>量 %{y:,}<extra></extra>',
      showlegend: false,
    };

    const traces = [
      candle,
      mkMa(ma5, 'MA5', '#6366f1'),
      mkMa(ma10, 'MA10', '#f59e0b'),
      mkMa(ma20, 'MA20', '#8b5cf6'),
      volume,
    ];

    const trades = opts.trades || [];
    const buys = trades.filter(t => t.side === 'buy');
    const sells = trades.filter(t => t.side === 'sell');
    if (buys.length) {
      traces.push({
        type: 'scatter', mode: 'markers',
        x: buys.map(t => t.date),
        y: buys.map(t => t.price),
        marker: {
          color: '#dc2626', size: 11, symbol: 'triangle-up',
          line: { color: '#fff', width: 1 },
        },
        name: '买',
        hovertemplate: '%{x}<br>买 %{y:.2f}<extra></extra>',
        xaxis: 'x', yaxis: 'y',
      });
    }
    if (sells.length) {
      traces.push({
        type: 'scatter', mode: 'markers',
        x: sells.map(t => t.date),
        y: sells.map(t => t.price),
        marker: {
          color: '#059669', size: 11, symbol: 'triangle-down',
          line: { color: '#fff', width: 1 },
        },
        name: '卖',
        hovertemplate: '%{x}<br>卖 %{y:.2f}<extra></extra>',
        xaxis: 'x', yaxis: 'y',
      });
    }

    const shapes = [];
    const annotations = [];
    const cost = Number(opts.cost) || 0;
    if (cost > 0) {
      shapes.push({
        type: 'line', xref: 'paper', x0: 0, x1: 1,
        yref: 'y', y0: cost, y1: cost,
        line: { color: '#f59e0b', width: 1.2, dash: 'dot' },
      });
      annotations.push({
        xref: 'paper', x: 1, xanchor: 'right',
        yref: 'y', y: cost, yanchor: 'bottom',
        text: '成本 ' + cost.toFixed(2),
        showarrow: false,
        font: { size: 10, color: '#d97706' },
        bgcolor: 'rgba(255,251,235,0.85)',
        borderpad: 2,
      });
    }

    const layout = {
      margin: { l: 48, r: 52, t: 32, b: 40 },
      paper_bgcolor: '#fafbfc',
      plot_bgcolor: '#fafbfc',
      hovermode: 'x',
      // 只看图, 不需要拖拽 / 缩放
      dragmode: false,
      shapes: shapes,
      annotations: annotations,
      xaxis: {
        type: 'category',
        rangeslider: { visible: false },
        showgrid: false,
        tickfont: { size: 10, color: '#94a3b8' },
        nticks: 8,
        anchor: 'y2',
        fixedrange: true,
      },
      yaxis: {
        domain: [0.30, 1],
        tickfont: { size: 10, color: '#64748b' },
        gridcolor: 'rgba(148,163,184,0.25)',
        zeroline: false,
        side: 'right',
        fixedrange: true,
      },
      yaxis2: {
        domain: [0, 0.22],
        tickfont: { size: 9, color: '#94a3b8' },
        showgrid: false,
        zeroline: false,
        side: 'right',
        fixedrange: true,
      },
      legend: {
        orientation: 'h',
        x: 0, y: 1.14,
        font: { size: 11, color: '#64748b' },
        bgcolor: 'rgba(0,0,0,0)',
      },
      showlegend: true,
    };

    Plotly.react(el, traces, layout, {
      displayModeBar: false,
      responsive: true,
      staticPlot: false,
    });
    requestAnimationFrame(() => {
      try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ }
    });
    setTimeout(() => {
      try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ }
    }, 80);
  }

  return {
    get, post, request,
    fmtNum, fmtPct, fmtSign, pnlClass, fmtSignalTs, toast,
    alertLevelText, alertBadgeClass, alertTimeText, alertMsgText,
    renderStockKline,
  };
})();

// 顶部 Tab 不整页跳走: 投研对话 iframe 一直留着, 切去实盘/回测再回来内容还在
(function initChatKeepAlive() {
  if (window !== window.top) {
    document.body.classList.add('is-embed');
    return;
  }

  var main = document.getElementById('app-main');
  var chatDock = document.getElementById('chat-dock');
  var chatKeep = document.getElementById('chat-keep');
  var pageKeep = document.getElementById('page-keep');
  if (!main || !chatDock || !chatKeep || !pageKeep) return;

  function normPath(href) {
    var u = new URL(href, location.origin);
    var p = u.pathname.replace(/\/$/, '');
    return p || '/';
  }

  var initialPath = normPath(location.href);

  function isChat(path) {
    return path === '/chat';
  }

  function setActive(path) {
    document.querySelectorAll('.nav-tab').forEach(function (a) {
      var p = normPath(a.getAttribute('href') || '');
      var on = p === path || (path.indexOf('/live') === 0 && p.indexOf('/live') === 0);
      a.classList.toggle('active', on);
    });
  }

  function showChat() {
    if (!chatKeep.getAttribute('src')) {
      chatKeep.src = '/gradio-chat/';
    }
    main.hidden = true;
    pageKeep.hidden = true;
    chatDock.hidden = false;
    setActive('/chat');
  }

  function showInitial() {
    chatDock.hidden = true;
    pageKeep.hidden = true;
    main.hidden = false;
    setActive(initialPath);
  }

  function showOther(path) {
    chatDock.hidden = true;
    main.hidden = true;
    if (pageKeep.dataset.path !== path) {
      pageKeep.src = path;
      pageKeep.dataset.path = path;
    }
    pageKeep.hidden = false;
    setActive(path);
  }

  function go(path, push) {
    if (isChat(path)) {
      showChat();
    } else if (path === initialPath) {
      showInitial();
    } else {
      showOther(path);
    }
    if (push) history.pushState({ keep: 1 }, '', path);
  }

  document.querySelectorAll('.nav-tab').forEach(function (a) {
    a.addEventListener('click', function (e) {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      e.preventDefault();
      go(normPath(a.href), true);
    });
  });

  window.addEventListener('popstate', function () {
    go(normPath(location.href), false);
  });
})();
