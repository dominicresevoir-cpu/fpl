// Vanilla JS, no build step — small enough to keep in one file.

(function () {
  const dataEl = document.getElementById("players-data");
  if (!dataEl) return;

  const allPlayers = JSON.parse(dataEl.textContent);
  const tbody = document.getElementById("players-tbody");
  const filterInput = document.getElementById("player-filter");
  const headers = document.querySelectorAll("#players-table th[data-sort]");

  let sortKey = "score";
  let sortDir = -1; // descending, matches the server's initial sort

  function render() {
    const query = (filterInput.value || "").toLowerCase();
    const rows = allPlayers
      .filter((p) => !query || p.name.toLowerCase().includes(query) || p.team_name.toLowerCase().includes(query))
      .sort((a, b) => {
        const av = a[sortKey];
        const bv = b[sortKey];
        if (typeof av === "number" && typeof bv === "number") return (av - bv) * sortDir;
        return String(av).localeCompare(String(bv)) * sortDir;
      });

    tbody.innerHTML = rows
      .map(
        (p) => `<tr>
          <td>${p.name}</td>
          <td>${p.team_name}</td>
          <td>${p.position}</td>
          <td>£${p.now_cost.toFixed(1)}m</td>
          <td>${p.score.toFixed(2)}</td>
          <td>${p.form.toFixed(1)}</td>
          <td>${p.ep_next.toFixed(1)}</td>
        </tr>`
      )
      .join("");
  }

  headers.forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      sortDir = key === sortKey ? -sortDir : -1;
      sortKey = key;
      render();
    });
  });

  filterInput.addEventListener("input", render);
  render();
})();

(function () {
  const form = document.getElementById("run-form");
  const loading = document.getElementById("run-loading");
  if (!form || !loading) return;

  form.addEventListener("submit", () => {
    loading.hidden = false;
    form.querySelector("button").disabled = true;
  });
})();
