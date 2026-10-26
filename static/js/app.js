document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".user-menu").forEach((menu) => {
    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { menu.open = false; menu.querySelector("summary")?.focus(); }
    });
  });

  document.querySelectorAll("[data-card-href]").forEach((card) => {
    const navigate = () => { window.location.href = card.dataset.cardHref; };
    card.addEventListener("click", (event) => {
      if (!event.target.closest("a, button, input, select, textarea, details, summary, form")) navigate();
    });
    card.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && event.target === card) { event.preventDefault(); navigate(); }
    });
  });

  document.querySelectorAll("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.dialogOpen);
      showStableDialog(target);
      const form = target?.querySelector("form[data-member-editor-form]");
      if (form && target) target.dataset.originalForm = new URLSearchParams(new FormData(form)).toString();
    });
  });
  const unsavedDialog = document.querySelector("[data-unsaved-dialog]");
  const askUnsaved = (message) => new Promise((resolve) => {
    if (!unsavedDialog) { resolve(window.confirm(message)); return; }
    unsavedDialog.querySelector("[data-unsaved-message]").textContent = message;
    const finish = (discard) => { unsavedDialog.close(); resolve(discard); };
    unsavedDialog.querySelector("[data-unsaved-keep]").onclick = () => finish(false);
    unsavedDialog.querySelector("[data-unsaved-discard]").onclick = () => finish(true);
    showStableDialog(unsavedDialog);
  });
  const memberDialogDirty = (dialog) => {
    const form = dialog?.querySelector("form[data-member-editor-form]");
    return Boolean(form && dialog.dataset.originalForm !== new URLSearchParams(new FormData(form)).toString());
  };
  const closeMemberDialog = async (dialog) => {
    if (memberDialogDirty(dialog) && !await askUnsaved("You have changes that haven't been saved.")) return;
    dialog.close();
  };
  document.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = button.closest("dialog");
      if (dialog?.matches("[data-member-dialog]")) closeMemberDialog(dialog);
      else dialog?.close();
    });
  });
  document.querySelectorAll("[data-member-cancel]").forEach((button) => {
    button.addEventListener("click", () => closeMemberDialog(button.closest("dialog")));
  });
  document.querySelectorAll("[data-member-dialog]").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeMemberDialog(dialog);
    });
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeMemberDialog(dialog);
    });
  });
  document.querySelectorAll("form[data-member-editor-form]").forEach((form) => {
    const permissions = form.querySelector("[data-admin-permissions]");
    const role = form.elements.role;
    if (!permissions || !role) return;
    const updatePermissionsVisibility = () => { permissions.hidden = role.value !== "admin"; };
    role.addEventListener("change", updatePermissionsVisibility);
    updatePermissionsVisibility();
  });

  document.querySelectorAll("input[data-invite-search]").forEach((input) => {
    const results = input.closest("form")?.querySelector("[data-invite-results]");
    let timer = null;
    input.addEventListener("input", () => {
      window.clearTimeout(timer);
      const query = input.value.trim();
      if (query.length < 2) { results.innerHTML = ""; return; }
      timer = window.setTimeout(async () => {
        try {
          const response = await fetch(`${input.dataset.inviteSearch}?q=${encodeURIComponent(query)}`, { headers: { Accept: "application/json" } });
          const payload = await response.json();
          results.innerHTML = payload.results.length ? payload.results.map((item) => {
            const label = item.state === "member" ? "Already a member" : item.state === "pending" ? "Already invited" : "Invite";
            const action = item.state === "available" ? `<button type="button" data-invite-username="${item.username}">${label}</button>` : `<span class="muted small">${label}</span>`;
            return `<div class="invite-search-result"><span>${item.username}</span>${action}</div>`;
          }).join("") : '<p class="muted small">No matching accounts.</p>';
          results.querySelectorAll("[data-invite-username]").forEach((button) => button.addEventListener("click", () => {
            input.value = button.dataset.inviteUsername;
            results.innerHTML = `<p class="muted small">Selected ${button.dataset.inviteUsername}. Send the invitation to continue.</p>`;
          }));
        } catch (_error) {
          results.innerHTML = '<p class="muted small">Search is temporarily unavailable.</p>';
        }
      }, 220);
    });
  });

  document.querySelectorAll("[data-password-toggle]").forEach((button) => {
    const input = button.closest(".password-control")?.querySelector("[data-password-input]");
    if (!input) return;
    button.addEventListener("click", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.textContent = showing ? "Show" : "Hide";
      button.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      button.setAttribute("aria-pressed", String(!showing));
      input.focus();
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const parseTime = (value) => {
    const compact = value.trim().toUpperCase().replace(/\s+/g, "");
    const match = compact.match(/^(\d{1,2})(?::?(\d{2}))?(AM|PM)$/);
    if (!match) return null;
    const hour = Number(match[1]);
    const minute = Number(match[2] || "0");
    if (hour < 1 || hour > 12 || minute > 59 || minute % 5 !== 0) return null;
    return (hour % 12) * 60 + minute + (match[3] === "PM" ? 720 : 0);
  };
  const formatTime = (minutes) => {
    const hour = Math.floor((minutes % 720) / 60) || 12;
    return `${String(hour).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")} ${minutes >= 720 ? "PM" : "AM"}`;
  };
  const normalizedTime = (value) => {
    const minutes = parseTime(value);
    return minutes === null ? "" : `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  };

  const dialog = document.querySelector("[data-time-picker-dialog]");
  const dialogHour = dialog?.querySelector("[data-picker-hour]");
  const dialogMinute = dialog?.querySelector("[data-picker-minute]");
  const dialogPeriod = dialog?.querySelector("[data-picker-period]");
  let activeTimeInput = null;
  let originalTimeValue = "";
  const pickerParent = dialog?.parentNode;
  const pickerNextSibling = dialog?.nextSibling;
  let embeddedPickerOwner = null;
  let pickerLayoutOwner = null;
  const setDialogTime = (minutes) => {
    dialogHour.value = String(Math.floor((minutes % 720) / 60) || 12);
    dialogMinute.value = String(minutes % 60).padStart(2, "0");
    dialogPeriod.value = minutes >= 720 ? "PM" : "AM";
  };
  const dialogMinutes = () => (Number(dialogHour.value) % 12) * 60 + Number(dialogMinute.value) + (dialogPeriod.value === "PM" ? 720 : 0);
  const openPicker = (input) => {
    if (!dialog) return;
    if (activeTimeInput === input) return;
    closePicker(false, false);
    activeTimeInput = input;
    originalTimeValue = input.value;
    setDialogTime(parseTime(input.value) ?? 540);
    const bounds = input.getBoundingClientRect();
    dialog.hidden = false;
    const availabilityOwner = input.closest(
      "[data-availability-window-dialog], [data-day-availability-dialog]",
    );
    if (availabilityOwner) {
      const activeDayCard = input.closest(".day-window-card-active");
      const pickerOwner = activeDayCard || availabilityOwner;
      pickerOwner.appendChild(dialog);
      embeddedPickerOwner = pickerOwner;
      if (activeDayCard) {
        activeDayCard.classList.add("time-picker-open");
        pickerLayoutOwner = activeDayCard;
      }
      dialog.style.position = "absolute";
      dialog.style.width = `${Math.max(280, bounds.width)}px`;
      const ownerBounds = pickerOwner.getBoundingClientRect();
      const left = Math.max(0, Math.min(
        bounds.left - ownerBounds.left,
        pickerOwner.clientWidth - dialog.offsetWidth,
      ));
      dialog.style.left = `${left}px`;
      dialog.style.top = `${bounds.bottom - ownerBounds.top + 8}px`;
    }
    if (!availabilityOwner) {
      dialog.style.width = `${Math.max(280, bounds.width)}px`;
      dialog.style.left = `${Math.max(8, Math.min(bounds.left, window.innerWidth - dialog.offsetWidth - 8))}px`;
      const spaceBelow = window.innerHeight - bounds.bottom;
      if (spaceBelow >= dialog.offsetHeight + 12) {
        dialog.style.top = `${bounds.bottom + 6}px`;
      } else {
        dialog.style.top = `${Math.max(8, bounds.top - dialog.offsetHeight - 6)}px`;
      }
    }
  };
  const closePicker = (accept, restoreOriginal = true) => {
    if (!activeTimeInput || !dialog) return;
    if (accept) {
      activeTimeInput.value = formatTime(dialogMinutes());
      activeTimeInput.setCustomValidity("");
      activeTimeInput.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (restoreOriginal) {
      activeTimeInput.value = originalTimeValue;
    }
    dialog.hidden = true;
    pickerLayoutOwner?.classList.remove("time-picker-open");
    pickerLayoutOwner = null;
    if (embeddedPickerOwner && pickerParent) {
      if (pickerNextSibling && pickerNextSibling.parentNode === pickerParent) pickerParent.insertBefore(dialog, pickerNextSibling);
      else pickerParent.appendChild(dialog);
      embeddedPickerOwner = null;
      dialog.style.position = "";
      dialog.style.width = "";
      dialog.style.left = "";
      dialog.style.top = "";
    }
    activeTimeInput = null;
    originalTimeValue = "";
  };
  const bindUnifiedTimeInput = (input) => {
    if (!input || input.dataset.timeBound) return;
    input.dataset.timeBound = "true";
    input.addEventListener("focus", () => openPicker(input));
    input.addEventListener("click", () => openPicker(input));
    input.addEventListener("input", () => {
      const minutes = parseTime(input.value);
      if (minutes !== null && activeTimeInput === input) setDialogTime(minutes);
    });
    input.addEventListener("blur", () => {
      if (dialog && !dialog.hidden && dialog.contains(document.activeElement)) return;
      if (!input.value.trim()) return;
      const minutes = parseTime(input.value);
      if (minutes === null) {
        input.setCustomValidity("Please enter a time in 5-minute increments.");
      } else {
        input.setCustomValidity("");
        input.value = formatTime(minutes);
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const minutes = parseTime(input.value);
        if (minutes !== null) { setDialogTime(minutes); closePicker(true); }
      }
      if (event.key === "Escape") closePicker(false);
      if (event.key === "ArrowDown" && event.altKey) {
        event.preventDefault();
        openPicker(input);
      }
    });
  };
  document.querySelectorAll("[data-unified-time]").forEach(bindUnifiedTimeInput);
  dialog?.querySelector("[data-picker-ok]").addEventListener("click", () => closePicker(true));
  dialog?.querySelector("[data-picker-cancel]").addEventListener("click", () => closePicker(false));
  dialog?.querySelectorAll("select").forEach((select) => select.addEventListener("change", () => {
    if (activeTimeInput) {
      activeTimeInput.value = formatTime(dialogMinutes());
      activeTimeInput.dispatchEvent(new Event("input", {bubbles: true}));
    }
  }));
  document.addEventListener("pointerdown", (event) => {
    if (activeTimeInput && !dialog.contains(event.target) && event.target !== activeTimeInput) closePicker(true);
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePicker(false); });

  document.querySelectorAll("[data-rsvp-form]").forEach((form) => {
    const status = form.querySelector("[data-rsvp-status]");
    const late = form.querySelector("[data-rsvp-arriving-late]");
    const early = form.querySelector("[data-rsvp-leaving-early]");
    const arrivalField = form.querySelector("[data-rsvp-arrival-field]");
    const departureField = form.querySelector("[data-rsvp-departure-field]");
    const arrival = arrivalField.querySelector("[data-unified-time]");
    const departure = departureField.querySelector("[data-unified-time]");
    const modifiers = form.querySelector("[data-rsvp-modifiers]");
    const updateFields = () => {
      const attending = status.value === "attending";
      modifiers.hidden = !attending;
      arrivalField.hidden = !attending || !late.checked;
      departureField.hidden = !attending || !early.checked;
      if (!attending || !late.checked) arrival.value = "";
      if (!attending || !early.checked) departure.value = "";
      if (status.value === "pending") { late.checked = false; early.checked = false; }
    };
    late.addEventListener("change", () => {
      if (late.checked && !arrival.value) arrival.value = form.dataset.arrivalDefault;
      updateFields();
    });
    early.addEventListener("change", () => {
      if (early.checked && !departure.value) departure.value = form.dataset.departureDefault;
      updateFields();
    });
    status.addEventListener("change", updateFields);
    updateFields();
  });

  const availabilityMinutes = (value) => {
    if (!value) return null;
    const parts = value.split(":").map(Number);
    return parts.length === 2 && parts.every(Number.isFinite) ? parts[0] * 60 + parts[1] : null;
  };
  const restorePageScroll = (left, top) => {
    const root = document.documentElement;
    const previousBehavior = root.style.scrollBehavior;
    root.style.scrollBehavior = "auto";
    window.scrollTo(left, top);
    root.style.scrollBehavior = previousBehavior;
  };
  const keepPagePosition = (left, top) => {
    restorePageScroll(left, top);
    window.requestAnimationFrame(() => {
      restorePageScroll(left, top);
      window.requestAnimationFrame(() => restorePageScroll(left, top));
    });
  };
  const openModalDialogs = new Set();
  let modalScrollState = null;
  const registerModal = (target) => {
    if (!target || openModalDialogs.has(target)) return;
    if (!openModalDialogs.size) {
      modalScrollState = {
        left: window.scrollX, top: window.scrollY,
        body: {
          position: document.body.style.position, top: document.body.style.top,
          left: document.body.style.left, width: document.body.style.width,
          overflow: document.body.style.overflow,
        },
      };
      document.body.style.position = "fixed";
      document.body.style.top = `${-modalScrollState.top}px`;
      document.body.style.left = `${-modalScrollState.left}px`;
      document.body.style.width = "100%";
      document.body.style.overflow = "hidden";
    }
    openModalDialogs.add(target);
    if (target.dataset.modalScrollBound) return;
    target.dataset.modalScrollBound = "true";
    target.addEventListener("close", () => {
      openModalDialogs.delete(target);
      if (!openModalDialogs.size && modalScrollState) {
        const saved = modalScrollState;
        Object.assign(document.body.style, saved.body);
        modalScrollState = null;
        keepPagePosition(saved.left, saved.top);
      }
    });
  };
  const showStableDialog = (target) => {
    registerModal(target);
    target.showModal();
  };
  const closeStableDialog = (target) => {
    if (target.open) target.close();
  };
  const availabilityTimeText = (value) => {
    const minutes = availabilityMinutes(value);
    return minutes === null ? value : formatTime(minutes);
  };
  const availabilityColumnForDay = (day) => document.querySelector(
    `[data-availability-calendar-day="${day}"]`,
  );
  const availabilityBlocksForDay = (day) => {
    const column = availabilityColumnForDay(day);
    return column ? Array.from(column.querySelectorAll(
      "[data-availability-window], [data-availability-window-readonly]",
    )) : [];
  };
  const refreshAvailabilitySummary = (day) => {
    const summary = document.querySelector(`[data-summary-day="${day}"] [data-summary-windows]`);
    if (!summary) return;
    summary.replaceChildren();
    availabilityBlocksForDay(day).forEach((block) => {
      const item = document.createElement("p");
      item.dataset.summaryEntryId = block.dataset.entryId;
      item.textContent = `${availabilityTimeText(block.dataset.startTime)} – ${availabilityTimeText(block.dataset.endTime)}`;
      summary.appendChild(item);
    });
    if (!summary.children.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.dataset.summaryEmpty = "true";
      empty.textContent = "No availability";
      summary.appendChild(empty);
    }
  };
  const updateAvailabilityBlock = (entry) => {
    document.querySelectorAll(`[data-entry-id="${entry.id}"]`).forEach((block) => {
      block.dataset.day = entry.day;
      block.dataset.startTime = availabilityTimeText(entry.start_time);
      block.dataset.endTime = availabilityTimeText(entry.end_time);
      const start = availabilityMinutes(entry.start_time);
      const end = availabilityMinutes(entry.end_time);
      if (start !== null && end !== null) {
        block.style.top = `${(start / 1440) * 100}%`;
        block.style.height = `${((end - start) / 1440) * 100}%`;
      }
      const label = block.querySelector(".availability-window-label");
      if (label) label.textContent = `${availabilityTimeText(entry.start_time)} – ${availabilityTimeText(entry.end_time)}`;
      block.title = `${availabilityTimeText(entry.start_time)} - ${availabilityTimeText(entry.end_time)}`;
      if (block.matches("[data-availability-window]")) {
        block.setAttribute("aria-label", `${entry.day} availability, ${availabilityTimeText(entry.start_time)} to ${availabilityTimeText(entry.end_time)}. Edit availability.`);
      }
    });
    refreshAvailabilitySummary(entry.day);
  };
  const removeAvailabilityBlock = (id, day) => {
    document.querySelectorAll(`[data-entry-id="${id}"]`).forEach((block) => block.remove());
    refreshAvailabilitySummary(day);
  };
  const makeAvailabilityBlock = (entry) => {
    const track = document.querySelector(`[data-availability-calendar-day="${entry.day}"] .calendar-day-track`);
    if (!track) return;
    const block = document.createElement("button");
    block.type = "button";
    block.className = "calendar-window availability-window-block";
    block.dataset.availabilityWindow = "true";
    block.dataset.entryId = entry.id;
    block.dataset.day = entry.day;
    block.dataset.startTime = availabilityTimeText(entry.start_time);
    block.dataset.endTime = availabilityTimeText(entry.end_time);
    block.title = `${availabilityTimeText(entry.start_time)} - ${availabilityTimeText(entry.end_time)}`;
    block.setAttribute("aria-label", `${entry.day} availability, ${block.dataset.startTime} to ${block.dataset.endTime}. Edit availability.`);
    block.style.top = `${(availabilityMinutes(entry.start_time) / 1440) * 100}%`;
    block.style.height = `${((availabilityMinutes(entry.end_time) - availabilityMinutes(entry.start_time)) / 1440) * 100}%`;
    const label = document.createElement("span");
    label.className = "availability-window-label";
    label.textContent = `${block.dataset.startTime} – ${block.dataset.endTime}`;
    block.appendChild(label);
    track.appendChild(block);
    return block;
  };

  document.querySelectorAll("[data-availability-editor]").forEach((editor) => {
    const tabs = editor.querySelectorAll("[data-availability-day]");
    const panels = editor.querySelectorAll("[data-availability-panel]");
    let draftTimes = null;
    const activateDay = (day) => {
      const visiblePanel = Array.from(panels).find((panel) => !panel.classList.contains("is-hidden"));
      if (visiblePanel) {
        draftTimes = {
          start: visiblePanel.querySelector('[name="start_time"]').value,
          end: visiblePanel.querySelector('[name="end_time"]').value,
        };
      }
      tabs.forEach((tab) => {
        const active = tab.dataset.availabilityDay === day;
        tab.classList.toggle("is-active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      panels.forEach((panel) => panel.classList.toggle("is-hidden", panel.dataset.availabilityPanel !== day));
      const selectedPanel = Array.from(panels).find((panel) => panel.dataset.availabilityPanel === day);
      if (draftTimes && selectedPanel) {
        selectedPanel.querySelector('[name="start_time"]').value = draftTimes.start;
        selectedPanel.querySelector('[name="end_time"]').value = draftTimes.end;
      }
    };
    tabs.forEach((tab) => tab.addEventListener("click", () => activateDay(tab.dataset.availabilityDay)));
    editor.querySelectorAll("[data-availability-add-form]").forEach((form) => {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const start = form.elements.start_time;
        const end = form.elements.end_time;
        const startMinutes = parseTime(start.value);
        const endMinutes = parseTime(end.value);
        if (startMinutes === null || endMinutes === null) {
          (startMinutes === null ? start : end).setCustomValidity("Please enter a time in 5-minute increments.");
          (startMinutes === null ? start : end).reportValidity();
          return;
        }
        start.value = formatTime(startMinutes);
        end.value = formatTime(endMinutes);
        if (endMinutes <= startMinutes) {
          end.setCustomValidity("End time must be later than start time.");
          end.reportValidity();
          end.setCustomValidity("");
          return;
        }
        start.value = formatTime(startMinutes);
        end.value = formatTime(endMinutes);
        const message = form.querySelector("[data-availability-form-message]");
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        try {
          const response = await fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
          });
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.error || "Availability could not be added.");
          makeAvailabilityBlock(payload.entry);
          refreshAvailabilitySummary(payload.entry.day);
          if (message) { message.textContent = "Availability window added."; message.classList.add("success-text"); }
        } catch (error) {
          if (message) { message.textContent = error.message; message.classList.remove("success-text"); }
        } finally {
          button.disabled = false;
        }
      });
    });
    const current = document.querySelector("[data-current-availability]");
    if (!current) return;
    const readOnly = current.querySelector("[data-availability-readonly]");
    const bulkForm = current.querySelector("[data-availability-bulk]");
    const edit = current.querySelector("[data-availability-edit]");
    const cancel = current.querySelector("[data-availability-cancel]");
    const save = current.querySelector("[data-availability-save]");
    const editMode = (enabled) => {
      readOnly.hidden = enabled; bulkForm.hidden = !enabled;
      edit.hidden = enabled; cancel.hidden = !enabled; save.hidden = !enabled;
    };
    edit.addEventListener("click", () => editMode(true));
    let dirty = false;
    const markDirty = () => { dirty = true; };
    bulkForm.addEventListener("input", markDirty);
    bulkForm.addEventListener("change", markDirty);
    cancel.addEventListener("click", async () => {
      if (dirty && !await askUnsaved("You have unsaved availability changes. Discard them?")) return;
      window.location.reload();
    });
    current.querySelectorAll("[data-remove-availability]").forEach((button) => button.addEventListener("click", () => {
      const row = button.closest("[data-availability-entry]"); row.dataset.deleted = "true"; row.hidden = true; dirty = true;
    }));
    save.addEventListener("click", () => {
      const changes = Array.from(bulkForm.querySelectorAll("[data-availability-entry]")).map((row) => ({
        id: row.dataset.entryId,
        deleted: row.dataset.deleted === "true",
        start_time: normalizedTime(row.querySelector('[name="start_time"]').value),
        end_time: normalizedTime(row.querySelector('[name="end_time"]').value),
      }));
      if (changes.some((change) => !change.deleted && (!change.start_time || !change.end_time))) {
        window.alert("Please enter all times in 5-minute increments before saving."); return;
      }
      bulkForm.querySelector("[data-availability-payload]").value = JSON.stringify(changes);
      dirty = false;
      bulkForm.requestSubmit();
    });
  });

  const windowDialog = document.querySelector("[data-availability-window-dialog]");
  const windowForm = windowDialog?.querySelector("[data-availability-window-form]");
  const windowStart = windowForm?.querySelector("[data-window-start]");
  const windowEnd = windowForm?.querySelector("[data-window-end]");
  const closeWindowDialog = () => {
    if (!windowDialog) return;
    closePicker(false);
    closeStableDialog(windowDialog);
  };
  const openWindowDialog = (block) => {
    if (!windowDialog || !windowForm) return;
    closeWindowDialog();
    windowForm.action = windowDialog.dataset.editUrlTemplate.replace("ENTRY_ID", block.dataset.entryId);
    windowForm.querySelector("[data-window-day]").value = block.dataset.day;
    windowStart.value = block.dataset.startTime;
    windowEnd.value = block.dataset.endTime;
    windowStart.setCustomValidity("");
    windowEnd.setCustomValidity("");
    windowDialog.dataset.entryId = block.dataset.entryId;
    windowDialog.dataset.entryDay = block.dataset.day;
    windowDialog.querySelector("#availability-window-title").textContent = `Edit Availability · ${block.dataset.day}`;
    showStableDialog(windowDialog);
  };
  const weeklyCalendar = document.querySelector(".weekly-calendar");
  weeklyCalendar?.addEventListener("click", (event) => {
    const block = event.target.closest("[data-availability-window]");
    if (block) {
      event.stopPropagation();
      openWindowDialog(block);
      return;
    }
    const column = event.target.closest("[data-availability-calendar-day]");
    if (column) openDayDialog(column);
  });
  weeklyCalendar?.addEventListener("keydown", (event) => {
    const column = event.target.closest("[data-availability-calendar-day]");
    if (!column || event.target.closest("[data-availability-window]")) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDayDialog(column);
    }
  });
  windowDialog?.querySelector("[data-window-dialog-close]")?.addEventListener("click", closeWindowDialog);
  windowDialog?.querySelector("[data-window-dialog-cancel]")?.addEventListener("click", closeWindowDialog);
  windowDialog?.querySelector("[data-window-dialog-delete]")?.addEventListener("click", async () => {
    const id = windowDialog.dataset.entryId;
    if (!id || !window.confirm("Delete this availability window?")) return;
    const response = await fetch(windowDialog.dataset.deleteUrlTemplate.replace("ENTRY_ID", id), {
      method: "POST", headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
      body: new URLSearchParams({return_to: windowForm.querySelector('[name="return_to"]').value}),
    });
    if (!response.ok) return;
    removeAvailabilityBlock(id, windowDialog.dataset.entryDay);
    closeWindowDialog();
  });
  dialog?.addEventListener("click", (event) => event.stopPropagation());
  dialog?.addEventListener("pointerdown", (event) => event.stopPropagation());
  windowDialog?.addEventListener("pointerdown", (event) => {
    if (activeTimeInput && embeddedPickerOwner === windowDialog
      && !dialog.contains(event.target) && event.target !== activeTimeInput) closePicker(true);
    if (event.target === windowDialog) { closeWindowDialog(); return; }
    event.stopPropagation();
  });
  windowDialog?.addEventListener("click", (event) => {
    if (event.target === windowDialog) closeWindowDialog(); else event.stopPropagation();
  });
  windowDialog?.addEventListener("cancel", (event) => event.preventDefault());
  windowForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const startMinutes = parseTime(windowStart.value);
    const endMinutes = parseTime(windowEnd.value);
    if (startMinutes === null || endMinutes === null || endMinutes <= startMinutes) {
      const invalid = startMinutes === null ? windowStart : windowEnd;
      invalid.setCustomValidity(endMinutes <= startMinutes ? "End time must be later than start time." : "Please enter a time in 5-minute increments.");
      invalid.reportValidity(); invalid.setCustomValidity(""); return;
    }
    windowStart.value = formatTime(startMinutes); windowEnd.value = formatTime(endMinutes);
    const response = await fetch(windowForm.action, {
      method: "POST", body: new FormData(windowForm),
      headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    });
    if (!response.ok) return;
    const payload = await response.json();
    updateAvailabilityBlock(payload.entry);
    closeWindowDialog();
  });

  const dayDialog = document.querySelector("[data-day-availability-dialog]");
  const dayReadonly = dayDialog?.querySelector("[data-day-readonly]");
  const dayCancelButton = dayDialog?.querySelector("[data-day-dialog-cancel]");
  const daySaveButton = dayDialog?.querySelector("[data-day-dialog-save]");
  const dayDeleteDialog = document.querySelector("[data-day-delete-dialog]");
  const dayDeleteMessage = dayDeleteDialog?.querySelector("[data-day-delete-message]");
  const dayDeleteConfirm = dayDeleteDialog?.querySelector("[data-day-delete-confirm]");
  const dayDeleteCancelButtons = dayDeleteDialog?.querySelectorAll("[data-day-delete-cancel]");
  const canEditDay = dayDialog?.dataset.editable === "true";
  let activeDay = null;
  let activeDayColumn = null;
  let persistedWindows = new Map();
  let stagedWindows = new Map();
  let activeWindowId = null;
  let pendingDeleteId = null;
  const dayEntries = () => Array.from(activeDayColumn?.querySelectorAll(
    "[data-availability-window], [data-availability-window-readonly]",
  ) || []).map((block) => ({
    id: block.dataset.entryId,
    day: activeDay,
    start_time: normalizedTime(block.dataset.startTime),
    end_time: normalizedTime(block.dataset.endTime),
  }));
  const stagedTimeText = (value) => {
    const canonicalMinutes = availabilityMinutes(value);
    if (canonicalMinutes !== null) return formatTime(canonicalMinutes);
    const normalized = normalizedTime(value);
    return normalized ? availabilityTimeText(normalized) : value;
  };
  const resetDayStaging = () => {
    persistedWindows = new Map(dayEntries().map((entry) => [entry.id, {...entry}]));
    stagedWindows = new Map(Array.from(persistedWindows, ([id, entry]) => [id, {...entry}]));
    activeWindowId = null;
  };
  const windowIsDirty = (id) => {
    const persisted = persistedWindows.get(id);
    const staged = stagedWindows.get(id);
    if (!persisted || !staged) return false;
    return (normalizedTime(staged.start_time) || staged.start_time.trim()) !== persisted.start_time
      || (normalizedTime(staged.end_time) || staged.end_time.trim()) !== persisted.end_time;
  };
  const hasStagedChanges = () => (
    Array.from(persistedWindows.keys()).some((id) => !stagedWindows.has(id))
    || Array.from(stagedWindows.keys()).some(windowIsDirty)
  );
  const updateDayActions = () => {
    const dirty = hasStagedChanges();
    dayCancelButton.hidden = !dirty;
    daySaveButton.hidden = !dirty;
  };
  const makeDayWindowCard = (entry, {editable = false, dirty = false} = {}) => {
    const card = document.createElement(editable ? "button" : "article");
    card.className = "day-window-card";
    if (editable) {
      card.type = "button";
      card.dataset.dayWindowSelect = entry.id;
      card.setAttribute("aria-label", `Edit ${stagedTimeText(entry.start_time)} to ${stagedTimeText(entry.end_time)}`);
    }
    const line = document.createElement("span");
    line.className = "day-window-time";
    line.textContent = `${stagedTimeText(entry.start_time)} – ${stagedTimeText(entry.end_time)}`;
    card.appendChild(line);
    if (editable) {
      const icon = document.createElement("span");
      icon.className = "day-window-edit-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "✎";
      card.appendChild(icon);
    }
    if (dirty) {
      const status = document.createElement("em");
      status.className = "day-window-unsaved";
      status.textContent = "Unsaved changes";
      card.appendChild(status);
    }
    if (editable) {
      const remove = document.createElement("span");
      remove.className = "day-window-delete";
      remove.dataset.dayWindowDelete = entry.id;
      remove.setAttribute("role", "button");
      remove.setAttribute("tabindex", "0");
      remove.setAttribute("aria-label", `Delete ${stagedTimeText(entry.start_time)} to ${stagedTimeText(entry.end_time)}`);
      remove.textContent = "×";
      card.appendChild(remove);
    }
    return card;
  };
  const renderDayWindows = () => {
    dayReadonly.replaceChildren();
    const entries = Array.from(stagedWindows.values());
    if (!entries.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = `No availability windows for ${activeDay}.`; dayReadonly.appendChild(empty); }
    entries.forEach((entry) => {
      if (entry.id === activeWindowId) {
        const card = document.createElement("article");
        card.className = "day-window-card day-window-card-active";
        card.dataset.activeDayWindow = entry.id;
        const fields = document.createElement("div");
        fields.className = "day-window-fields";
        fields.append(makeDayTimeInput("start_time", "Start Time", entry.start_time, entry.id), makeDayTimeInput("end_time", "End Time", entry.end_time, entry.id));
        card.appendChild(fields);
        const remove = document.createElement("span");
        remove.className = "day-window-delete";
        remove.dataset.dayWindowDelete = entry.id;
        remove.setAttribute("role", "button");
        remove.setAttribute("tabindex", "0");
        remove.setAttribute("aria-label", `Delete ${stagedTimeText(entry.start_time)} to ${stagedTimeText(entry.end_time)}`);
        remove.textContent = "×";
        card.appendChild(remove);
        dayReadonly.appendChild(card);
      } else {
        dayReadonly.appendChild(makeDayWindowCard(entry, {editable: canEditDay, dirty: windowIsDirty(entry.id)}));
      }
    });
    updateDayActions();
  };
  const makeDayTimeInput = (name, label, value, entryId) => {
    const field = document.createElement("label");
    field.className = "unified-time-input";
    const caption = document.createElement("span");
    caption.textContent = label;
    const control = document.createElement("span");
    control.className = "unified-time-control";
    const input = document.createElement("input");
    input.name = name;
    input.type = "text";
    input.value = stagedTimeText(value);
    input.inputMode = "text";
    input.autocomplete = "off";
    input.dataset.unifiedTime = "true";
    control.appendChild(input);
    field.append(caption, control);
    bindUnifiedTimeInput(input);
    input.addEventListener("input", () => {
      const staged = stagedWindows.get(entryId);
      if (staged) {
        staged[name] = input.value;
        updateDayActions();
      }
    });
    return field;
  };
  const captureActiveWindow = () => {
    if (!activeWindowId) return;
    if (activeTimeInput && dayDialog.contains(activeTimeInput)) closePicker(true);
    const row = dayReadonly.querySelector(`[data-active-day-window="${activeWindowId}"]`);
    const staged = stagedWindows.get(activeWindowId);
    if (!row || !staged) return;
    staged.start_time = row.querySelector('[name="start_time"]').value;
    staged.end_time = row.querySelector('[name="end_time"]').value;
  };
  const openDayDialog = (column) => {
    if (!dayDialog) return;
    activeDayColumn = column;
    activeDay = column.dataset.day;
    dayDialog.querySelector("#day-availability-title").textContent = `${activeDay} Availability`;
    resetDayStaging(); renderDayWindows(); showStableDialog(dayDialog);
  };
  const requestCloseDayDialog = async () => {
    captureActiveWindow();
    if (hasStagedChanges()
      && !await askUnsaved("You have availability changes that haven't been saved.")) return;
    closePicker(false);
    resetDayStaging();
    closeStableDialog(dayDialog);
  };
  dayReadonly?.addEventListener("click", (event) => {
    if (!canEditDay) return;
    const remove = event.target.closest("[data-day-window-delete]");
    if (remove) {
      event.preventDefault();
      event.stopPropagation();
      const entry = stagedWindows.get(remove.dataset.dayWindowDelete);
      if (!entry) return;
      pendingDeleteId = entry.id;
      dayDeleteMessage.textContent = `${stagedTimeText(entry.start_time)} – ${stagedTimeText(entry.end_time)}\n\nAre you sure you want to remove this availability window?`;
      showStableDialog(dayDeleteDialog);
      return;
    }
    const selected = event.target.closest("[data-day-window-select]");
    if (!selected) return;
    captureActiveWindow();
    activeWindowId = selected.dataset.dayWindowSelect;
    renderDayWindows();
  });
  dayReadonly?.addEventListener("keydown", (event) => {
    const remove = event.target.closest("[data-day-window-delete]");
    if (remove && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      remove.click();
    }
  });
  dayDeleteCancelButtons?.forEach((button) => button.addEventListener("click", () => {
    pendingDeleteId = null;
    closeStableDialog(dayDeleteDialog);
  }));
  dayDeleteDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    pendingDeleteId = null;
    closeStableDialog(dayDeleteDialog);
  });
  dayDeleteDialog?.addEventListener("click", (event) => {
    if (event.target === dayDeleteDialog) {
      pendingDeleteId = null;
      closeStableDialog(dayDeleteDialog);
    }
  });
  dayDeleteConfirm?.addEventListener("click", () => {
    if (!pendingDeleteId) return;
    captureActiveWindow();
    stagedWindows.delete(pendingDeleteId);
    if (activeWindowId === pendingDeleteId) activeWindowId = null;
    pendingDeleteId = null;
    closeStableDialog(dayDeleteDialog);
    renderDayWindows();
  });
  dayCancelButton?.addEventListener("click", () => {
    closePicker(false); resetDayStaging(); renderDayWindows();
  });
  dayDialog?.querySelector("[data-day-dialog-close]")?.addEventListener("click", requestCloseDayDialog);
  dayDialog?.addEventListener("pointerdown", (event) => {
    const bounds = dayDialog.getBoundingClientRect();
    const insideDialog = event.clientX >= bounds.left && event.clientX <= bounds.right
      && event.clientY >= bounds.top && event.clientY <= bounds.bottom;
    if (!insideDialog) return;
    if (activeTimeInput && !dialog.contains(event.target) && event.target !== activeTimeInput) {
      closePicker(true);
    }
    event.stopPropagation();
  });
  dayDialog?.addEventListener("click", (event) => {
    if (event.target !== dayDialog) return;
    const bounds = dayDialog.getBoundingClientRect();
    const insideDialog = event.clientX >= bounds.left && event.clientX <= bounds.right
      && event.clientY >= bounds.top && event.clientY <= bounds.bottom;
    if (!insideDialog) requestCloseDayDialog();
  });
  dayDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    requestCloseDayDialog();
  });
  daySaveButton?.addEventListener("click", async () => {
    captureActiveWindow();
    const changes = Array.from(stagedWindows.values()).filter((entry) => windowIsDirty(entry.id)).map((entry) => ({
      id: entry.id, deleted: false,
      start_time: normalizedTime(entry.start_time),
      end_time: normalizedTime(entry.end_time),
    })).concat(Array.from(persistedWindows.keys()).filter((id) => !stagedWindows.has(id)).map((id) => ({
      id, deleted: true, start_time: "", end_time: "",
    })));
    const invalid = changes.find((change) => !change.deleted && (!change.start_time || !change.end_time || change.end_time <= change.start_time));
    const message = dayDialog.querySelector("[data-day-message]");
    if (invalid) { message.textContent = "Enter valid five-minute times with the end after the start."; return; }
    if (!changes.length) { updateDayActions(); return; }
    const action = document.querySelector("[data-availability-bulk]").action;
    daySaveButton.disabled = true;
    try {
      const response = await fetch(action, {method: "POST", headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}, body: new URLSearchParams({availability_payload: JSON.stringify(changes)})});
      if (!response.ok) { const payload = await response.json(); message.textContent = payload.error || "Availability could not be saved."; return; }
      changes.forEach((change) => {
        if (change.deleted) removeAvailabilityBlock(change.id, activeDay);
        else updateAvailabilityBlock({...change, day: activeDay});
      });
      resetDayStaging(); renderDayWindows();
    } finally {
      daySaveButton.disabled = false;
    }
  });

  const planForm = document.querySelector("form[data-schedule-preview-url]");
  if (!planForm) return;
  const message = planForm.querySelector("[data-preview-message]");
  const resultsContainer = planForm.querySelector("[data-preview-results]");
  const watchedNames = ["start_date", "end_date", "duration_minutes", "minimum_attendance", "earliest_time", "latest_time"];
  let timer; let requestNumber = 0;
  const renderResults = (results) => {
    resultsContainer.replaceChildren();
    if (!results.length) { message.textContent = "No options currently meet these requirements."; return; }
    message.textContent = "Preview only — save the plan before scheduling a time.";
    results.slice(0, 5).forEach((result, index) => {
      const card = document.createElement("article"); card.className = "preview-result";
      const title = document.createElement("strong"); title.textContent = `${result.day}, ${result.date}`;
      const time = document.createElement("span"); time.textContent = `${formatTime(Number(result.start.slice(0, 2)) * 60 + Number(result.start.slice(3))) } – ${formatTime(Number(result.end.slice(0, 2)) * 60 + Number(result.end.slice(3)))}`;
      const attendance = document.createElement("span"); attendance.textContent = `${result.attendance}/${result.group_size} available`;
      const explanation = document.createElement("span");
      const unavailable = result.group_size - result.attendance;
      explanation.textContent = unavailable ? `${unavailable} unavailable` : "Everyone available";
      const badge = document.createElement("span"); badge.className = result.exact_match ? "preview-badge" : "preview-badge alternative"; badge.textContent = index === 0 ? "Best Match" : result.exact_match ? "Exact match" : `Shorter option · ${result.available_duration_minutes} min`;
      card.append(title, time, attendance, explanation, badge); resultsContainer.append(card);
    });
  };
  const requestPreview = async () => {
    const required = ["start_date", "end_date", "duration_minutes", "minimum_attendance"];
    if (required.some((name) => !planForm.elements[name]?.value)) { resultsContainer.replaceChildren(); message.textContent = "Complete the scheduling fields to preview matching times."; return; }
    const thisRequest = ++requestNumber; message.textContent = "Calculating best times…";
    try {
      const response = await fetch(planForm.dataset.schedulePreviewUrl, { method: "POST", body: new URLSearchParams(new FormData(planForm)), headers: { "X-Requested-With": "fetch" } });
      const data = await response.json(); if (thisRequest !== requestNumber) return;
      if (!data.success) { resultsContainer.replaceChildren(); message.textContent = data.errors?.[0] || "Enter valid scheduling requirements."; return; }
      renderResults(data.results);
    } catch (_error) { if (thisRequest === requestNumber) { resultsContainer.replaceChildren(); message.textContent = "Preview is temporarily unavailable. You can still save the plan."; } }
  };
  watchedNames.forEach((name) => { const field = planForm.elements[name]; if (field) field.addEventListener("input", () => { window.clearTimeout(timer); timer = window.setTimeout(requestPreview, 400); }); });
  requestPreview();
});
