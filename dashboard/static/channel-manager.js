(() => {
  "use strict";

  const STORAGE_KEY = "influencer-hub.channel-manager.views.v1";
  const nameInput = document.getElementById("saved-view-name");
  const viewSelect = document.getElementById("saved-view-select");
  const status = document.getElementById("saved-view-status");
  if (!nameInput || !viewSelect || !status) return;

  const readViews = () => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(saved)
        ? saved.filter((item) => item && typeof item.name === "string" && typeof item.query === "string").slice(0, 20)
        : [];
    } catch (_error) {
      return [];
    }
  };

  const renderViews = (selected = "") => {
    const views = readViews();
    viewSelect.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = views.length ? "Choose a saved view" : "No saved views yet";
    viewSelect.appendChild(placeholder);
    views.forEach((view, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = view.name;
      viewSelect.appendChild(option);
    });
    if (selected !== "") viewSelect.value = selected;
  };

  const announce = (message, isError = false) => {
    status.textContent = message;
    status.setAttribute("role", isError ? "alert" : "status");
  };

  renderViews();

  document.getElementById("saved-view-save")?.addEventListener("click", () => {
    const name = nameInput.value.trim().slice(0, 40);
    if (!name) {
      announce("Enter a name for this view first.", true);
      nameInput.focus();
      return;
    }
    const params = new URLSearchParams(window.location.search);
    params.delete("page");
    const query = params.toString();
    const views = readViews();
    const existingIndex = views.findIndex((view) => view.name.toLowerCase() === name.toLowerCase());
    const entry = { name, query };
    if (existingIndex >= 0) views[existingIndex] = entry;
    else views.unshift(entry);
    const bounded = views.slice(0, 20);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(bounded));
      renderViews(String(existingIndex >= 0 ? existingIndex : 0));
      nameInput.value = name;
      announce(`Saved “${name}” on this browser.`);
    } catch (_error) {
      announce("This browser blocked local storage; the view was not saved.", true);
    }
  });

  document.getElementById("saved-view-apply")?.addEventListener("click", () => {
    const index = Number.parseInt(viewSelect.value, 10);
    const view = readViews()[index];
    if (!view) {
      announce("Choose a saved view to open.", true);
      return;
    }
    const suffix = view.query ? `?${view.query}` : "";
    window.location.assign(`${window.location.pathname}${suffix}`);
  });

  document.getElementById("saved-view-delete")?.addEventListener("click", () => {
    const index = Number.parseInt(viewSelect.value, 10);
    const views = readViews();
    if (!Number.isInteger(index) || !views[index]) {
      announce("Choose a saved view to remove.", true);
      return;
    }
    const [removed] = views.splice(index, 1);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(views));
      renderViews();
      announce(`Removed “${removed.name}”.`);
    } catch (_error) {
      announce("This browser blocked local storage; the view could not be removed.", true);
    }
  });
})();
