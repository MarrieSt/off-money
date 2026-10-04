(() => {
  const root = document.querySelector("[data-behaviour-calendar]");
  if (!root) return;

  const monthUrl = root.dataset.monthUrl;
  const dayUrl = root.dataset.dayUrl;
  const transactionsUrl = root.dataset.transactionsUrl;
  const palette = [
    "#1479d8", "#d56d43", "#218c72", "#8d67a8", "#b28a22", "#c24f70",
    "#348fa2", "#6e843b", "#d05e9a", "#5571b8", "#aa6941", "#438b55",
  ];
  const elements = {
    month: root.querySelector("[data-calendar-month]"),
    previous: root.querySelector("[data-calendar-previous]"),
    next: root.querySelector("[data-calendar-next]"),
    today: root.querySelector("[data-calendar-today]"),
    error: root.querySelector("[data-calendar-error]"),
    retry: root.querySelector("[data-calendar-retry]"),
    loading: root.querySelector("[data-calendar-loading]"),
    content: root.querySelector("[data-calendar-content]"),
    days: root.querySelector("[data-calendar-days]"),
    detail: root.querySelector("[data-calendar-detail]"),
    detailDate: root.querySelector("[data-detail-date]"),
    detailTotal: root.querySelector("[data-detail-total]"),
    detailBehavior: root.querySelector("[data-detail-behavior]"),
    detailEmpty: root.querySelector("[data-detail-empty]"),
    detailTransactions: root.querySelector("[data-detail-transactions]"),
    detailClose: root.querySelector("[data-detail-close]"),
    viewTransactions: root.querySelector("[data-view-transactions]"),
  };

  let todayIso = null;
  let currentMonth = null;
  let selectedDate = null;
  let monthRequest = 0;
  let detailRequest = 0;

  const money = (value, whole = false) => new Intl.NumberFormat("en-GB", {
    style: "currency",
    currency: "GBP",
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: whole ? 0 : 2,
  }).format(Number(value));

  const parseMonth = (value) => {
    const [year, month] = value.split("-").map(Number);
    return { year, month };
  };

  const localMonth = (value) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
  const daysInMonth = (year, month) => new Date(year, month, 0).getDate();
  const mondayOffset = (year, month) => (new Date(year, month - 1, 1).getDay() + 6) % 7;
  const dateIso = (year, month, day) => `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const localDate = (iso) => new Date(`${iso}T00:00:00`);
  const fullDate = (iso) => new Intl.DateTimeFormat("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).format(localDate(iso));

  const behaviorText = (band) => ({
    zero: "No-spend day",
    low: "Low-spend day",
    medium: "Higher-spend day",
    high: "High-spend day",
    future: "Future day",
  }[band] || "Spending day");

  const behaviorLabel = (day) => {
    if (day.is_future) return `${fullDate(day.date)}, future date`;
    if (day.band === "zero") return `${fullDate(day.date)}, no spend`;
    return `${fullDate(day.date)}, ${money(day.total_spend)} spent, ${behaviorText(day.band).toLowerCase()}`;
  };

  const colorFor = (key) => {
    let hash = 2166136261;
    for (const character of key) hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
    return palette[(hash >>> 0) % palette.length];
  };

  async function getJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Unable to load calendar");
    return payload;
  }

  function renderMonth(payload) {
    currentMonth = payload.month;
    todayIso = payload.today;
    elements.month.textContent = payload.month_label;
    elements.next.disabled = !payload.can_navigate_forward;
    elements.days.replaceChildren();

    const { year, month } = parseMonth(payload.month);
    const numberOfDays = daysInMonth(year, month);
    const offset = mondayOffset(year, month);
    const cellCount = Math.ceil((offset + numberOfDays) / 7) * 7;
    const dayMap = new Map(payload.days.map((day) => [day.date, day]));

    for (let cellIndex = 0; cellIndex < cellCount; cellIndex += 1) {
      const monthDay = cellIndex - offset + 1;
      if (monthDay < 1 || monthDay > numberOfDays) {
        const pad = document.createElement("div");
        pad.className = "behaviour-day behaviour-day-padding";
        const padDate = document.createElement("span");
        padDate.className = "behaviour-day-number";
        padDate.textContent = String(monthDay < 1
          ? daysInMonth(year, month - 1) + monthDay
          : monthDay - numberOfDays);
        pad.setAttribute("aria-hidden", "true");
        pad.append(padDate);
        elements.days.append(pad);
        continue;
      }

      const day = dayMap.get(dateIso(year, month, monthDay));
      const button = document.createElement("button");
      button.type = "button";
      button.className = `behaviour-day behaviour-day-${day.band}${day.is_today ? " is-today" : ""}${day.date === selectedDate ? " is-selected" : ""}`;
      button.disabled = day.is_future;
      button.setAttribute("aria-label", behaviorLabel(day));
      button.setAttribute("aria-pressed", String(day.date === selectedDate));
      button.dataset.date = day.date;

      const number = document.createElement("span");
      number.className = "behaviour-day-number";
      number.textContent = String(day.day_number);
      button.append(number);

      const total = document.createElement("span");
      total.className = "behaviour-day-total";
      total.textContent = day.is_future ? "" : money(day.total_spend, true);
      button.append(total);

      const bars = document.createElement("span");
      bars.className = "behaviour-day-bars";
      bars.setAttribute("aria-hidden", "true");
      day.bars.forEach((bar) => {
        const mark = document.createElement("i");
        mark.className = "behaviour-mini-bar";
        mark.style.height = `${bar.height}%`;
        mark.style.backgroundColor = colorFor(bar.category);
        mark.title = `${bar.description}: ${money(bar.amount)}`;
        bars.append(mark);
      });
      button.append(bars);
      button.addEventListener("click", () => selectDay(day.date));
      elements.days.append(button);
    }

    if (selectedDate && selectedDate.startsWith(payload.month)) {
      const selected = payload.days.find((day) => day.date === selectedDate);
      if (selected && !selected.is_future) loadDay(selectedDate);
      else closeDetail();
    } else {
      closeDetail();
    }
  }

  async function loadMonth(month) {
    const request = ++monthRequest;
    elements.loading.hidden = false;
    elements.content.hidden = true;
    elements.error.hidden = true;
    const params = new URLSearchParams();
    if (month) params.set("month", month);
    try {
      const suffix = params.toString() ? `?${params.toString()}` : "";
      const payload = await getJson(`${monthUrl}${suffix}`);
      if (request !== monthRequest) return;
      renderMonth(payload);
      elements.loading.hidden = true;
      elements.content.hidden = false;
    } catch (error) {
      if (request !== monthRequest) return;
      elements.loading.hidden = true;
      elements.error.hidden = false;
      elements.error.querySelector("span").textContent = error.message || "Unable to load calendar.";
    }
  }

  async function selectDay(isoDate) {
    selectedDate = isoDate;
    elements.content.classList.add("has-selection");
    elements.days.querySelectorAll("[data-date]").forEach((cell) => {
      const selected = cell.dataset.date === isoDate;
      cell.classList.toggle("is-selected", selected);
      cell.setAttribute("aria-pressed", String(selected));
    });
    elements.detail.hidden = false;
    elements.detailDate.textContent = fullDate(isoDate);
    elements.detailTotal.textContent = "Loading…";
    elements.detailBehavior.textContent = "";
    elements.detailTransactions.replaceChildren();
    elements.detailEmpty.hidden = true;
    elements.viewTransactions.href = `${transactionsUrl}?date=${encodeURIComponent(isoDate)}`;

    const request = ++detailRequest;
    try {
      const params = new URLSearchParams({ date: isoDate });
      const payload = await getJson(`${dayUrl}?${params.toString()}`);
      if (request !== detailRequest) return;
      elements.detailTotal.textContent = money(payload.total_spend);
      elements.detailTotal.dataset.band = payload.band;
      elements.detailBehavior.textContent = behaviorText(payload.band);
      elements.detailEmpty.hidden = payload.transactions.length > 0;
      elements.detailTransactions.replaceChildren();
      payload.transactions.forEach((transaction) => {
        const row = document.createElement("a");
        row.className = "calendar-detail-transaction";
        row.href = `/transactions/${transaction.id}/`;
        row.target = "_blank";
        row.rel = "noopener";
        const description = document.createElement("strong");
        description.textContent = transaction.description;
        const detail = document.createElement("span");
        detail.textContent = `${transaction.category} · ${transaction.account}`;
        const amount = document.createElement("b");
        amount.textContent = money(transaction.amount);
        row.append(description, detail, amount);
        elements.detailTransactions.append(row);
      });
    } catch (error) {
      if (request !== detailRequest) return;
      elements.detailTotal.textContent = "—";
      elements.detailBehavior.textContent = error.message || "Unable to load day details.";
    }
  }

  function closeDetail() {
    selectedDate = null;
    detailRequest += 1;
    elements.detail.hidden = true;
    elements.content.classList.remove("has-selection");
  }

  elements.previous.addEventListener("click", () => {
    const { year, month } = parseMonth(currentMonth || localMonth(new Date()));
    loadMonth(localMonth(new Date(year, month - 2, 1)));
  });
  elements.next.addEventListener("click", () => {
    if (elements.next.disabled) return;
    const { year, month } = parseMonth(currentMonth);
    loadMonth(localMonth(new Date(year, month, 1)));
  });
  elements.today.addEventListener("click", () => loadMonth(null));
  elements.retry.addEventListener("click", () => loadMonth(currentMonth));
  elements.detailClose.addEventListener("click", closeDetail);

  loadMonth(null);
})();
