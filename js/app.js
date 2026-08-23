(function () {
  "use strict";

  /* ---------------------------------------------------------
     Config
     --------------------------------------------------------- */

  var DATA_URL = "data/events.json";
  var KNOWN_KEYS = ["title", "description", "date", "time", "url"];
  var WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];
  var MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  var MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

  /* ---------------------------------------------------------
     State
     --------------------------------------------------------- */

  var allEvents = [];
  var filtered = [];
  var lastFocusedEl = null;

  /* ---------------------------------------------------------
     DOM references
     --------------------------------------------------------- */

  var els = {};
  [
    "clockTime", "clockDate",
    "statUpcoming", "statWeek", "statNextTitle", "statNextWhen",
    "searchInput", "clearSearch",
    "dateFrom", "dateTo", "includePast", "onlyDated", "onlyWithLinks", "resetFilters",
    "resultsMeta", "eventList", "emptyState",
    "overlay", "detailPanel", "detailClose", "detailContent"
  ].forEach(function (id) { els[id] = document.getElementById(id); });

  /* ---------------------------------------------------------
     Date helpers (all in local time, ISO strings "YYYY-MM-DD")
     --------------------------------------------------------- */

  function todayISO() {
    var d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function parseLocalDate(iso) {
    var parts = String(iso).split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function daysBetween(fromISO, toISO) {
    var a = parseLocalDate(fromISO);
    var b = parseLocalDate(toISO);
    return Math.round((b - a) / 86400000);
  }

  function addDaysISO(iso, days) {
    var d = parseLocalDate(iso);
    d.setDate(d.getDate() + days);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function formatShortDate(iso) {
    var d = parseLocalDate(iso);
    var month = MONTHS[d.getMonth()];
    return d.getDate() + " " + month.charAt(0) + month.slice(1).toLowerCase();
  }

  function isValidISODate(iso) {
    return typeof iso === "string" && /^\d{4}-\d{2}-\d{2}$/.test(iso) && !isNaN(parseLocalDate(iso).getTime());
  }

  function sortMinutes(timeStr) {
    if (!timeStr) return 0;
    var first = String(timeStr).split(/[-–]/)[0].trim();
    var m = first.match(/^(\d{1,2}):(\d{2})/);
    if (!m) return 0;
    return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  }

  function hasDate(ev) {
    return isValidISODate(ev.date);
  }

  function hasLink(ev) {
    return typeof ev.url === "string" && ev.url.trim().length > 0;
  }

  /* ---------------------------------------------------------
     Small utilities
     --------------------------------------------------------- */

  function escapeHTML(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function escapeRegExp(str) {
    return String(str).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlight(text, query) {
    var safe = escapeHTML(text);
    if (!query) return safe;
    var re = new RegExp("(" + escapeRegExp(query) + ")", "ig");
    return safe.replace(re, "<mark>$1</mark>");
  }

  function humanizeKey(key) {
    return String(key)
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  /* ---------------------------------------------------------
     Live clock — a small nod to the departures-board theme
     --------------------------------------------------------- */

  function updateClock() {
    var now = new Date();
    els.clockTime.textContent = pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds());
    els.clockDate.textContent = now.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  updateClock();
  setInterval(updateClock, 1000);

  /* ---------------------------------------------------------
     Loading data
     --------------------------------------------------------- */

  function loadEvents() {
    fetch(DATA_URL)
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        allEvents = (Array.isArray(data) ? data : []).filter(function (ev) {
          return ev && typeof ev.title === "string" && ev.title.trim().length > 0;
        });
        applyFilters();
      })
      .catch(function (err) {
        allEvents = [];
        els.resultsMeta.textContent = "Couldn't load data/events.json";
        els.eventList.innerHTML = "";
        els.emptyState.hidden = false;
        els.emptyState.textContent =
          "Couldn't load data/events.json (" + err.message + "). " +
          "If you're opening this file directly from disk, serve it with a local web server, or view it once it's published on GitHub Pages.";
        renderStats([]);
      });
  }

  /* ---------------------------------------------------------
     Default date range — the board opens scoped to the next
     two weeks; From/To can be widened, narrowed, or cleared.
     --------------------------------------------------------- */

  function setDefaultDateRange() {
    var today = todayISO();
    els.dateFrom.value = today;
    els.dateTo.value = addDaysISO(today, 14);
  }

  /* ---------------------------------------------------------
     Filtering
     --------------------------------------------------------- */

  function getUpcomingAll() {
    var today = todayISO();
    return allEvents
      .filter(function (ev) { return hasDate(ev) && ev.date >= today; })
      .slice()
      .sort(compareEvents);
  }

  function compareEvents(a, b) {
    var aHas = hasDate(a), bHas = hasDate(b);
    if (aHas && !bHas) return -1;
    if (!aHas && bHas) return 1;
    if (!aHas && !bHas) return (a.title || "").localeCompare(b.title || "");
    if (a.date !== b.date) return a.date < b.date ? -1 : 1;
    return sortMinutes(a.time) - sortMinutes(b.time);
  }

  function applyFilters() {
    var query = els.searchInput.value.trim().toLowerCase();
    var fromVal = els.dateFrom.value;
    var toVal = els.dateTo.value;
    var includePast = els.includePast.checked;
    var onlyDated = els.onlyDated.checked;
    var onlyWithLinks = els.onlyWithLinks.checked;
    var today = todayISO();

    var result = allEvents.filter(function (ev) {
      var dated = hasDate(ev);

      if (onlyDated && !dated) return false;
      if (onlyWithLinks && !hasLink(ev)) return false;

      // Date-range and past/future rules only make sense for dated events;
      // undated events bypass them entirely (they're excluded only by the
      // "Only dated events" toggle above).
      if (dated) {
        if (!includePast && ev.date < today) return false;
        if (fromVal && ev.date < fromVal) return false;
        if (toVal && ev.date > toVal) return false;
      }

      return true;
    });

    if (query) {
      result = result.filter(function (ev) {
        var haystack = ((ev.title || "") + " " + (ev.description || "")).toLowerCase();
        return haystack.indexOf(query) !== -1;
      });
    }

    result.sort(compareEvents);
    filtered = result;

    renderStats(getUpcomingAll());
    renderList(filtered, query);
    renderResultsMeta(filtered.length, query, includePast, fromVal, toVal);
  }

  /* ---------------------------------------------------------
     Rendering — overview stats
     --------------------------------------------------------- */

  function renderStats(upcoming) {
    els.statUpcoming.textContent = String(upcoming.length);

    var today = todayISO();
    var inWeek = upcoming.filter(function (ev) { return daysBetween(today, ev.date) <= 7; }).length;
    els.statWeek.textContent = String(inWeek);

    if (upcoming.length === 0) {
      els.statNextTitle.textContent = "—";
      els.statNextWhen.textContent = "Nothing scheduled";
      return;
    }
    var next = upcoming[0];
    els.statNextTitle.textContent = next.title;
    els.statNextWhen.textContent = relativeLabel(next.date).text + (next.time ? " · " + next.time : "");
  }

  /* ---------------------------------------------------------
     Rendering — results meta line
     --------------------------------------------------------- */

  function renderResultsMeta(count, query, includePast, fromVal, toVal) {
    var scope = includePast ? "events" : "upcoming events";
    var rangeLabel = "";
    if (fromVal && toVal) rangeLabel = " · " + formatShortDate(fromVal) + " – " + formatShortDate(toVal);
    else if (fromVal) rangeLabel = " · from " + formatShortDate(fromVal);
    else if (toVal) rangeLabel = " · until " + formatShortDate(toVal);

    if (query) {
      els.resultsMeta.textContent = count + (count === 1 ? " result" : " results") + " for \u201c" + els.searchInput.value.trim() + "\u201d" + rangeLabel;
    } else {
      els.resultsMeta.textContent = "Showing " + count + " " + scope + rangeLabel;
    }
  }

  /* ---------------------------------------------------------
     Rendering — relative day badge
     --------------------------------------------------------- */

  function relativeLabel(iso) {
    var diff = daysBetween(todayISO(), iso);
    if (diff === 0) return { text: "Today", cls: "badge-today" };
    if (diff === 1) return { text: "Tomorrow", cls: "badge-soon" };
    if (diff > 1 && diff <= 7) return { text: "In " + diff + " days", cls: "badge-soon" };
    if (diff > 7) return { text: "In " + diff + " days", cls: "badge-future" };
    if (diff === -1) return { text: "Yesterday", cls: "badge-past" };
    return { text: Math.abs(diff) + " days ago", cls: "badge-past" };
  }

  /* ---------------------------------------------------------
     Rendering — event list
     --------------------------------------------------------- */

  function renderList(events, query) {
    els.eventList.innerHTML = "";

    if (events.length === 0) {
      els.emptyState.hidden = false;
      return;
    }
    els.emptyState.hidden = true;

    var frag = document.createDocumentFragment();

    events.forEach(function (ev, index) {
      var dated = hasDate(ev);

      var dateBlockHtml;
      var badgeHtml;
      if (dated) {
        var d = parseLocalDate(ev.date);
        var rel = relativeLabel(ev.date);
        dateBlockHtml =
          '<div class="row-date">' +
            '<span class="row-weekday">' + WEEKDAYS[d.getDay()] + '</span>' +
            '<span class="row-day">' + pad(d.getDate()) + '</span>' +
            '<span class="row-month">' + MONTHS[d.getMonth()] + '</span>' +
          '</div>';
        badgeHtml = '<span class="row-badge ' + rel.cls + '">' + rel.text + '</span>';
      } else {
        dateBlockHtml = '<div class="row-date row-date-tbd"><span class="row-date-tbd-label">No<br>date</span></div>';
        badgeHtml = '<span class="row-badge badge-past">No date</span>';
      }

      var row = document.createElement("article");
      row.className = "event-row";
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", "View details for " + ev.title);
      row.style.setProperty("--i", index);

      row.innerHTML =
        dateBlockHtml +
        '<div class="row-main">' +
          '<div class="row-top">' +
            '<h3 class="row-title">' + highlight(ev.title, query) + '</h3>' +
            badgeHtml +
          '</div>' +
          (ev.description ? '<p class="row-excerpt">' + highlight(ev.description, query) + '</p>' : '') +
        '</div>' +
        '<div class="row-time">' +
          '<span>' + escapeHTML(ev.time || "") + '</span>' +
          '<svg viewBox="0 0 24 24" fill="none"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
        '</div>';

      row.addEventListener("click", function () { openDetail(ev, row); });
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openDetail(ev, row);
        }
      });

      frag.appendChild(row);
    });

    els.eventList.appendChild(frag);
  }

  /* ---------------------------------------------------------
     Detail panel
     --------------------------------------------------------- */

  function openDetail(ev, triggerEl) {
    lastFocusedEl = triggerEl;

    var dated = hasDate(ev);
    var niceDate = "Date not specified";
    var rel = null;
    if (dated) {
      var d = parseLocalDate(ev.date);
      var WEEKDAYS_FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
      niceDate = WEEKDAYS_FULL[d.getDay()] + ", " + d.getDate() + " " + MONTHS_FULL[d.getMonth()] + " " + d.getFullYear();
      rel = relativeLabel(ev.date);
    }

    var html = "";
    html += '<span class="detail-eyebrow">Event details</span>';
    html += '<h2 class="detail-title" id="detailTitle">' + escapeHTML(ev.title) + '</h2>';

    html += '<div class="detail-meta">';
    html += chip(iconCalendar(), niceDate);
    if (ev.time) html += chip(iconClock(), escapeHTML(ev.time));
    if (rel) html += '<span class="detail-chip"><span class="row-badge ' + rel.cls + '" style="padding:0;background:none;">' + rel.text + '</span></span>';
    html += '</div>';

    if (ev.description) {
      html += '<p class="detail-description">' + escapeHTML(ev.description) + '</p>';
    }

    if (ev.url) {
      html += '<a class="detail-link" href="' + escapeHTML(ev.url) + '" target="_blank" rel="noopener noreferrer">' +
        'Open event page' + iconExternal() + '</a>';
    } else {
      html += '<span class="detail-link-disabled">No external link provided for this event</span>';
    }

    var extraKeys = Object.keys(ev).filter(function (k) { return KNOWN_KEYS.indexOf(k) === -1; });
    if (extraKeys.length > 0) {
      html += '<div class="detail-extra"><span class="detail-extra-title">More details</span><dl>';
      extraKeys.forEach(function (k) {
        var val = ev[k];
        if (val === null || val === undefined || val === "") return;
        html += '<dt>' + escapeHTML(humanizeKey(k)) + '</dt><dd>' + escapeHTML(val) + '</dd>';
      });
      html += '</dl></div>';
    }

    els.detailContent.innerHTML = html;

    els.overlay.hidden = false;
    els.detailPanel.hidden = false;
    els.detailPanel.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    els.detailClose.focus();
  }

  function chip(icon, text) {
    return '<span class="detail-chip">' + icon + '<span>' + text + '</span></span>';
  }

  function iconCalendar() {
    return '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" stroke-width="2"/><path d="M3 10h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  }
  function iconClock() {
    return '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';
  }
  function iconExternal() {
    return '<svg viewBox="0 0 24 24" fill="none"><path d="M7 17L17 7M9 7h8v8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  }

  function closeDetail() {
    els.overlay.hidden = true;
    els.detailPanel.hidden = true;
    els.detailPanel.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    if (lastFocusedEl) lastFocusedEl.focus();
  }

  /* ---------------------------------------------------------
     Event wiring
     --------------------------------------------------------- */

  els.searchInput.addEventListener("input", function () {
    els.clearSearch.hidden = els.searchInput.value.length === 0;
    applyFilters();
  });

  els.clearSearch.addEventListener("click", function () {
    els.searchInput.value = "";
    els.clearSearch.hidden = true;
    els.searchInput.focus();
    applyFilters();
  });

  els.dateFrom.addEventListener("change", applyFilters);
  els.dateTo.addEventListener("change", applyFilters);
  els.includePast.addEventListener("change", applyFilters);
  els.onlyDated.addEventListener("change", applyFilters);
  els.onlyWithLinks.addEventListener("change", applyFilters);

  els.resetFilters.addEventListener("click", function () {
    els.searchInput.value = "";
    els.clearSearch.hidden = true;
    setDefaultDateRange();
    els.includePast.checked = false;
    els.onlyDated.checked = false;
    els.onlyWithLinks.checked = true;
    applyFilters();
  });

  els.detailClose.addEventListener("click", closeDetail);
  els.overlay.addEventListener("click", closeDetail);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !els.detailPanel.hidden) closeDetail();
  });

  /* ---------------------------------------------------------
     Go
     --------------------------------------------------------- */

  setDefaultDateRange();
  loadEvents();
})();
