/* ═══════════════════════════════════════════════════════════════════
   ElectroTrack 2.0 — interface controller
   Navigation, 3D charts, live preview, command palette, theme, motion.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var root = document.documentElement;
  var body = document.body;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var isViewer = body.classList.contains('is-viewer');
  var etData = {};
  try {
    var dataNode = document.getElementById('et-data');
    if (dataNode) etData = JSON.parse(dataNode.textContent || '{}');
  } catch (error) { etData = {}; }

  var charts = [];
  var sparklines = [];
  var $ = function (selector, scope) { return (scope || document).querySelector(selector); };
  var $$ = function (selector, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(selector)); };
  var formatNumber = function (value, decimals) {
    var number = Number(value) || 0;
    return number.toLocaleString('en-US', { minimumFractionDigits: decimals || 0, maximumFractionDigits: decimals || 0 });
  };
  var icons = function () { if (window.lucide) window.lucide.createIcons(); };

  /* ---------- Theme ---------- */
  function applyTheme(theme) {
    root.dataset.theme = theme;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#edf2fb' : '#050916');
    try { localStorage.setItem('et-theme', theme); } catch (error) { /* private mode */ }
    redrawCharts();
  }
  root.dataset.theme = (function () {
    try { return localStorage.getItem('et-theme') || 'dark'; } catch (error) { return 'dark'; }
  })();
  $$('#theme-toggle').forEach(function (button) {
    button.addEventListener('click', function () {
      applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark');
      var icon = button.querySelector('svg');
      if (icon) { icon.style.transform = 'rotate(180deg) scale(.85)'; setTimeout(function () { icon.style.transform = ''; }, 260); }
    });
  });

  /* ---------- Icons ---------- */
  icons();

  /* ---------- View routing ---------- */
  var views = $$('.page-view');
  var navLinks = $$('.nav-link');
  var sectionTitle = $('#current-section');
  var currentView = 'dashboard';
  var viewTitles = {};
  navLinks.forEach(function (link) { viewTitles[link.dataset.nav] = link.textContent.trim(); });
  viewTitles.history = 'Consumer history';

  function showView(id, updateHash) {
    if (!views.some(function (view) { return view.id === id; })) id = 'dashboard';
    views.forEach(function (view) { view.hidden = view.id !== id; });
    navLinks.forEach(function (link) {
      var active = link.dataset.nav === id;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    currentView = id;
    if (sectionTitle) sectionTitle.textContent = viewTitles[id] || 'Dashboard';
    if (updateHash !== false) history.replaceState(null, '', '#' + id);
    closeSidebar();
    if (updateHash !== false) window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    requestAnimationFrame(function () {
      setupReveals();
      resizeCharts();
      initSparklines();
    });
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[href^="#"]');
    if (!link) return;
    var id = link.getAttribute('href').slice(1);
    if (!views.some(function (view) { return view.id === id; })) return;
    event.preventDefault();
    showView(id);
  });
  window.addEventListener('hashchange', function () {
    var id = location.hash.slice(1) || 'dashboard';
    if (id !== currentView) showView(id, false);
  });

  var params = new URLSearchParams(location.search);
  var initial = location.hash.slice(1) || 'dashboard';
  if (params.has('add') || params.has('edit')) initial = 'consumers';
  if (params.has('reading')) initial = 'readings';
  if (params.has('history')) initial = 'history';
  if (params.get('view') === 'analytics') initial = 'analytics';
  showView(initial, false);

  /* ---------- Sidebar (mobile) ---------- */
  var sidebar = $('#sidebar');
  var sidebarBackdrop = $('#sidebar-backdrop');
  var menuToggle = $('#menu-toggle');
  function openSidebar() {
    if (!sidebar) return;
    sidebar.classList.add('open');
    if (sidebarBackdrop) sidebarBackdrop.hidden = false;
    if (menuToggle) menuToggle.setAttribute('aria-expanded', 'true');
    body.style.overflow = 'hidden';
  }
  function closeSidebar() {
    if (!sidebar || !sidebar.classList.contains('open')) return;
    sidebar.classList.remove('open');
    if (sidebarBackdrop) sidebarBackdrop.hidden = true;
    if (menuToggle) menuToggle.setAttribute('aria-expanded', 'false');
    body.style.overflow = '';
  }
  menuToggle && menuToggle.addEventListener('click', openSidebar);
  $('#sidebar-close') && $('#sidebar-close').addEventListener('click', closeSidebar);
  sidebarBackdrop && sidebarBackdrop.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeSidebar(); });

  /* ---------- Reveal on scroll ---------- */
  var revealObserver = null;
  function setupReveals() {
    var elements = $$('.reveal:not(.in)');
    if (!elements.length) return;
    if (reduceMotion || !('IntersectionObserver' in window)) {
      elements.forEach(function (el) { el.classList.add('in'); });
      return;
    }
    if (!revealObserver) {
      revealObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) { entry.target.classList.add('in'); revealObserver.unobserve(entry.target); }
        });
      }, { rootMargin: '0px 0px -6% 0px', threshold: 0.05 });
    }
    elements.forEach(function (el) { revealObserver.observe(el); });
  }
  setupReveals();

  /* ---------- Animated counters ---------- */
  function animateCounter(el) {
    if (el.dataset.counted === '1') return;
    el.dataset.counted = '1';
    var target = Number(el.dataset.counter || 0);
    var decimals = Number(el.dataset.decimals || 0);
    if (reduceMotion) { el.textContent = formatNumber(target, decimals); return; }
    var start = performance.now();
    var duration = 1100 + Math.random() * 250;
    function tick(now) {
      var progress = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatNumber(target * eased, decimals);
      if (progress < 1) requestAnimationFrame(tick);
      else el.textContent = formatNumber(target, decimals);
    }
    requestAnimationFrame(tick);
  }
  var counterObserver = 'IntersectionObserver' in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) { if (entry.isIntersecting) { animateCounter(entry.target); counterObserver.unobserve(entry.target); } });
  }, { threshold: 0.35 }) : null;
  $$('[data-counter]').forEach(function (el) {
    if (counterObserver) counterObserver.observe(el); else animateCounter(el);
  });

  /* ---------- 3D tilt ---------- */
  if (!reduceMotion && window.matchMedia('(hover: hover)').matches) {
    $$('[data-tilt]').forEach(function (card) {
      card.addEventListener('pointermove', function (event) {
        var rect = card.getBoundingClientRect();
        var px = (event.clientX - rect.left) / rect.width;
        var py = (event.clientY - rect.top) / rect.height;
        card.style.setProperty('--rx', ((0.5 - py) * 7).toFixed(2) + 'deg');
        card.style.setProperty('--ry', ((px - 0.5) * 9).toFixed(2) + 'deg');
        card.style.setProperty('--mx', (px * 100).toFixed(1) + '%');
        card.style.setProperty('--my', (py * 100).toFixed(1) + '%');
      });
      card.addEventListener('pointerleave', function () {
        card.style.setProperty('--rx', '0deg');
        card.style.setProperty('--ry', '0deg');
      });
    });
  }

  /* ---------- Chart helpers ---------- */
  function themeColors() {
    var light = root.dataset.theme === 'light';
    return {
      light: light,
      grid: light ? 'rgba(15,23,42,0.08)' : 'rgba(148,163,184,0.11)',
      gridStrong: light ? 'rgba(15,23,42,0.14)' : 'rgba(148,163,184,0.2)',
      text: light ? '#566783' : '#8899b9',
      faint: light ? '#7f8da6' : '#5f7093',
      cyan: light ? '#0891b2' : '#22d3ee',
      cyanSoft: light ? 'rgba(8,145,178,0.18)' : 'rgba(34,211,238,0.2)',
      indigo: light ? '#5b5ce2' : '#818cf8',
      violet: light ? '#9333ea' : '#c084fc',
      green: light ? '#15803d' : '#4ade80',
      surface: light ? 'rgba(255,255,255,0.9)' : 'rgba(17,26,50,0.94)'
    };
  }

  function fitCanvas(canvas) {
    var wrap = canvas.parentElement;
    var rect = wrap.getBoundingClientRect();
    var width = Math.max(220, Math.floor(rect.width));
    var height = Math.max(140, Math.floor(rect.height));
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = width + 'px';
      canvas.style.height = height + 'px';
    }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    return { ctx: ctx, width: width, height: height };
  }

  function roundRect(ctx, x, y, w, h, r) {
    if (h < 0) { y += h; h = -h; }
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function tooltipFor(tooltip) {
    if (!tooltip) return { show: function () {}, hide: function () {} };
    return {
      show: function (x, y, html) {
        tooltip.innerHTML = html;
        tooltip.classList.add('visible');
        var width = tooltip.offsetWidth || 120;
        var wrapWidth = tooltip.parentElement.clientWidth;
        var clamped = Math.max(width / 2 + 6, Math.min(wrapWidth - width / 2 - 6, x));
        tooltip.style.left = clamped + 'px';
        tooltip.style.top = Math.max(44, y) + 'px';
      },
      hide: function () { tooltip.classList.remove('visible'); }
    };
  }

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

  /* ---------- Isometric 3D bar chart ---------- */
  function renderIsoBars(canvas, options) {
    if (!canvas) return null;
    var tooltip = tooltipFor(options.tooltip);
    var labels = options.labels || [];
    var values = options.values || [];
    var hover = -1;
    var progress = reduceMotion ? 1 : 0;
    var startTime = 0;
    var finished = reduceMotion;
    var lastGeometry = null;

    function geometry(width, height) {
      var pad = { top: 34, right: 16, bottom: 34, left: 44 };
      var max = Math.max.apply(null, values.concat([1]));
      var slot = (width - pad.left - pad.right) / Math.max(labels.length, 1);
      var barWidth = Math.min(48, slot * 0.5);
      var depth = Math.min(13, barWidth * 0.32);
      var baseline = height - pad.bottom;
      var usable = Math.max(50, baseline - pad.top - 26);
      var smallest = Math.max(3, Math.min.apply(null, values) * 0.05);
      return { pad: pad, max: max, slot: slot, barWidth: barWidth, depth: depth, baseline: baseline, usable: usable, smallest: smallest };
    }

    function draw(now) {
      var fitted = fitCanvas(canvas);
      var ctx = fitted.ctx, width = fitted.width, height = fitted.height;
      var colors = themeColors();
      var g = geometry(width, height);
      lastGeometry = g;
      ctx.clearRect(0, 0, width, height);

      if (!values.some(function (value) { return value > 0; })) {
        ctx.beginPath();
        ctx.strokeStyle = colors.gridStrong;
        ctx.moveTo(g.pad.left, g.baseline + 0.5);
        ctx.lineTo(width - g.pad.right, g.baseline + 0.5);
        ctx.stroke();
        labels.forEach(function (label, index) {
          ctx.font = '500 9.5px "Inter", sans-serif';
          ctx.fillStyle = colors.faint;
          ctx.textAlign = 'center';
          ctx.fillText(label, g.pad.left + g.slot * index + g.slot / 2, g.baseline + 21);
        });
        ctx.font = '600 11px "Inter", sans-serif';
        ctx.fillStyle = colors.faint;
        ctx.textAlign = 'center';
        ctx.fillText(options.emptyMessage || 'No usage recorded for this period.', width / 2, g.baseline - 24);
        finished = true;
        return;
      }

      /* perspective floor */
      var floorGradient = ctx.createLinearGradient(0, g.baseline - 8, width, g.baseline + 30);
      floorGradient.addColorStop(0, 'transparent');
      floorGradient.addColorStop(0.5, colors.cyanSoft);
      floorGradient.addColorStop(1, 'transparent');
      ctx.fillStyle = floorGradient;
      ctx.fillRect(0, g.baseline - 1, width, 2);

      for (var line = 1; line <= 4; line += 1) {
        var y = g.baseline - (g.usable / 4) * line;
        ctx.beginPath();
        ctx.strokeStyle = line === 4 ? colors.gridStrong : colors.grid;
        ctx.lineWidth = 1;
        ctx.moveTo(g.pad.left, y + 0.5);
        ctx.lineTo(width - g.pad.right, y + 0.5);
        ctx.stroke();
        ctx.font = '9px "JetBrains Mono", monospace';
        ctx.fillStyle = colors.faint;
        ctx.textAlign = 'right';
        ctx.fillText(formatNumber(g.max / 4 * line), g.pad.left - 2, y + 3);
      }

      labels.forEach(function (label, index) {
        var target = g.smallest + ((values[index] || 0) / g.max) * (g.usable - g.smallest);
        var h = Math.max(2, target * (finished ? 1 : easeOutCubic(progress)));
        var x = g.pad.left + g.slot * index + (g.slot - g.barWidth) / 2;
        var y = g.baseline - h;
        var active = index === hover;
        var lift = active ? 4 : 0;

        ctx.save();
        ctx.globalAlpha = 0.9;
        /* reflected shadow */
        var refl = ctx.createLinearGradient(0, g.baseline, 0, g.baseline + 16);
        refl.addColorStop(0, active ? 'rgba(34,211,238,0.26)' : 'rgba(34,211,238,0.13)');
        refl.addColorStop(1, 'transparent');
        ctx.fillStyle = refl;
        ctx.fillRect(x + g.depth, g.baseline, g.barWidth, 16);
        ctx.restore();

        /* side face */
        ctx.beginPath();
        ctx.moveTo(x + g.barWidth, y - lift);
        ctx.lineTo(x + g.barWidth + g.depth, y - g.depth - lift);
        ctx.lineTo(x + g.barWidth + g.depth, g.baseline - g.depth);
        ctx.lineTo(x + g.barWidth, g.baseline);
        ctx.closePath();
        var side = ctx.createLinearGradient(x + g.barWidth, 0, x + g.barWidth + g.depth, 0);
        side.addColorStop(0, active ? 'rgba(34,211,238,0.6)' : 'rgba(49,46,129,0.85)');
        side.addColorStop(1, active ? 'rgba(129,140,248,0.55)' : 'rgba(30,27,75,0.85)');
        ctx.fillStyle = side;
        ctx.fill();

        /* top face */
        ctx.beginPath();
        ctx.moveTo(x, y - lift);
        ctx.lineTo(x + g.depth, y - g.depth - lift);
        ctx.lineTo(x + g.barWidth + g.depth, y - g.depth - lift);
        ctx.lineTo(x + g.barWidth, y - lift);
        ctx.closePath();
        ctx.fillStyle = active ? 'rgba(207,250,254,0.95)' : 'rgba(165,243,252,0.72)';
        ctx.fill();

        /* front face */
        var front = ctx.createLinearGradient(0, y, 0, g.baseline);
        front.addColorStop(0, active ? '#67e8f9' : '#22d3ee');
        front.addColorStop(0.55, active ? '#818cf8' : '#4f46e5');
        front.addColorStop(1, active ? 'rgba(129,140,248,0.55)' : 'rgba(49,46,129,0.55)');
        ctx.fillStyle = front;
        ctx.shadowColor = active ? 'rgba(34,211,238,0.7)' : 'rgba(34,211,238,0.24)';
        ctx.shadowBlur = active ? 24 : 12;
        roundRect(ctx, x, y - lift, g.barWidth, h + lift, Math.min(5, g.barWidth / 4));
        ctx.fill();
        ctx.shadowBlur = 0;

        /* value */
        if (values[index] > 0) {
          ctx.font = '600 10px "JetBrains Mono", monospace';
          ctx.fillStyle = active ? colors.cyan : colors.text;
          ctx.textAlign = 'center';
          ctx.fillText(formatNumber(values[index]), x + g.barWidth / 2, y - g.depth - 9 - lift);
        }

        /* label */
        var labelY = g.baseline + 21;
        if (labels.length <= 9 || index % 2 === 0) {
          ctx.font = (active ? '600 ' : '500 ') + '9.5px "Inter", sans-serif';
          ctx.fillStyle = active ? colors.text : colors.faint;
          ctx.textAlign = 'center';
          ctx.fillText(label, x + g.barWidth / 2, labelY);
        }
      });

      if (!finished) {
        progress = Math.min(1, (now - startTime) / (reduceMotion ? 1 : 950));
        if (progress >= 1) finished = true;
        requestAnimationFrame(draw);
      }
    }

    function frame(now) {
      if (!startTime) startTime = now;
      draw(now);
    }

    canvas.addEventListener('pointermove', function (event) {
      if (!lastGeometry) return;
      var rect = canvas.getBoundingClientRect();
      var x = event.clientX - rect.left;
      var index = Math.floor((x - lastGeometry.pad.left) / lastGeometry.slot);
      if (index < 0 || index >= labels.length) { hover = -1; tooltip.hide(); return; }
      hover = index;
      var value = values[index] || 0;
      var barX = lastGeometry.pad.left + lastGeometry.slot * index + lastGeometry.slot / 2;
      var ratio = value / lastGeometry.max;
      var barY = lastGeometry.baseline - ratio * lastGeometry.usable;
      tooltip.show(barX, barY - 20, '<strong>' + formatNumber(value) + ' kWh</strong><span>' + labels[index] + '</span>');
      if (finished) draw(performance.now());
    });
    canvas.addEventListener('pointerleave', function () {
      hover = -1; tooltip.hide();
      if (finished) draw(performance.now());
    });

    requestAnimationFrame(frame);
    return { resize: function () { if (finished) draw(performance.now()); } };
  }

  /* ---------- Smooth line / area chart ---------- */
  function renderLineChart(canvas, options) {
    if (!canvas) return null;
    var tooltip = tooltipFor(options.tooltip);
    var labels = options.labels || [];
    var values = options.values || [];
    var series = options.series || null;
    var hover = -1;
    var progress = reduceMotion ? 1 : 0;
    var startTime = 0;
    var finished = reduceMotion;
    var geometry = null;
    var flatMax = 1;

    function allValues() {
      if (series) return series.reduce(function (acc, item) { return acc.concat(item.values || []); }, []);
      return values;
    }

    function buildGeometry(width, height) {
      var pad = { top: 26, right: 18, bottom: 34, left: 46 };
      var max = Math.max.apply(null, allValues().concat([1]));
      flatMax = max;
      var stepX = labels.length > 1 ? (width - pad.left - pad.right) / (labels.length - 1) : 0;
      return { pad: pad, max: max, stepX: stepX, plotW: width - pad.left - pad.right, plotH: height - pad.top - pad.bottom };
    }

    function xAt(index, g) { return g.pad.left + (labels.length > 1 ? g.stepX * index : g.plotW / 2); }
    function yAt(value, g) { return g.pad.top + g.plotH - (value / g.max) * g.plotH; }

    function smoothPath(ctx, points) {
      if (!points.length) return;
      ctx.moveTo(points[0].x, points[0].y);
      for (var i = 0; i < points.length - 1; i += 1) {
        var current = points[i], next = points[i + 1];
        var midX = (current.x + next.x) / 2;
        ctx.bezierCurveTo(midX, current.y, midX, next.y, next.x, next.y);
      }
    }

    function draw(now) {
      var fitted = fitCanvas(canvas);
      var ctx = fitted.ctx, width = fitted.width, height = fitted.height;
      var colors = themeColors();
      var g = buildGeometry(width, height);
      geometry = g;
      ctx.clearRect(0, 0, width, height);

      if (!allValues().some(function (value) { return value > 0; })) {
        labels.forEach(function (label, index) {
          ctx.font = '9px "Inter", sans-serif';
          ctx.fillStyle = colors.faint;
          ctx.textAlign = 'center';
          ctx.fillText(String(label).slice(0, 9), xAt(index, g), height - 14);
        });
        ctx.font = '600 11px "Inter", sans-serif';
        ctx.fillStyle = colors.faint;
        ctx.textAlign = 'center';
        ctx.fillText(options.emptyMessage || 'No usage recorded for this period.', width / 2, height / 2);
        finished = true;
        return;
      }

      /* grid */
      ctx.font = '9px "JetBrains Mono", monospace';
      ctx.textAlign = 'right';
      for (var line = 0; line <= 4; line += 1) {
        var value = (g.max / 4) * line;
        var y = yAt(value, g);
        ctx.beginPath();
        ctx.strokeStyle = line === 0 ? colors.gridStrong : colors.grid;
        ctx.lineWidth = 1;
        ctx.moveTo(g.pad.left, y + 0.5);
        ctx.lineTo(width - g.pad.right, y + 0.5);
        ctx.stroke();
        ctx.fillStyle = colors.faint;
        ctx.fillText(formatNumber(g.max / 4 * line), g.pad.left - 7, y + 3);
      }

      /* x labels */
      ctx.textAlign = 'center';
      var labelStride = labels.length > 10 ? Math.ceil(labels.length / 7) : 1;
      labels.forEach(function (label, index) {
        if (index % labelStride !== 0 && index !== labels.length - 1) return;
        ctx.font = '9px "Inter", sans-serif';
        ctx.fillStyle = hover === index ? colors.text : colors.faint;
        ctx.fillText(String(label).slice(0, 9), xAt(index, g), height - 14);
      });

      var groups = series || [{ values: values, color: colors.cyan, fill: true }];
      var drawnGroups = groups.filter(function (item) { return item.values && item.values.some(function (v) { return v > 0; }); });
      if (!drawnGroups.length) drawnGroups = [{ values: values, color: colors.cyan, fill: true, empty: true }];

      drawnGroups.forEach(function (group, groupIndex) {
        var points = labels.map(function (label, index) {
          var val = (group.values[index] || 0) * (finished ? 1 : easeOutCubic(progress));
          return { x: xAt(index, g), y: yAt(val, g), value: group.values[index] || 0 };
        });
        if (!points.length) return;
        var color = group.color || (groupIndex === 0 ? colors.cyan : colors.violet);

        if (group.fill !== false) {
          var area = ctx.createLinearGradient(0, g.pad.top, 0, g.pad.top + g.plotH);
          area.addColorStop(0, group.color ? group.color.replace('rgb', 'rgba').replace(')', ',0.22)') : (colors.light ? 'rgba(8,145,178,0.2)' : 'rgba(34,211,238,0.22)'));
          area.addColorStop(1, 'rgba(34,211,238,0)');
          ctx.save();
          ctx.beginPath();
          smoothPath(ctx, points);
          ctx.lineTo(points[points.length - 1].x, g.pad.top + g.plotH);
          ctx.lineTo(points[0].x, g.pad.top + g.plotH);
          ctx.closePath();
          ctx.fillStyle = area;
          ctx.fill();
          ctx.restore();
        }

        ctx.save();
        ctx.beginPath();
        smoothPath(ctx, points);
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.2;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.shadowColor = color;
        ctx.shadowBlur = 12;
        ctx.stroke();
        ctx.restore();

        points.forEach(function (point, index) {
          if (index === hover) {
            ctx.beginPath();
            ctx.fillStyle = colors.surface;
            ctx.strokeStyle = color;
            ctx.lineWidth = 2;
            ctx.arc(point.x, point.y, 4.5, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();
          } else {
            ctx.beginPath();
            ctx.fillStyle = color;
            ctx.globalAlpha = 0.7;
            ctx.arc(point.x, point.y, 2.2, 0, Math.PI * 2);
            ctx.fill();
            ctx.globalAlpha = 1;
          }
        });
      });

      if (hover >= 0 && labels.length) {
        var hx = xAt(hover, g);
        ctx.beginPath();
        ctx.strokeStyle = colors.gridStrong;
        ctx.setLineDash([3, 4]);
        ctx.moveTo(hx, g.pad.top);
        ctx.lineTo(hx, g.pad.top + g.plotH);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      if (!finished) {
        progress = Math.min(1, (now - startTime) / (reduceMotion ? 1 : 1050));
        if (progress >= 1) finished = true;
        requestAnimationFrame(draw);
      }
    }

    function frame(now) { if (!startTime) startTime = now; draw(now); }

    canvas.addEventListener('pointermove', function (event) {
      if (!geometry) return;
      var rect = canvas.getBoundingClientRect();
      var x = event.clientX - rect.left;
      var index = labels.length > 1 ? Math.round((x - geometry.pad.left) / geometry.stepX) : 0;
      index = Math.max(0, Math.min(labels.length - 1, index));
      hover = index;
      var lines = [];
      if (series) {
        series.forEach(function (item) {
          lines.push('<span>' + item.name + ': <b style="color:' + item.color + '">' + formatNumber(item.values[index]) + ' kWh</b></span>');
        });
      } else {
        lines.push('<strong>' + formatNumber(values[index]) + ' kWh</strong>');
      }
      tooltip.show(xAt(index, geometry), yAt((series ? series[0].values[index] : values[index]) || 0, geometry) - 14,
        '<span>' + labels[index] + '</span>' + lines.join(''));
      if (finished) draw(performance.now());
    });
    canvas.addEventListener('pointerleave', function () {
      hover = -1; tooltip.hide();
      if (finished) draw(performance.now());
    });

    requestAnimationFrame(frame);
    return { resize: function () { if (finished) draw(performance.now()); } };
  }

  /* ---------- Horizontal 3D comparison bars ---------- */
  function renderComparison(canvas, options) {
    if (!canvas) return null;
    var tooltip = tooltipFor(options.tooltip);
    var labels = options.labels || [];
    var values = options.values || [];
    var hover = -1;
    var progress = reduceMotion ? 1 : 0;
    var startTime = 0;
    var finished = reduceMotion;
    var geometry = null;

    function draw(now) {
      var fitted = fitCanvas(canvas);
      var ctx = fitted.ctx, width = fitted.width, height = fitted.height;
      var colors = themeColors();
      var top = 8, bottom = 8;
      var labelWidth = Math.min(148, width * 0.36);
      var barArea = width - labelWidth - 58;
      var rowHeight = (height - top - bottom) / Math.max(labels.length, 1);
      var barHeight = Math.min(22, rowHeight * 0.52);
      var max = Math.max.apply(null, values.concat([1]));
      geometry = { top: top, rowHeight: rowHeight, labelWidth: labelWidth, barArea: barArea, barHeight: barHeight, max: max };
      ctx.clearRect(0, 0, width, height);

      if (!labels.length) {
        ctx.font = '11px "Inter", sans-serif';
        ctx.fillStyle = colors.faint;
        ctx.textAlign = 'center';
        ctx.fillText('No comparison data for this period.', width / 2, height / 2);
        return;
      }

      labels.forEach(function (label, index) {
        var y = top + rowHeight * index + rowHeight / 2;
        var target = (values[index] || 0) / max * barArea;
        var w = Math.max(3, target * (finished ? 1 : easeOutCubic(progress)));
        var active = index === hover;

        ctx.font = '10px "Inter", sans-serif';
        ctx.textAlign = 'left';
        ctx.fillStyle = active ? colors.text : colors.faint;
        var display = String(label);
        if (ctx.measureText(display).width > labelWidth - 12) {
          while (display.length > 3 && ctx.measureText(display + '…').width > labelWidth - 12) display = display.slice(0, -1);
          display += '…';
        }
        ctx.fillText(display, 2, y + 3);

        /* track */
        ctx.fillStyle = colors.grid;
        roundRect(ctx, labelWidth, y - barHeight / 2, barArea, barHeight, barHeight / 2);
        ctx.fill();

        var gradient = ctx.createLinearGradient(labelWidth, 0, labelWidth + w, 0);
        gradient.addColorStop(0, colors.cyan);
        gradient.addColorStop(1, colors.indigo);
        ctx.fillStyle = gradient;
        ctx.shadowColor = active ? colors.cyan : 'transparent';
        ctx.shadowBlur = active ? 14 : 0;
        roundRect(ctx, labelWidth, y - barHeight / 2, w, barHeight, barHeight / 2);
        ctx.fill();
        ctx.shadowBlur = 0;

        /* 3D underside */
        ctx.fillStyle = 'rgba(15,23,42,0.22)';
        roundRect(ctx, labelWidth + 2, y + barHeight / 2 - 1, w, 3, 2);
        ctx.fill();

        ctx.font = '600 9.5px "JetBrains Mono", monospace';
        ctx.fillStyle = active ? colors.text : colors.faint;
        ctx.textAlign = 'right';
        ctx.fillText(formatNumber(values[index]), width - 2, y + 3);
      });

      if (!finished) {
        progress = Math.min(1, (now - startTime) / (reduceMotion ? 1 : 1000));
        if (progress >= 1) finished = true;
        requestAnimationFrame(draw);
      }
    }
    function frame(now) { if (!startTime) startTime = now; draw(now); }

    canvas.addEventListener('pointermove', function (event) {
      if (!geometry) return;
      var rect = canvas.getBoundingClientRect();
      var y = event.clientY - rect.top;
      var index = Math.floor((y - geometry.top) / geometry.rowHeight);
      if (index < 0 || index >= labels.length) { hover = -1; tooltip.hide(); return; }
      hover = index;
      tooltip.show(geometry.labelWidth + 80, geometry.top + geometry.rowHeight * index + 6,
        '<strong>' + formatNumber(values[index]) + ' kWh</strong><span>' + labels[index] + '</span>');
      if (finished) draw(performance.now());
    });
    canvas.addEventListener('pointerleave', function () {
      hover = -1; tooltip.hide();
      if (finished) draw(performance.now());
    });

    requestAnimationFrame(frame);
    return { resize: function () { if (finished) draw(performance.now()); } };
  }

  function resizeCharts() {
    charts.forEach(function (chart) { if (chart && chart.resize) chart.resize(); });
  }
  function redrawCharts() {
    resizeCharts();
    drawAllSparklines();
  }
  window.addEventListener('resize', function () {
    clearTimeout(window.__etResize);
    window.__etResize = setTimeout(resizeCharts, 120);
  });

  /* ---------- Build dashboard chart ---------- */
  var chart = etData.chart || { labels: [], values: [] };
  var usageChart = renderIsoBars($('#usage-chart'), {
    labels: chart.labels, values: chart.values, tooltip: $('#usage-tooltip')
  });
  if (usageChart) charts.push(usageChart);

  var totalValues = (chart.values || []).reduce(function (a, b) { return a + b; }, 0);
  var averageValue = chart.values && chart.values.length ? Math.round(totalValues / chart.values.length) : 0;
  var peakValue = chart.values && chart.values.length ? Math.max.apply(null, chart.values) : 0;
  if ($('#chart-total')) $('#chart-total').textContent = formatNumber(totalValues) + ' kWh';
  if ($('#chart-average')) $('#chart-average').textContent = formatNumber(averageValue) + ' kWh';
  if ($('#chart-peak')) $('#chart-peak').textContent = formatNumber(peakValue) + ' kWh';

  /* ---------- Analytics charts ---------- */
  var analyticsVisible = !!$('#analytics-trend');
  if (analyticsVisible) {
    var trend = renderLineChart($('#analytics-trend'), {
      labels: chart.labels, values: chart.values, tooltip: $('#trend-tooltip')
    });
    if (trend) charts.push(trend);

    var comparison = etData.comparison || { labels: [], values: [] };
    var compare = renderComparison($('#analytics-compare'), {
      labels: comparison.labels, values: comparison.values, tooltip: $('#compare-tooltip')
    });
    if (compare) charts.push(compare);

    $('#analytics-total') && ($('#analytics-total').textContent = formatNumber(totalValues) + ' kWh');
    $('#analytics-average') && ($('#analytics-average').textContent = formatNumber(averageValue) + ' kWh');
    $('#analytics-peak') && ($('#analytics-peak').textContent = formatNumber(peakValue) + ' kWh');
    $('#analytics-entities') && ($('#analytics-entities').textContent = (comparison.labels || []).length);
    fillTable('#trend-table-body', (chart.labels || []).map(function (label, index) { return [label, formatNumber(chart.values[index]) + ' kWh']; }));
    fillTable('#compare-table-body', (comparison.labels || []).map(function (label, index) { return [label, formatNumber(comparison.values[index]) + ' kWh']; }));
  }

  function fillTable(selector, rows) {
    var tbody = $(selector);
    if (!tbody) return;
    tbody.innerHTML = rows.map(function (row) {
      return '<tr><td>' + escapeHtml(row[0]) + '</td><td>' + escapeHtml(row[1]) + '</td></tr>';
    }).join('') || '<tr><td colspan="2" class="empty-state">No data.</td></tr>';
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (char) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char];
    });
  }

  /* ---------- History chart ---------- */
  var historyCanvas = $('#history-chart');
  if (historyCanvas && etData.history) {
    var colors = themeColors();
    var historyChart = renderLineChart(historyCanvas, {
      labels: etData.history.labels,
      series: [
        { name: 'Recorded', values: etData.history.recorded, color: colors.cyan, fill: true },
        { name: 'Sample', values: etData.history.sample, color: colors.violet, fill: false }
      ],
      tooltip: $('#history-tooltip')
    });
    if (historyChart) charts.push(historyChart);
  }

  /* ---------- Sparklines ---------- */
  function drawSparkline(canvas) {
    var key = canvas.dataset.spark;
    var values = [];
    if (key === 'consumers') values = (etData.consumers || []).map(function (item) { return item.usage; });
    else values = (etData.daily || []).map(function (item) { return item.total; });
    if (!values.length) values = [0, 0, 0];
    var rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    var colors = themeColors();
    var max = Math.max.apply(null, values.concat([1]));
    var min = Math.min.apply(null, values.concat([0]));
    var range = max - min || 1;
    var pts = values.map(function (value, index) {
      return {
        x: (rect.width / Math.max(1, values.length - 1)) * index,
        y: rect.height - 4 - ((value - min) / range) * (rect.height - 9)
      };
    });
    var gradient = ctx.createLinearGradient(0, 0, 0, rect.height);
    gradient.addColorStop(0, 'rgba(34,211,238,0.32)');
    gradient.addColorStop(1, 'rgba(34,211,238,0)');
    ctx.beginPath();
    ctx.moveTo(pts[0].x, rect.height);
    pts.forEach(function (point, index) {
      if (index === 0) ctx.lineTo(point.x, point.y);
      else {
        var prev = pts[index - 1];
        var mid = (prev.x + point.x) / 2;
        ctx.bezierCurveTo(mid, prev.y, mid, point.y, point.x, point.y);
      }
    });
    ctx.lineTo(pts[pts.length - 1].x, rect.height);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    ctx.beginPath();
    pts.forEach(function (point, index) {
      if (index === 0) ctx.moveTo(point.x, point.y);
      else {
        var prev = pts[index - 1];
        var mid = (prev.x + point.x) / 2;
        ctx.bezierCurveTo(mid, prev.y, mid, point.y, point.x, point.y);
      }
    });
    ctx.strokeStyle = colors.cyan;
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }
  function initSparklines() {
    $$('.sparkline').forEach(function (canvas) { if (!canvas.dataset.drawn) { canvas.dataset.drawn = '1'; sparklines.push(canvas); } });
    drawAllSparklines();
  }
  function drawAllSparklines() { sparklines.forEach(drawSparkline); }
  initSparklines();

  /* ---------- Hero particle scene ---------- */
  (function initHeroScene() {
    var canvas = $('#hero-canvas');
    if (!canvas) return;
    var wrap = canvas.parentElement;
    var ctx = null, dpr = 1, width = 0, height = 0;
    var points = [];
    var angleY = 0.4, angleX = 0.15;
    var targetTiltX = 0.15, targetTiltY = 0.4;
    var pointer = { x: 0, y: 0 };
    var count = window.innerWidth < 700 ? 34 : 64;

    for (var i = 0; i < count; i += 1) {
      var theta = Math.random() * Math.PI * 2;
      var phi = Math.acos(2 * Math.random() - 1);
      var radius = 140 + Math.random() * 130;
      points.push({
        x: radius * Math.sin(phi) * Math.cos(theta),
        y: radius * Math.cos(phi) * 0.72,
        z: radius * Math.sin(phi) * Math.sin(theta),
        size: 0.7 + Math.random() * 1.9,
        hue: Math.random() > 0.55 ? 'violet' : 'cyan',
        phase: Math.random() * Math.PI * 2
      });
    }

    function resize() {
      var rect = wrap.getBoundingClientRect();
      width = rect.width; height = rect.height;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx = canvas.getContext('2d');
    }
    resize();
    if ('ResizeObserver' in window) new ResizeObserver(resize).observe(wrap);
    else window.addEventListener('resize', resize);

    wrap.addEventListener('pointermove', function (event) {
      var rect = wrap.getBoundingClientRect();
      pointer.x = (event.clientX - rect.left) / rect.width - 0.5;
      pointer.y = (event.clientY - rect.top) / rect.height - 0.5;
      targetTiltY = 0.4 + pointer.x * 0.5;
      targetTiltX = 0.15 - pointer.y * 0.35;
    });
    wrap.addEventListener('pointerleave', function () { targetTiltX = 0.15; targetTiltY = 0.4; });

    var last = performance.now();
    function frame(now) {
      if (!ctx) { requestAnimationFrame(frame); return; }
      var elapsed = now - last; last = now;
      if (document.hidden) { requestAnimationFrame(frame); return; }
      angleY += (targetTiltY - angleY) * 0.035 + 0.0016;
      angleX += (targetTiltX - angleX) * 0.035;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);

      var cosY = Math.cos(angleY), sinY = Math.sin(angleY);
      var cosX = Math.cos(angleX), sinX = Math.sin(angleX);
      var cx = width / 2, cy = height * 0.48;
      var projected = points.map(function (point, index) {
        var x1 = point.x * cosY - point.z * sinY;
        var z1 = point.x * sinY + point.z * cosY;
        var y1 = point.y * cosX - z1 * sinX;
        var z2 = point.y * sinX + z1 * cosX;
        var perspective = 620 / (620 + z2);
        return {
          x: cx + x1 * perspective,
          y: cy + y1 * perspective,
          z: z2,
          alpha: Math.max(0.1, Math.min(0.95, perspective * 0.9)),
          size: point.size * perspective,
          hue: point.hue,
          index: index
        };
      }).sort(function (a, b) { return b.z - a.z; });

      for (var a = 0; a < projected.length; a += 1) {
        for (var b = a + 1; b < projected.length; b += 1) {
          var dx = projected[a].x - projected[b].x;
          var dy = projected[a].y - projected[b].y;
          var distance = Math.sqrt(dx * dx + dy * dy);
          if (distance < 56 && projected[a].z < 280 && projected[b].z < 280) {
            ctx.beginPath();
            ctx.strokeStyle = 'rgba(129,140,248,' + (0.14 * (1 - distance / 56) * projected[a].alpha) + ')';
            ctx.lineWidth = 0.6;
            ctx.moveTo(projected[a].x, projected[a].y);
            ctx.lineTo(projected[b].x, projected[b].y);
            ctx.stroke();
          }
        }
      }
      projected.forEach(function (point) {
        var color = point.hue === 'violet' ? '192,132,252' : '34,211,238';
        ctx.beginPath();
        ctx.fillStyle = 'rgba(' + color + ',' + (point.alpha * 0.85) + ')';
        ctx.shadowColor = 'rgba(' + color + ',0.9)';
        ctx.shadowBlur = 7 * point.size;
        ctx.arc(point.x, point.y, Math.max(0.6, point.size), 0, Math.PI * 2);
        ctx.fill();
        ctx.shadowBlur = 0;
      });
      requestAnimationFrame(frame);
    }
    if (!reduceMotion) requestAnimationFrame(frame);
  })();

  /* ---------- Reading preview ---------- */
  var readingDialog = $('#reading-dialog');
  var readingMeter = $('#reading-consumer');
  var readingDate = $('#reading-date');
  var readingCumulative = $('#reading-cumulative');
  var preview = $('#reading-preview');
  var previewState = $('#preview-state');
  var previewUsage = $('#preview-usage');
  var previewNote = $('#preview-note');
  var previewPrevious = $('#preview-previous');
  var previewDate = $('#preview-date');
  var previewMeter = $('#preview-meter');
  var meterContext = null;
  var previewController = null;

  function setPreviewState(state, label) {
    if (!preview) return;
    preview.hidden = false;
    preview.dataset.state = state;
    if (previewState) previewState.textContent = label;
  }

  function updateReadingPreview() {
    if (!preview || readingDialog.hidden || !readingDialog.open) return;
    if (!readingMeter || !readingMeter.value) {
      preview.hidden = true;
      return;
    }
    if (!meterContext || String(meterContext.meter_id) !== readingMeter.value) {
      preview.hidden = true;
      return;
    }
    var cumulative = Number(readingCumulative.value);
    var date = readingDate.value;
    setPreviewState('ok', 'Ready');
    previewUsage.textContent = '—';
    previewMeter.textContent = meterContext.meter_code + ' · ' + meterContext.consumer_name;
    previewPrevious.textContent = meterContext.previous_date
      ? 'Previous reading: ' + formatNumber(meterContext.previous_cumulative) + ' kWh on ' + meterContext.previous_date
      : 'No previous baseline';
    previewDate.textContent = meterContext.latest_date ? 'Latest entry: ' + meterContext.latest_date : 'No readings yet';

    if (!date || !readingCumulative.value) {
      previewNote.textContent = 'Choose a date and cumulative value to preview the calculation.';
      return;
    }
    if (meterContext.pending) {
      setPreviewState('review', 'Blocked');
      previewNote.textContent = 'This meter has a reading pending review. Approve or reject it before recording a new value.';
      return;
    }
    if (meterContext.latest_date && date <= meterContext.latest_date) {
      setPreviewState('review', 'Date');
      previewNote.textContent = 'The reading date must be after the latest saved reading (' + meterContext.latest_date + ').';
      return;
    }
    if (!meterContext.previous_date || meterContext.previous_cumulative === null) {
      setPreviewState('ok', 'Baseline');
      previewNote.textContent = 'This becomes the baseline reading. Usage starts with the next cumulative value.';
      return;
    }
    var delta = cumulative - Number(meterContext.previous_cumulative);
    if (cumulative < 0 || Number.isNaN(delta)) {
      previewNote.textContent = 'Enter a cumulative value of 0 or more.';
      return;
    }
    if (delta < 0) {
      setPreviewState('reset', 'Reset');
      previewUsage.textContent = '—';
      previewNote.textContent = 'The cumulative value is lower than the previous reading. It will be flagged as a possible reset or rollover for review.';
      return;
    }
    previewUsage.textContent = formatNumber(delta);
    if (delta > Number(meterContext.review_threshold || 0)) {
      setPreviewState('review', 'Review');
      previewNote.textContent = 'This ' + formatNumber(delta) + ' kWh increase exceeds the ' + formatNumber(meterContext.review_threshold) + ' kWh review threshold. It will wait for approval before affecting totals.';
    } else if (delta === 0) {
      setPreviewState('ok', 'No change');
      previewNote.textContent = 'The meter has not advanced since the last reading. Usage for this interval is 0 kWh.';
    } else {
      setPreviewState('ok', 'Ready');
      previewNote.textContent = 'Estimated usage of ' + formatNumber(delta) + ' kWh. This will be saved as a valid reading.';
    }
  }

  function loadMeterContext(meterId) {
    if (!meterId) { preview && (preview.hidden = true); meterContext = null; return; }
    if (previewController) previewController.abort();
    previewController = new AbortController();
    fetch('/api/meters/' + encodeURIComponent(meterId) + '/latest', { signal: previewController.signal, headers: { Accept: 'application/json' } })
      .then(function (response) { if (!response.ok) throw new Error('Meter unavailable'); return response.json(); })
      .then(function (data) {
        meterContext = data;
        if (readingDate && data.latest_date && (!readingDate.value || readingDate.value <= data.latest_date)) {
          var next = new Date(data.latest_date + 'T12:00:00');
          next.setDate(next.getDate() + 1);
          var today = new Date();
          if (next > today) next = today;
          readingDate.value = next.toISOString().slice(0, 10);
        }
        updateReadingPreview();
      })
      .catch(function (error) {
        if (error.name === 'AbortError') return;
        meterContext = null;
        if (preview) { preview.hidden = false; preview.dataset.state = 'review'; previewState.textContent = 'Unavailable'; previewNote.textContent = 'Could not load the previous reading for this meter. You can still save if the value is valid.'; }
      });
  }

  readingMeter && readingMeter.addEventListener('change', function () { meterContext = null; loadMeterContext(readingMeter.value); });
  [readingDate, readingCumulative].forEach(function (input) { input && input.addEventListener('input', updateReadingPreview); });
  readingDialog && readingDialog.addEventListener('close', function () {
    if (preview) preview.hidden = true;
    meterContext = null;
    if (previewController) previewController.abort();
  });

  /* ---------- Dialogs ---------- */
  var addDialog = $('#consumer-dialog');
  var editDialog = $('#edit-dialog');
  var editForm = $('#edit-form');
  var meterDialog = $('#meter-dialog');
  var meterEditDialog = $('#meter-edit-dialog');
  var meterEditForm = $('#meter-edit-form');

  function openModal(dialog) { if (dialog && !dialog.open) dialog.showModal(); }
  function closeModal(dialog) { if (dialog && dialog.open) dialog.close(); }

  function openReading(meterId) {
    if (!readingDialog) return;
    if (readingCumulative) readingCumulative.value = '';
    if (preview) preview.hidden = true;
    meterContext = null;
    if (meterId && readingMeter) {
      readingMeter.value = String(meterId);
      loadMeterContext(meterId);
    }
    openModal(readingDialog);
    setTimeout(function () { readingMeter && readingMeter.focus(); }, 120);
  }

  $$('[data-open-consumer], #add-consumer').forEach(function (button) {
    button.addEventListener('click', function () { openModal(addDialog); });
  });
  $$('button[data-open-reading], #add-reading').forEach(function (button) {
    button.addEventListener('click', function () { openReading(readingMeter && readingMeter.value); });
  });
  $('#add-meter') && $('#add-meter').addEventListener('click', function () { openModal(meterDialog); });

  [['#close-dialog', addDialog], ['#cancel-dialog', addDialog], ['#close-edit-dialog', editDialog], ['#cancel-edit-dialog', editDialog], ['#close-reading-dialog', readingDialog], ['#cancel-reading-dialog', readingDialog], ['#close-meter-dialog', meterDialog], ['#cancel-meter-dialog', meterDialog], ['#close-meter-edit', meterEditDialog], ['#cancel-meter-edit', meterEditDialog]].forEach(function (pair) {
    var button = $(pair[0]);
    if (button) button.addEventListener('click', function () { closeModal(pair[1]); });
  });
  [addDialog, editDialog, readingDialog, meterDialog, meterEditDialog].forEach(function (dialog) {
    if (!dialog) return;
    dialog.addEventListener('click', function (event) { if (event.target === dialog) dialog.close(); });
  });

  function openEdit(button) {
    if (!editDialog || !editForm) return;
    $('#edit-name').value = button.dataset.name || '';
    $('#edit-consumer-type').value = button.dataset.type || 'Residential';
    $('#edit-consumer-region').value = button.dataset.region || '';
    $('#edit-phone').value = button.dataset.phone || '';
    $('#edit-email').value = button.dataset.email || '';
    $('#edit-address').value = button.dataset.address || '';
    $('#edit-notes').value = button.dataset.notes || '';
    $('#edit-tariff').value = button.dataset.tariff || '';
    editForm.action = '/consumers/' + button.dataset.id + '/update';
    openModal(editDialog);
  }
  $$('.edit-consumer').forEach(function (button) { button.addEventListener('click', function () { openEdit(button); }); });

  $$('.edit-meter').forEach(function (button) {
    button.addEventListener('click', function () {
      if (!meterEditForm) return;
      $('#meter-edit-code').value = button.dataset.code || '';
      $('#meter-edit-type').value = button.dataset.type || 'Standard';
      $('#meter-edit-phase').value = button.dataset.phase || 'Single';
      $('#meter-edit-capacity').value = button.dataset.capacity || '';
      meterEditForm.action = '/meters/' + button.dataset.id + '/update';
      openModal(meterEditDialog);
    });
  });

  /* confirmation prompts */
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (form.classList.contains('delete-form') && !confirm('Delete this consumer? This cannot be undone.')) event.preventDefault();
    if (form.classList.contains('delete-meter-form') && !confirm('Delete this meter? Meters with readings cannot be deleted.')) event.preventDefault();
    if (form.classList.contains('delete-tariff-form') && !confirm('Delete this tariff? Consumers using it will become unassigned.')) event.preventDefault();
  }, true);

  if (body.dataset.openForm === 'true' && !isViewer) openModal(addDialog);
  if (body.dataset.editId && !isViewer) {
    var editButton = $$('.edit-consumer').find(function (button) { return button.dataset.id === body.dataset.editId; });
    if (editButton) openEdit(editButton);
  }
  if (body.dataset.openReading === 'true' && !isViewer) openReading(null);

  /* ---------- Consumer search, filter, sort ---------- */
  var search = $('#consumer-search');
  var filter = $('#consumer-filter');
  var consumerRows = $$('.consumer-row');
  var noResults = $('#no-search-results');
  var consumerCount = $('#consumer-count');
  var sortState = { key: null, direction: 1 };

  function filterRows() {
    if (!search || !filter) return;
    var term = search.value.trim().toLowerCase();
    var visible = 0;
    consumerRows.forEach(function (row) {
      var show = row.textContent.toLowerCase().indexOf(term) !== -1;
      if (show && filter.value !== 'all' && row.dataset.status !== filter.value) show = false;
      row.hidden = !show;
      if (show) visible += 1;
    });
    if (noResults) noResults.hidden = visible > 0 || consumerRows.length === 0;
    if (consumerCount) consumerCount.textContent = visible + ' of ' + consumerRows.length + ' accounts';
  }
  if (search) search.addEventListener('input', filterRows);
  if (filter) filter.addEventListener('change', filterRows);
  filterRows();

  $$('#consumer-table thead th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.dataset.sort;
      sortState.direction = sortState.key === key ? -sortState.direction : 1;
      sortState.key = key;
      var sorted = consumerRows.slice().sort(function (a, b) {
        if (key === 'usage') return (Number(a.dataset.sortValue) - Number(b.dataset.sortValue)) * sortState.direction;
        return a.dataset.sortText.localeCompare(b.dataset.sortText) * sortState.direction;
      });
      var tbody = $('#consumer-table tbody');
      sorted.forEach(function (row) { tbody.appendChild(row); });
      if (noResults) tbody.appendChild(noResults);
      $$('#consumer-table thead th[data-sort]').forEach(function (other) {
        other.classList.remove('sorted-asc', 'sorted-desc');
      });
      th.classList.add(sortState.direction === 1 ? 'sorted-asc' : 'sorted-desc');
    });
  });

  /* ---------- CSV dropzone ---------- */
  var dropzone = $('#csv-dropzone');
  var csvInput = $('#readings-csv');
  var csvFileName = $('#csv-file-name');
  if (dropzone && csvInput) {
    function showFileName() {
      if (!csvFileName) return;
      var file = csvInput.files && csvInput.files[0];
      csvFileName.hidden = !file;
      csvFileName.textContent = file ? file.name : '';
    }
    csvInput.addEventListener('change', showFileName);
    ['dragenter', 'dragover'].forEach(function (type) {
      dropzone.addEventListener(type, function (event) { event.preventDefault(); dropzone.classList.add('dragover'); });
    });
    ['dragleave', 'drop'].forEach(function (type) {
      dropzone.addEventListener(type, function (event) { event.preventDefault(); dropzone.classList.remove('dragover'); });
    });
    dropzone.addEventListener('drop', function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      try {
        var transfer = new DataTransfer();
        transfer.items.add(files[0]);
        csvInput.files = transfer.files;
        showFileName();
      } catch (error) { /* browser does not allow assignment */ }
    });
  }

  /* ---------- Toasts ---------- */
  function dismissToast(toast) {
    if (!toast || toast.classList.contains('leaving')) return;
    toast.classList.add('leaving');
    setTimeout(function () { toast.remove(); }, 320);
  }
  $$('[data-toast]').forEach(function (toast) {
    var close = toast.querySelector('.flash-close');
    if (close) close.addEventListener('click', function () { dismissToast(toast); });
    setTimeout(function () { dismissToast(toast); }, 6200);
  });

  /* ---------- Clock ---------- */
  var clock = $('#live-clock');
  function updateClock() {
    if (!clock) return;
    clock.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  updateClock();
  setInterval(updateClock, 30000);

  /* ---------- Command palette ---------- */
  var overlay = $('#command-overlay');
  var commandInput = $('#command-input');
  var commandResults = $('#command-results');
  var commandItems = [];
  var commandSelected = 0;

  function buildCommands() {
    var commands = [];
    var pages = [
      ['Dashboard', 'layout-dashboard', 'dashboard'],
      ['Consumers', 'users', 'consumers'],
      ['Meters', 'cpu', 'meters'],
      ['Meter readings', 'gauge', 'readings'],
      ['Analytics', 'chart-no-axes-combined', 'analytics'],
      ['Alerts', 'bell-ring', 'alerts'],
      ['Billing & tariffs', 'receipt-text', 'billing'],
      ['Reports', 'file-text', 'reports'],
      ['Settings', 'settings-2', 'settings']
    ];
    if (!isViewer) {
      commands.push({ group: 'Actions', label: 'Record a meter reading', sub: 'Open the reading dialog', icon: 'gauge', hint: 'R', run: function () { showView('readings'); setTimeout(function () { openReading(null); }, 120); } });
      commands.push({ group: 'Actions', label: 'Add a consumer', sub: 'Create a new account', icon: 'user-plus', hint: 'C', run: function () { showView('consumers'); setTimeout(function () { openModal(addDialog); }, 120); } });
      commands.push({ group: 'Actions', label: 'Add a meter', sub: 'Attach a meter to a consumer', icon: 'cpu', hint: 'M', run: function () { showView('meters'); setTimeout(function () { openModal(meterDialog); }, 120); } });
    }
    commands.push({ group: 'Actions', label: 'Toggle light / dark theme', sub: 'Change the interface appearance', icon: 'sun-moon', hint: 'T', run: function () { applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark'); } });
    commands.push({ group: 'Actions', label: 'Download database backup', sub: 'Save a copy of your local data', icon: 'database', run: function () { location.href = '/backup'; } });
    commands.push({ group: 'Actions', label: 'Print reports', sub: 'Open the print dialog', icon: 'printer', run: function () { showView('reports'); setTimeout(function () { window.print(); }, 200); } });
    pages.forEach(function (page) {
      commands.push({ group: 'Navigate', label: page[0], sub: 'Jump to section', icon: page[1], run: function () { showView(page[2]); } });
    });
    (etData.consumers || []).forEach(function (consumer) {
      commands.push({
        group: 'Consumers', label: consumer.name, sub: consumer.meters + ' · ' + consumer.region, icon: 'user',
        run: function () { location.href = '/?history=' + consumer.id + '#history'; }
      });
    });
    (etData.meters || []).forEach(function (meter) {
      commands.push({
        group: 'Meters', label: meter.code, sub: meter.consumer + ' · ' + meter.type, icon: 'cpu',
        run: function () { showView('readings'); if (!isViewer) setTimeout(function () { openReading(meter.id); }, 140); }
      });
    });
    return commands;
  }
  commandItems = buildCommands();

  function renderCommands(query) {
    if (!commandResults) return;
    var term = (query || '').trim().toLowerCase();
    var filtered = commandItems.filter(function (item) {
      return !term || (item.label + ' ' + (item.sub || '') + ' ' + item.group).toLowerCase().indexOf(term) !== -1;
    }).slice(0, 18);
    if (commandSelected >= filtered.length) commandSelected = Math.max(0, filtered.length - 1);
    var html = '';
    var lastGroup = '';
    filtered.forEach(function (item, index) {
      if (item.group !== lastGroup) { html += '<div class="command-group">' + escapeHtml(item.group) + '</div>'; lastGroup = item.group; }
      html += '<button type="button" class="command-item' + (index === commandSelected ? ' selected' : '') + '" data-index="' + index + '" role="option">' +
        '<span class="ci-icon"><i data-lucide="' + item.icon + '"></i></span>' +
        '<span class="ci-copy"><strong>' + escapeHtml(item.label) + '</strong><small>' + escapeHtml(item.sub || '') + '</small></span>' +
        (item.hint ? '<span class="ci-hint">' + escapeHtml(item.hint) + '</span>' : '') + '</button>';
    });
    commandResults.innerHTML = html || '<div class="command-empty">No matches for “' + escapeHtml(query) + '”.</div>';
    icons();
    $$('.command-item', commandResults).forEach(function (button, index) {
      button.addEventListener('click', function () { runCommand(filtered[index]); });
      button.addEventListener('mousemove', function () {
        commandSelected = index;
        $$('.command-item', commandResults).forEach(function (other, otherIndex) { other.classList.toggle('selected', otherIndex === index); });
      });
    });
    commandResults.__filtered = filtered;
  }

  function runCommand(item) {
    if (!item) return;
    closeCommand();
    item.run();
  }
  function openCommand() {
    if (!overlay) return;
    overlay.hidden = false;
    commandSelected = 0;
    if (commandInput) { commandInput.value = ''; renderCommands(''); }
    setTimeout(function () { commandInput && commandInput.focus(); }, 30);
    body.style.overflow = 'hidden';
  }
  function closeCommand() {
    if (!overlay) return;
    overlay.hidden = true;
    body.style.overflow = '';
    if (commandInput) commandInput.blur();
  }
  var commandTrigger = $('#command-trigger');
  commandTrigger && commandTrigger.addEventListener('click', openCommand);
  overlay && overlay.addEventListener('click', function (event) { if (event.target === overlay) closeCommand(); });
  commandInput && commandInput.addEventListener('input', function () { commandSelected = 0; renderCommands(commandInput.value); });
  commandInput && commandInput.addEventListener('keydown', function (event) {
    var filtered = commandResults.__filtered || [];
    if (event.key === 'ArrowDown') { event.preventDefault(); commandSelected = Math.min(filtered.length - 1, commandSelected + 1); renderCommands(commandInput.value); }
    if (event.key === 'ArrowUp') { event.preventDefault(); commandSelected = Math.max(0, commandSelected - 1); renderCommands(commandInput.value); }
    if (event.key === 'Enter') { event.preventDefault(); runCommand(filtered[commandSelected]); }
    if (event.key === 'Escape') { event.preventDefault(); closeCommand(); }
  });
  document.addEventListener('keydown', function (event) {
    var key = event.key.toLowerCase();
    if ((event.metaKey || event.ctrlKey) && key === 'k') { event.preventDefault(); if (overlay && !overlay.hidden) closeCommand(); else openCommand(); }
    if (event.key === 'Escape' && overlay && !overlay.hidden) closeCommand();
  });

  /* ---------- Keyboard shortcuts ---------- */
  document.addEventListener('keydown', function (event) {
    if (event.target.matches('input, select, textarea')) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key.toLowerCase() === 'r' && !isViewer) { event.preventDefault(); showView('readings'); setTimeout(function () { openReading(null); }, 100); }
    if (event.key.toLowerCase() === 'c' && !isViewer) { event.preventDefault(); showView('consumers'); setTimeout(function () { openModal(addDialog); }, 100); }
    if (event.key.toLowerCase() === 't') { event.preventDefault(); applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark'); }
  });

  /* ---------- Form loading states ---------- */
  $$('form[data-loading]').forEach(function (form) {
    form.addEventListener('submit', function () {
      var button = form.querySelector('[type="submit"]');
      if (!button) return;
      button.classList.add('loading');
      var icon = button.querySelector('svg');
      if (icon) { icon.style.display = 'none'; }
    });
  });

  /* ---------- Show view chart sizing after layout ---------- */
  window.addEventListener('load', function () {
    resizeCharts();
    drawAllSparklines();
    icons();
  });
  setTimeout(function () { resizeCharts(); drawAllSparklines(); }, 380);
})();
