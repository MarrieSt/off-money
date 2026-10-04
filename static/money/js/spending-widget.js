(() => {
  const root = document.querySelector("[data-spending-widget]");
  if (!root) return;

  const apiUrl = root.dataset.apiUrl;
  const transactionsUrl = root.dataset.transactionsUrl;
  const storageKey = "money-spending-widget-state";
  const cache = new Map();
  const palette = [
    "#1479d8", "#d56d43", "#218c72", "#8d67a8", "#b28a22", "#c24f70",
    "#348fa2", "#6e843b", "#d05e9a", "#5571b8", "#aa6941", "#438b55",
  ];

  let state = { granularity: "day", stack_by: "category", window_anchor: null, hidden_series: [] };
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (saved) state = { ...state, ...saved };
  } catch (_error) {
    sessionStorage.removeItem(storageKey);
  }

  const elements = {
    total: root.querySelector("[data-total]"),
    range: root.querySelector("[data-range]"),
    chart: root.querySelector("[data-chart]"),
    chartShell: root.querySelector("[data-chart-shell]"),
    legend: root.querySelector("[data-legend]"),
    showAll: root.querySelector("[data-show-all]"),
    loading: root.querySelector("[data-loading]"),
    empty: root.querySelector("[data-empty]"),
    error: root.querySelector("[data-error]"),
    retry: root.querySelector("[data-retry]"),
    tooltip: root.querySelector("[data-tooltip]"),
    drawer: root.querySelector("[data-drawer]"),
    drawerTitle: root.querySelector("[data-drawer-title]"),
    drawerBody: root.querySelector("[data-drawer-body]"),
    stackSelect: root.querySelector("[data-stack-by]"),
    currentButton: root.querySelector("[data-current]"),
    nextButton: root.querySelector("[data-next]"),
  };
  let responseData = null;
  let requestVersion = 0;

  const money = (value) => new Intl.NumberFormat("en-GB", {
    style: "currency", currency: "GBP", minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(value));

  const colorFor = (key) => {
    if (key === "__other__") return "#8192a4";
    let hash = 2166136261;
    for (const character of key) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
    return palette[(hash >>> 0) % palette.length];
  };

  const formatDate = (iso, options = { day: "numeric", month: "short" }) =>
    new Intl.DateTimeFormat("en-GB", options).format(new Date(`${iso}T00:00:00`));

  const localIsoDate = (value) => {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };

  const rangeText = (data) => {
    const startYear = data.range_start.slice(0, 4);
    const endYear = data.range_end.slice(0, 4);
    const start = formatDate(data.range_start, startYear === endYear
      ? { day: "numeric", month: "short" }
      : { day: "numeric", month: "short", year: "numeric" });
    const end = formatDate(data.range_end, { day: "numeric", month: "short", year: "numeric" });
    return `${start} – ${end}`;
  };

  function persist() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(state));
    } catch (_error) {
      // Session storage can be unavailable in private browsing; widget state still works in memory.
    }
  }

  async function fetchJson(url, useCache = false) {
    if (useCache && cache.has(url)) return cache.get(url);
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Unable to load spending");
    if (useCache) {
      cache.set(url, payload);
      if (cache.size > 20) cache.delete(cache.keys().next().value);
    }
    return payload;
  }

  async function load() {
    const version = ++requestVersion;
    elements.loading.hidden = false;
    elements.chartShell.hidden = true;
    elements.legend.hidden = true;
    elements.empty.hidden = true;
    elements.error.hidden = true;

    const params = new URLSearchParams({
      granularity: state.granularity,
      stack_by: state.stack_by,
    });
    if (state.window_anchor) params.set("end_date", state.window_anchor);

    try {
      const data = await fetchJson(`${apiUrl}?${params.toString()}`, true);
      if (version !== requestVersion) return;
      responseData = data;
      state.window_anchor = data.window_anchor;
      const visibleKeys = new Set(data.series.map((item) => item.key));
      state.hidden_series = state.hidden_series.filter((key) => visibleKeys.has(key));
      persist();
      render(data);
      elements.loading.hidden = true;
      elements.chartShell.hidden = false;
      elements.legend.hidden = false;
      elements.empty.hidden = Number(data.total) !== 0;
      elements.currentButton.hidden = data.is_current;
      elements.currentButton.textContent = state.granularity === "day" ? "Today" : "Current";
      elements.nextButton.disabled = !data.can_navigate_forward;
      elements.stackSelect.value = state.stack_by;
      root.querySelectorAll("[data-granularity]").forEach((button) => {
        const selected = button.dataset.granularity === state.granularity;
        button.setAttribute("aria-pressed", String(selected));
      });
      elements.total.textContent = money(data.total);
      elements.range.textContent = `${rangeText(data)} · GBP`;
    } catch (error) {
      if (version !== requestVersion) return;
      elements.loading.hidden = true;
      elements.chartShell.hidden = true;
      elements.legend.hidden = true;
      elements.error.hidden = false;
      elements.error.textContent = error.message || "Unable to load spending";
    }
  }

  function render(data) {
    renderChart(data);
    renderLegend(data);
  }

  function renderChart(data) {
    const chart = elements.chart;
    chart.replaceChildren();
    chart.setAttribute("aria-label", `Spending by ${state.stack_by}, ${rangeText(data)}`);

    const visibleSeries = data.series.filter((series) => !state.hidden_series.includes(series.key));
    const visibleKeys = new Set(visibleSeries.map((series) => series.key));
    let maxPositive = 0;
    let minNegative = 0;
    const visibleBuckets = data.buckets.map((bucket) => {
      const values = bucket.segments
        .filter((segment) => visibleKeys.has(segment.key))
        .map((segment) => Number(segment.value));
      const positive = values.filter((value) => value > 0).reduce((sum, value) => sum + value, 0);
      const negative = values.filter((value) => value < 0).reduce((sum, value) => sum + value, 0);
      maxPositive = Math.max(maxPositive, positive);
      minNegative = Math.min(minNegative, negative);
      return { bucket, positive, negative };
    });
    const span = Math.max(maxPositive - minNegative, 1);
    const zeroTop = (maxPositive / span) * 100;

    const axis = document.createElement("div");
    axis.className = "chart-axis";
    for (let index = 0; index <= 4; index += 1) {
      const value = maxPositive - (span * index) / 4;
      const tick = document.createElement("span");
      tick.className = "chart-tick";
      tick.style.top = `${index * 25}%`;
      tick.textContent = money(value);
      axis.append(tick);
    }
    chart.append(axis);

    const plot = document.createElement("div");
    plot.className = "chart-plot";
    for (let index = 0; index <= 4; index += 1) {
      const line = document.createElement("span");
      line.className = `chart-gridline${index === zeroTop / 25 ? " chart-zero-line" : ""}`;
      line.style.top = `${index * 25}%`;
      plot.append(line);
    }
    const zeroLine = document.createElement("span");
    zeroLine.className = "chart-zero-line";
    zeroLine.style.top = `${zeroTop}%`;
    plot.append(zeroLine);

    const columns = document.createElement("div");
    columns.className = "chart-columns";
    visibleBuckets.forEach(({ bucket, positive, negative }) => {
      const column = document.createElement("div");
      column.className = "chart-column";
      const hitArea = document.createElement("button");
      hitArea.type = "button";
      hitArea.className = "chart-column-hitarea";
      hitArea.dataset.bucketKey = bucket.key;
      hitArea.setAttribute("aria-label", `${bucket.key}: ${money(bucket.total)}. View transactions.`);
      column.append(hitArea);

      const track = document.createElement("div");
      track.className = "chart-bar-track";
      let positiveCursor = 0;
      let negativeCursor = 0;
      bucket.segments.forEach((segment) => {
        const value = Number(segment.value);
        if (!value || !visibleKeys.has(segment.key)) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = `chart-segment${value < 0 ? " chart-segment-negative" : ""}`;
        button.style.backgroundColor = colorFor(segment.key);
        button.style.height = `${(Math.abs(value) / span) * 100}%`;
        if (value > 0) {
          positiveCursor += value;
          button.style.top = `${zeroTop - (positiveCursor / span) * 100}%`;
        } else {
          button.style.top = `${zeroTop + (negativeCursor / span) * 100}%`;
          negativeCursor += Math.abs(value);
        }
        button.dataset.bucketKey = bucket.key;
        button.dataset.seriesKey = segment.key;
        button.setAttribute("aria-label", `${segment.label}: ${money(value)} on ${bucket.key}. View transactions.`);
        track.append(button);
      });
      column.append(track);

      const label = document.createElement("span");
      label.className = "chart-date-label";
      label.textContent = bucketLabel(bucket, data.granularity);
      column.append(label);
      columns.append(column);
    });
    plot.append(columns);
    chart.append(plot);
  }

  function bucketLabel(bucket, granularity) {
    if (granularity === "month") {
      return formatDate(bucket.start, { month: "short" });
    }
    if (granularity === "week") {
      const start = formatDate(bucket.start, { day: "numeric", month: "short" });
      const end = formatDate(bucket.end, { day: "numeric", month: "short" });
      return `${start}–${end}`;
    }
    return formatDate(bucket.start, { day: "numeric", month: "short" });
  }

  function renderLegend(data) {
    elements.legend.replaceChildren();
    data.series.forEach((series) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `legend-item${state.hidden_series.includes(series.key) ? " is-hidden" : ""}`;
      button.dataset.seriesKey = series.key;
      button.setAttribute("aria-pressed", String(!state.hidden_series.includes(series.key)));
      const swatch = document.createElement("span");
      swatch.className = "legend-swatch";
      swatch.style.backgroundColor = colorFor(series.key);
      const label = document.createElement("span");
      label.textContent = series.label;
      const total = document.createElement("span");
      total.className = "legend-total";
      total.textContent = money(series.total);
      button.append(swatch, label, total);
      elements.legend.append(button);
    });
    elements.showAll.hidden = state.hidden_series.length === 0;
  }

  function showTooltip(bucketKey, seriesKey, pointerEvent) {
    const bucket = responseData.buckets.find((item) => item.key === bucketKey);
    if (!bucket) return;
    const lines = document.createDocumentFragment();
    const heading = document.createElement("strong");
    heading.textContent = bucketLabel(bucket, responseData.granularity);
    lines.append(heading);
    const total = document.createElement("span");
    total.className = "tooltip-total";
    total.textContent = `Total ${money(bucket.total)}`;
    lines.append(total);
    bucket.breakdown.forEach((item) => {
      const row = document.createElement("span");
      row.className = `tooltip-row${item.key === seriesKey ? " is-highlighted" : ""}`;
      row.textContent = `${item.label}  ${money(item.value)}`;
      lines.append(row);
    });
    elements.tooltip.replaceChildren(lines);
    elements.tooltip.hidden = false;
    const x = pointerEvent?.clientX ?? window.innerWidth / 2;
    const y = pointerEvent?.clientY ?? 150;
    const left = Math.min(x + 14, window.innerWidth - elements.tooltip.offsetWidth - 12);
    const top = Math.min(y + 14, window.innerHeight - elements.tooltip.offsetHeight - 12);
    elements.tooltip.style.left = `${Math.max(12, left)}px`;
    elements.tooltip.style.top = `${Math.max(12, top)}px`;
  }

  async function openDrawer(bucket, series) {
    elements.drawer.hidden = false;
    elements.drawerTitle.textContent = series
      ? `${series.label} · ${bucketLabel(bucket, responseData.granularity)}`
      : `Transactions · ${bucketLabel(bucket, responseData.granularity)}`;
    elements.drawerBody.replaceChildren();
    const loading = document.createElement("p");
    loading.textContent = "Loading transactions…";
    elements.drawerBody.append(loading);
    const params = new URLSearchParams({
      start_date: bucket.start,
      end_date: bucket.end,
      stack_by: state.stack_by,
    });
    if (series) {
      params.set("series_key", series.key);
      if (series.key === "__other__") {
        series.excluded_keys.forEach((key) => params.append("excluded_keys", key));
      }
    }
    try {
      const payload = await fetchJson(`${transactionsUrl}?${params.toString()}`);
      elements.drawerBody.replaceChildren();
      if (payload.rows.length === 0) {
        const empty = document.createElement("p");
        empty.className = "drawer-empty";
        empty.textContent = "No matching transactions in this period.";
        elements.drawerBody.append(empty);
        return;
      }
      payload.rows.forEach((row) => {
        const item = document.createElement("article");
        item.className = "drawer-transaction";
        const top = document.createElement("div");
        top.className = "drawer-transaction-top";
        const label = document.createElement("strong");
        label.textContent = row.description;
        const value = document.createElement("span");
        value.className = row.is_refund ? "drawer-refund-value" : "";
        value.textContent = money(row.contribution);
        top.append(label, value);
        const detail = document.createElement("p");
        detail.textContent = `${formatDate(row.date, { day: "numeric", month: "short", year: "numeric" })} · ${row.account || "Account"} · ${row.category}${row.is_refund ? " · Refund" : ""}`;
        item.append(top, detail);
        elements.drawerBody.append(item);
      });
      if (payload.has_more) {
        const more = document.createElement("p");
        more.className = "drawer-more";
        more.textContent = "Showing the latest 200 matching transactions.";
        elements.drawerBody.append(more);
      }
    } catch (error) {
      elements.drawerBody.replaceChildren();
      const message = document.createElement("p");
      message.className = "drawer-error";
      message.textContent = error.message || "Unable to load transactions.";
      elements.drawerBody.append(message);
    }
  }

  function closeDrawer() {
    elements.drawer.hidden = true;
  }

  function shiftAnchor(amount) {
    if (!state.window_anchor) return;
    const value = new Date(`${state.window_anchor}T00:00:00`);
    if (state.granularity === "day") value.setDate(value.getDate() + amount * 7);
    if (state.granularity === "week") value.setDate(value.getDate() + amount * 49);
    if (state.granularity === "month") value.setMonth(value.getMonth() + amount * 7);
    state.window_anchor = localIsoDate(value);
  }

  root.querySelectorAll("[data-granularity]").forEach((button) => {
    button.addEventListener("click", () => {
      state.granularity = button.dataset.granularity;
      persist();
      load();
    });
  });
  elements.stackSelect.addEventListener("change", () => {
    state.stack_by = elements.stackSelect.value;
    state.hidden_series = [];
    persist();
    load();
  });
  root.querySelector("[data-previous]").addEventListener("click", () => {
    shiftAnchor(-1);
    persist();
    load();
  });
  elements.nextButton.addEventListener("click", () => {
    if (elements.nextButton.disabled) return;
    shiftAnchor(1);
    persist();
    load();
  });
  elements.currentButton.addEventListener("click", () => {
    state.window_anchor = null;
    persist();
    load();
  });
  elements.retry.addEventListener("click", load);
  elements.showAll.addEventListener("click", () => {
    state.hidden_series = [];
    persist();
    render(responseData);
  });
  elements.legend.addEventListener("click", (event) => {
    const button = event.target.closest("[data-series-key]");
    if (!button || !responseData) return;
    const selected = button.dataset.seriesKey;
    const allKeys = responseData.series.map((series) => series.key);
    const currentVisible = allKeys.filter((key) => !state.hidden_series.includes(key));
    state.hidden_series = currentVisible.length === 1 && currentVisible[0] === selected
      ? []
      : allKeys.filter((key) => key !== selected);
    persist();
    render(responseData);
  });
  elements.chart.addEventListener("pointerover", (event) => {
    const segment = event.target.closest("[data-series-key][data-bucket-key]");
    const column = event.target.closest("[data-bucket-key]");
    if (!column) return;
    showTooltip(column.dataset.bucketKey, segment?.dataset.seriesKey, event);
  });
  elements.chart.addEventListener("pointermove", (event) => {
    if (!elements.tooltip.hidden) {
      const left = Math.min(event.clientX + 14, window.innerWidth - elements.tooltip.offsetWidth - 12);
      const top = Math.min(event.clientY + 14, window.innerHeight - elements.tooltip.offsetHeight - 12);
      elements.tooltip.style.left = `${Math.max(12, left)}px`;
      elements.tooltip.style.top = `${Math.max(12, top)}px`;
    }
  });
  elements.chart.addEventListener("pointerleave", () => { elements.tooltip.hidden = true; });
  elements.chart.addEventListener("focusin", (event) => {
    const target = event.target.closest("[data-bucket-key]");
    if (target) showTooltip(target.dataset.bucketKey, target.dataset.seriesKey);
  });
  elements.chart.addEventListener("focusout", () => { elements.tooltip.hidden = true; });
  elements.chart.addEventListener("click", (event) => {
    const segment = event.target.closest("[data-series-key][data-bucket-key]");
    const target = segment || event.target.closest("[data-bucket-key]");
    if (!target || !responseData) return;
    const bucket = responseData.buckets.find((item) => item.key === target.dataset.bucketKey);
    const series = segment
      ? responseData.series.find((item) => item.key === segment.dataset.seriesKey)
      : null;
    if (bucket) openDrawer(bucket, series);
  });
  root.querySelector("[data-drawer-close]").addEventListener("click", closeDrawer);
  elements.drawer.addEventListener("click", (event) => {
    if (event.target === elements.drawer) closeDrawer();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });

  load();
})();
