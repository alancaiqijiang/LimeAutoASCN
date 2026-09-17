(() => {
  const toggle = document.querySelector(".catalog-sidebar-toggle");
  const sidebar = document.getElementById("catalog-sidebar");
  const sidebarClose = document.querySelector(".catalog-sidebar-close");
  const drawerQuery = window.matchMedia("(max-width: 760px)");
  const sidebarOpen = () => document.body.classList.contains("sidebar-open");
  const setSidebar = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
  };
  if (toggle) {
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      setSidebar(!sidebarOpen());
    });
  }
  if (sidebarClose) {
    sidebarClose.addEventListener("click", (event) => {
      event.preventDefault();
      setSidebar(false);
    });
  }
  if (sidebar && drawerQuery.addEventListener) {
    const syncDrawer = () => {
      if (!drawerQuery.matches) setSidebar(false);
    };
    drawerQuery.addEventListener("change", syncDrawer);
    syncDrawer();
  }
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sidebarOpen()) setSidebar(false);
  });
  document.addEventListener("click", (event) => {
    if (!sidebarOpen()) return;
    if (!(event.target instanceof Node)) return;
    if (event.target.closest("#catalog-sidebar, .catalog-sidebar-toggle, .catalog-sidebar-close")) return;
    setSidebar(false);
  });
  const treeNav = document.querySelector("nav.catalog-tree");
  if (treeNav) {
    treeNav.addEventListener("click", (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const item = event.target instanceof Element
        ? event.target.closest(".catalog-tree-item[data-tree-toggle]")
        : null;
      if (!item || !treeNav.contains(item)) return;
      const branch = item.closest(".catalog-tree-branch");
      const children = branch ? branch.querySelector(":scope > .catalog-tree-children") : null;
      if (!children) return;
      if (children.hidden) {
        // First click opens the branch in place; a later click enters the node.
        children.hidden = false;
        branch.classList.add("is-expanded");
        item.setAttribute("aria-expanded", "true");
        event.preventDefault();
      } else if (event.target.closest(".catalog-tree-caret")) {
        // The caret alone always toggles; labels keep normal navigation.
        children.hidden = true;
        branch.classList.remove("is-expanded");
        item.setAttribute("aria-expanded", "false");
        event.preventDefault();
      }
    });
  }
  const itemList = document.querySelector("[data-item-list]");
  const itemTemplate = document.querySelector("[data-item-template]");
  const addItem = document.querySelector("[data-add-item]");
  if (itemList && itemTemplate && addItem) {
    addItem.addEventListener("click", () => {
      const node = itemTemplate.content.firstElementChild.cloneNode(true);
      itemList.appendChild(node);
    });
    itemList.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-item]");
      if (!button) return;
      const row = button.closest("[data-item-row]");
      if (!row) return;
      if (itemList.querySelectorAll("[data-item-row]").length === 1) {
        row.querySelectorAll("input").forEach((input) => {
          input.value = "";
        });
        return;
      }
      row.remove();
    });
  }
  const filter = document.querySelector("[data-catalog-filter]");
  const cards = [...document.querySelectorAll("[data-catalog-card]")];
  const empty = document.querySelector("[data-catalog-filter-empty]");
  if (filter && cards.length) {
    filter.addEventListener("input", () => {
      const query = filter.value.trim().toLowerCase();
      let visible = 0;
      cards.forEach((card) => {
        const match = !query || (card.dataset.searchText || "").includes(query);
        card.hidden = !match;
        if (match) visible += 1;
      });
      if (empty) empty.hidden = visible !== 0;
    });
  }
  const copy = document.documentElement.lang.startsWith("zh")
    ? { none: "暂无参考图片", series: "暂无图片", aria: "图片不可用" }
    : { none: "No reference image", series: "Image unavailable", aria: "Image unavailable" };
  const unavailable = (image) => {
    const frame = image.closest(".catalog-epc-asset-frame, .catalog-media-frame");
    const placeholder = document.createElement("div");
    if (frame?.classList.contains("catalog-epc-asset-frame")) {
      placeholder.className = "catalog-epc-asset-placeholder";
    } else if (frame?.classList.contains("catalog-media-frame")) {
      placeholder.className = "catalog-image-unavailable";
      placeholder.textContent = copy.none;
    } else if (image.closest(".catalog-series-thumbnail, .catalog-series-heading-visual")) {
      placeholder.className = image.closest(".catalog-series-thumbnail")
        ? "catalog-series-thumbnail catalog-series-thumbnail-empty"
        : "catalog-series-heading-placeholder";
      if (image.closest(".catalog-series-thumbnail")) placeholder.textContent = copy.series;
    } else {
      placeholder.className = image.classList.contains("part-thumb") ? "part-thumb placeholder" : "catalog-image-unavailable";
      placeholder.textContent = copy.none;
    }
    placeholder.setAttribute("role", "img");
    placeholder.setAttribute("aria-label", copy.aria);
    image.replaceWith(placeholder);
  };
  document.addEventListener("error", (event) => {
    const image = event.target;
    if (image instanceof HTMLImageElement && image.matches("[data-catalog-image]")) unavailable(image);
  }, true);

  const seriesSelect = document.querySelector("[data-shortcut-series]");
  const modelSelect = document.querySelector("[data-shortcut-model]");
  const seriesInput = document.querySelector("[data-shortcut-series-code]");
  const seriesSubmit = document.querySelector("[data-shortcut-series-submit]");
  if (seriesSelect && modelSelect) {
    const fillModels = (rows, current) => {
      modelSelect.querySelectorAll("option[value]:not([value=''])").forEach((option) => option.remove());
      rows.forEach((row) => {
        const option = document.createElement("option");
        option.value = row.model_code;
        option.textContent = `${row.display_model_name} · ${row.model_code}`;
        if (row.model_code === current) option.selected = true;
        modelSelect.append(option);
      });
    };
    const loadModels = async () => {
      const series = seriesSelect.value;
      const current = modelSelect.value;
      if (seriesInput) seriesInput.value = series;
      if (!series) {
        fillModels([], "");
        return;
      }
      try {
        const response = await fetch(`/ops/catalog-models?series_code=${encodeURIComponent(series)}`, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const rows = await response.json();
        if (!Array.isArray(rows)) return;
        fillModels(rows, current);
        if (seriesSubmit) seriesSubmit.hidden = true;
      } catch {
        // Keep the GET series form so models can still load without JS fetch.
      }
    };
    seriesSelect.addEventListener("change", () => {
      modelSelect.value = "";
      loadModels();
    });
    if (seriesSelect.value) loadModels();
  }

  const searchSeries = document.querySelector("[data-search-series]");
  const searchModel = document.querySelector("[data-search-model]");
  if (searchSeries && searchModel) {
    const emptyOption = () => searchModel.querySelector("option[value='']") || searchModel.querySelector("option");
    const setEmptyLabel = () => {
      const option = emptyOption();
      if (!option) return;
      option.textContent = searchSeries.value
        ? (searchModel.dataset.selectModel || "")
        : (searchModel.dataset.needSeries || "");
    };
    const fillSearchModels = async () => {
      const series = searchSeries.value;
      const current = searchModel.value;
      searchModel.querySelectorAll("option[value]:not([value=''])").forEach((option) => option.remove());
      searchModel.disabled = !series;
      setEmptyLabel();
      if (!series) {
        searchModel.value = "";
        return;
      }
      try {
        const response = await fetch(`/catalog/search-models?series_code=${encodeURIComponent(series)}`, {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const rows = await response.json();
        if (!Array.isArray(rows)) return;
        rows.forEach((row) => {
          const option = document.createElement("option");
          option.value = row.model_code;
          option.textContent = row.display_model_name;
          if (row.model_code === current) option.selected = true;
          searchModel.append(option);
        });
      } catch {
        // Keep server-rendered options when the helper request fails.
      }
    };
    searchSeries.addEventListener("change", () => {
      searchModel.value = "";
      fillSearchModels();
    });
    fillSearchModels();
  }

  const locators = [...document.querySelectorAll("[data-catalog-locator]")];
  if (locators.length) {
    const closeOthers = (current) => {
      locators.forEach((item) => {
        if (item !== current) item.open = false;
      });
    };
    locators.forEach((item) => {
      item.addEventListener("toggle", () => {
        if (item.open) closeOthers(item);
      });
      const filter = item.querySelector("[data-locator-filter]");
      const links = () => [...item.querySelectorAll("[data-search-text]")];
      if (filter) {
        filter.addEventListener("input", () => {
          const query = filter.value.trim().toLowerCase();
          links().forEach((link) => {
            link.hidden = Boolean(query) && !(link.dataset.searchText || "").includes(query);
          });
        });
      }
    });
    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Node)) return;
      if (event.target.closest("[data-catalog-locator]")) return;
      locators.forEach((item) => {
        item.open = false;
      });
    });
  }

  const zoomCopy = {
    open: document.documentElement.getAttribute("data-zoom-open") || "",
    close: document.documentElement.getAttribute("data-zoom-close") || "",
    hint: document.documentElement.getAttribute("data-zoom-hint") || "",
  };
  const zoomRoot = document.createElement("div");
  zoomRoot.className = "catalog-image-zoom";
  zoomRoot.hidden = true;
  zoomRoot.setAttribute("role", "dialog");
  zoomRoot.setAttribute("aria-modal", "true");
  zoomRoot.setAttribute("aria-label", zoomCopy.open);
  const zoomClose = document.createElement("button");
  zoomClose.type = "button";
  zoomClose.className = "catalog-image-zoom-close";
  zoomClose.setAttribute("aria-label", zoomCopy.close);
  zoomClose.textContent = "×";
  const zoomStage = document.createElement("div");
  zoomStage.className = "catalog-image-zoom-stage";
  const zoomImage = document.createElement("img");
  zoomImage.alt = "";
  zoomImage.draggable = false;
  const zoomHint = document.createElement("p");
  zoomHint.className = "catalog-image-zoom-hint";
  zoomHint.textContent = zoomCopy.hint;
  zoomStage.append(zoomImage);
  zoomRoot.append(zoomClose, zoomStage, zoomHint);
  document.body.append(zoomRoot);

  const zoomState = { scale: 1, x: 0, y: 0, min: 1, max: 8, drag: null, last: null, moved: false };
  const previewFrame = (image) => image.closest(".catalog-epc-asset-frame, .catalog-media-frame, .catalog-series-heading-visual, .maintenance-photo-row, .maintenance-existing-photo");
  const catalogImage = (target) => target instanceof HTMLImageElement && target.matches("[data-catalog-image]");
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
  const applyZoom = () => {
    zoomImage.style.transform = `translate(${zoomState.x}px, ${zoomState.y}px) scale(${zoomState.scale})`;
  };
  const fitZoom = () => {
    const width = zoomImage.naturalWidth;
    const height = zoomImage.naturalHeight;
    const stageWidth = zoomStage.clientWidth;
    const stageHeight = zoomStage.clientHeight;
    if (!width || !height || !stageWidth || !stageHeight) return;
    const fitted = Math.min((stageWidth - 64) / width, (stageHeight - 96) / height);
    zoomState.min = Math.max(fitted, 0.05);
    zoomState.max = Math.max(8, width / Math.max(stageWidth, 1) * 4);
    zoomState.scale = zoomState.min;
    zoomState.x = (stageWidth - width * zoomState.scale) / 2;
    zoomState.y = (stageHeight - height * zoomState.scale) / 2;
    applyZoom();
  };
  const zoomAt = (clientX, clientY, nextScale) => {
    const rect = zoomStage.getBoundingClientRect();
    const cursorX = clientX - rect.left;
    const cursorY = clientY - rect.top;
    const imageX = (cursorX - zoomState.x) / zoomState.scale;
    const imageY = (cursorY - zoomState.y) / zoomState.scale;
    zoomState.scale = clamp(nextScale, zoomState.min, zoomState.max);
    zoomState.x = cursorX - imageX * zoomState.scale;
    zoomState.y = cursorY - imageY * zoomState.scale;
    applyZoom();
  };
  const closeZoom = () => {
    if (!zoomRoot.classList.contains("is-open")) return;
    zoomRoot.classList.remove("is-open");
    zoomRoot.hidden = true;
    document.body.classList.remove("catalog-zoom-open");
    zoomImage.removeAttribute("src");
    zoomState.drag = null;
    zoomStage.classList.remove("is-dragging");
    if (zoomState.last instanceof HTMLElement) zoomState.last.focus({ preventScroll: true });
    zoomState.last = null;
  };
  const openZoom = (image) => {
    if (!image?.src) return;
    zoomState.last = document.activeElement;
    zoomImage.alt = image.alt || zoomCopy.open;
    zoomRoot.hidden = false;
    zoomRoot.classList.add("is-open");
    document.body.classList.add("catalog-zoom-open");
    const show = () => {
      fitZoom();
      zoomClose.focus({ preventScroll: true });
    };
    if (zoomImage.src === image.src && zoomImage.complete && zoomImage.naturalWidth) {
      show();
      return;
    }
    zoomImage.addEventListener("load", show, { once: true });
    zoomImage.src = image.src;
  };

  document.querySelectorAll("[data-catalog-image]").forEach((image) => {
    image.setAttribute("title", zoomCopy.open);
  });
  document.addEventListener("click", (event) => {
    const image = event.target;
    if (!catalogImage(image)) return;
    // Images inside a link stay clickable as links (series cards) unless they
    // sit in a dedicated preview frame, where a click opens the zoom viewer.
    if (!previewFrame(image)) return;
    event.preventDefault();
    event.stopPropagation();
    openZoom(image);
  }, true);
  document.addEventListener("dblclick", (event) => {
    const image = event.target;
    if (!catalogImage(image)) return;
    event.preventDefault();
    event.stopPropagation();
    openZoom(image);
  }, true);
  zoomClose.addEventListener("click", (event) => {
    event.preventDefault();
    closeZoom();
  });
  zoomRoot.addEventListener("click", (event) => {
    if (zoomState.moved) return;
    if (event.target === zoomRoot || event.target === zoomStage) closeZoom();
  });
  zoomStage.addEventListener("dblclick", (event) => {
    event.preventDefault();
    if (zoomState.scale > zoomState.min * 1.05) {
      fitZoom();
      return;
    }
    zoomAt(event.clientX, event.clientY, Math.min(zoomState.max, zoomState.min * 2.5));
  });
  zoomStage.addEventListener("wheel", (event) => {
    if (!zoomRoot.classList.contains("is-open")) return;
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    zoomAt(event.clientX, event.clientY, zoomState.scale * factor);
  }, { passive: false });
  zoomStage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    zoomState.drag = { id: event.pointerId, x: event.clientX, y: event.clientY, originX: zoomState.x, originY: zoomState.y };
    zoomState.moved = false;
    zoomStage.classList.add("is-dragging");
    zoomStage.setPointerCapture(event.pointerId);
  });
  zoomStage.addEventListener("pointermove", (event) => {
    if (!zoomState.drag || event.pointerId !== zoomState.drag.id) return;
    const dx = event.clientX - zoomState.drag.x;
    const dy = event.clientY - zoomState.drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) zoomState.moved = true;
    zoomState.x = zoomState.drag.originX + dx;
    zoomState.y = zoomState.drag.originY + dy;
    applyZoom();
  });
  const endDrag = (event) => {
    if (!zoomState.drag || event.pointerId !== zoomState.drag.id) return;
    zoomState.drag = null;
    zoomStage.classList.remove("is-dragging");
  };
  zoomStage.addEventListener("pointerup", endDrag);
  zoomStage.addEventListener("pointercancel", endDrag);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeZoom();
  });
  window.addEventListener("resize", () => {
    if (zoomRoot.classList.contains("is-open")) fitZoom();
  });
})();