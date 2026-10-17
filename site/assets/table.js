// Client-side text filter for the repository table. No external scripts.
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
