// Client-side text filter for the repository table, and click-to-sort headers for every
// table on the page. No external scripts.
(function () {
  var input = document.getElementById("repo-filter");
  var table = document.getElementById("repo-table");
  if (!input || !table) return;
  var rows = Array.prototype.slice.call(table.tBodies[0].rows);
  var count = document.getElementById("repo-count");

  function apply() {
    var terms = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    rows.forEach(function (row) {
      var text = row.textContent.toLowerCase();
      var match = terms.every(function (t) { return text.indexOf(t) !== -1; });
      row.hidden = !match;
      if (match) shown += 1;
    });
    if (count) count.textContent = shown + " of " + rows.length + " repositories";
  }

  input.addEventListener("input", apply);
  apply();
})();

(function () {
  // Numbers compare as numbers ("9" < "10"), text ignoring case; empty cells sort last
  // either way. A cell's `data-sort` attribute, if set, is used instead of its text.
  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

  var style = document.createElement("style");
  style.textContent =
    "th > button.sort { background: none; border: 0; padding: 0; margin: 0; font: inherit;" +
    " color: inherit; text-align: inherit; cursor: pointer; }" +
    "th > button.sort:focus-visible { outline: 2px solid currentColor; }" +
    // A no-break space keeps the arrow on the label's last line. The `/ ""` alt text keeps
    // screen readers from reading the arrow (aria-sort says it); older browsers ignore that
    // declaration and use the one before it.
    "th > button.sort::after { content: \"\\A0\\2195\"; content: \"\\A0\\2195\" / \"\"; opacity: 0.35; }" +
    "th[aria-sort=ascending] > button.sort::after" +
    " { content: \"\\A0\\25B2\"; content: \"\\A0\\25B2\" / \"\"; opacity: 1; }" +
    "th[aria-sort=descending] > button.sort::after" +
    " { content: \"\\A0\\25BC\"; content: \"\\A0\\25BC\" / \"\"; opacity: 1; }";
  document.head.appendChild(style);

  function key(row, col) {
    var cell = row.cells[col];
    if (!cell) return "";
    var v = cell.getAttribute("data-sort");
    return (v !== null ? v : cell.textContent).trim();
  }

  function sortable(table) {
    var body = table.tBodies[0];
    var head = table.tHead && table.tHead.rows[0];
    if (!body || !head) return;
    // Original positions break ties, so equal keys keep the generated order.
    Array.prototype.forEach.call(body.rows, function (row, i) { row.dataset.pos = i; });
    var headers = Array.prototype.slice.call(head.cells);

    function sortBy(col, dir) {
      var sign = dir === "ascending" ? 1 : -1;
      var sorted = Array.prototype.slice.call(body.rows).sort(function (a, b) {
        var x = key(a, col), y = key(b, col);
        if (!x !== !y) return x ? -1 : 1;
        return sign * collator.compare(x, y) || a.dataset.pos - b.dataset.pos;
      });
      var frag = document.createDocumentFragment();
      sorted.forEach(function (row) { frag.appendChild(row); });
      body.appendChild(frag);
      headers.forEach(function (th, i) {
        if (i === col) th.setAttribute("aria-sort", dir);
        else th.removeAttribute("aria-sort");
      });
    }

    headers.forEach(function (th, col) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "sort";
      button.title = "Sort by " + th.textContent.trim();
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      button.addEventListener("click", function () {
        sortBy(col, th.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending");
      });
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("table"), sortable);
})();
