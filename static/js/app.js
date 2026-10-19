document.addEventListener("DOMContentLoaded", () => {
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
    dialog.style.width = `${Math.max(280, bounds.width)}px`;
    dialog.style.left = `${Math.max(8, Math.min(bounds.left, window.innerWidth - dialog.offsetWidth - 8))}px`;
    const spaceBelow = window.innerHeight - bounds.bottom;
    if (spaceBelow >= dialog.offsetHeight + 12) {
      dialog.style.top = `${bounds.bottom + 6}px`;
    } else {
      dialog.style.top = `${Math.max(8, bounds.top - dialog.offsetHeight - 6)}px`;
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
    activeTimeInput = null;
    originalTimeValue = "";
  };
  document.querySelectorAll("[data-unified-time]").forEach((input) => {
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
  });
  dialog?.querySelector("[data-picker-ok]").addEventListener("click", () => closePicker(true));
  dialog?.querySelector("[data-picker-cancel]").addEventListener("click", () => closePicker(false));
  dialog?.querySelectorAll("select").forEach((select) => select.addEventListener("change", () => {
    if (activeTimeInput) activeTimeInput.value = formatTime(dialogMinutes());
  }));
  document.addEventListener("pointerdown", (event) => {
    if (activeTimeInput && !dialog.contains(event.target) && event.target !== activeTimeInput) closePicker(false);
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
      form.addEventListener("submit", (event) => {
        const start = form.elements.start_time;
        const end = form.elements.end_time;
        const startMinutes = parseTime(start.value);
        const endMinutes = parseTime(end.value);
        if (startMinutes === null || endMinutes === null) {
          event.preventDefault();
          (startMinutes === null ? start : end).setCustomValidity("Please enter a time in 5-minute increments.");
          (startMinutes === null ? start : end).reportValidity();
          return;
        }
        start.value = formatTime(startMinutes);
        end.value = formatTime(endMinutes);
        if (endMinutes <= startMinutes) {
          event.preventDefault();
          end.setCustomValidity("End time must be later than start time.");
          end.reportValidity();
          end.setCustomValidity("");
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
    cancel.addEventListener("click", () => window.location.reload());
    current.querySelectorAll("[data-remove-availability]").forEach((button) => button.addEventListener("click", () => {
      const row = button.closest("[data-availability-entry]"); row.dataset.deleted = "true"; row.hidden = true;
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
      bulkForm.requestSubmit();
    });
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
    results.slice(0, 5).forEach((result) => {
      const card = document.createElement("article"); card.className = "preview-result";
      const title = document.createElement("strong"); title.textContent = `${result.day}, ${result.date}`;
      const time = document.createElement("span"); time.textContent = `${formatTime(Number(result.start.slice(0, 2)) * 60 + Number(result.start.slice(3))) } – ${formatTime(Number(result.end.slice(0, 2)) * 60 + Number(result.end.slice(3)))}`;
      const attendance = document.createElement("span"); attendance.textContent = `${result.attendance}/${result.group_size} available`;
      const badge = document.createElement("span"); badge.className = result.exact_match ? "preview-badge" : "preview-badge alternative"; badge.textContent = result.exact_match ? "Exact match" : `Shorter option · ${result.available_duration_minutes} min`;
      card.append(title, time, attendance, badge); resultsContainer.append(card);
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
