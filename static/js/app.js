document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-rsvp-form]").forEach((form) => {
    const status = form.querySelector("[data-rsvp-status]");
    const arrivalField = form.querySelector("[data-rsvp-arrival-field]");
    const departureField = form.querySelector("[data-rsvp-departure-field]");
    const arrival = form.querySelector("[data-rsvp-arrival]");
    const departure = form.querySelector("[data-rsvp-departure]");
    const updateTimeFields = () => {
      const needsArrival = ["arriving_late", "arriving_late_leaving_early"].includes(status.value);
      const needsDeparture = ["leaving_early", "arriving_late_leaving_early"].includes(status.value);
      arrivalField.hidden = !needsArrival;
      departureField.hidden = !needsDeparture;
      arrival.required = needsArrival;
      departure.required = needsDeparture;
      if (!needsArrival) arrival.value = "";
      if (!needsDeparture) departure.value = "";
    };
    status.addEventListener("change", updateTimeFields);
    updateTimeFields();
  });

  document.querySelectorAll("[data-availability-editor]").forEach((editor) => {
    const tabs = editor.querySelectorAll("[data-availability-day]");
    const panels = editor.querySelectorAll("[data-availability-panel]");
    const activateDay = (day) => {
      tabs.forEach((tab) => {
        const active = tab.dataset.availabilityDay === day;
        tab.classList.toggle("is-active", active);
        tab.setAttribute("aria-selected", String(active));
      });
      panels.forEach((panel) => {
        panel.classList.toggle("is-hidden", panel.dataset.availabilityPanel !== day);
      });
    };
    tabs.forEach((tab) => tab.addEventListener("click", () => activateDay(tab.dataset.availabilityDay)));

    const pickerMinutes = (picker) => {
      const hour = Number(picker.querySelector('[name$="_hour"]').value) % 12;
      const minute = Number(picker.querySelector('[name$="_minute"]').value);
      return hour * 60 + minute + (picker.querySelector('[name$="_period"]').value === "PM" ? 720 : 0);
    };
    const setPickerMinutes = (picker, minutes) => {
      const period = minutes >= 720 ? "PM" : "AM";
      const displayMinutes = minutes % 720;
      const hour = Math.floor(displayMinutes / 60) || 12;
      const minute = String(displayMinutes % 60).padStart(2, "0");
      picker.querySelector('[name$="_hour"]').value = String(hour);
      picker.querySelector('[name$="_minute"]').value = minute;
      picker.querySelector('[name$="_period"]').value = period;
    };

    editor.querySelectorAll("[data-availability-add-form]").forEach((form) => {
      const startPicker = form.querySelector('[data-time-role="start"]');
      const endPicker = form.querySelector('[data-time-role="end"]');
      startPicker.querySelectorAll("select").forEach((select) => {
        select.addEventListener("change", () => {
          const suggestedEnd = pickerMinutes(startPicker) + 60;
          if (suggestedEnd < 1440) setPickerMinutes(endPicker, suggestedEnd);
        });
      });
      form.addEventListener("submit", (event) => {
        if (pickerMinutes(endPicker) <= pickerMinutes(startPicker)) {
          event.preventDefault();
          endPicker.querySelector("select").setCustomValidity("End time must be later than start time.");
          endPicker.querySelector("select").reportValidity();
          endPicker.querySelector("select").setCustomValidity("");
        }
      });
    });
  });

  const planForm = document.querySelector("form[data-schedule-preview-url]");
  if (!planForm) return;

  const message = planForm.querySelector("[data-preview-message]");
  const resultsContainer = planForm.querySelector("[data-preview-results]");
  const watchedNames = [
    "start_date", "end_date", "duration_minutes", "minimum_attendance",
    "earliest_time", "latest_time",
  ];
  let timer;
  let requestNumber = 0;

  const formatTime = (value) => {
    const [hourText, minute] = value.split(":");
    const hour = Number(hourText);
    const suffix = hour >= 12 ? "PM" : "AM";
    return `${hour % 12 || 12}:${minute} ${suffix}`;
  };

  const renderResults = (results) => {
    resultsContainer.replaceChildren();
    if (!results.length) {
      message.textContent = "No options currently meet these requirements.";
      return;
    }
    message.textContent = "Preview only — save the plan before scheduling a time.";
    results.slice(0, 5).forEach((result) => {
      const card = document.createElement("article");
      card.className = "preview-result";
      const title = document.createElement("strong");
      title.textContent = `${result.day}, ${result.date}`;
      const time = document.createElement("span");
      time.textContent = `${formatTime(result.start)} – ${formatTime(result.end)}`;
      const attendance = document.createElement("span");
      attendance.textContent = `${result.attendance}/${result.group_size} available`;
      const badge = document.createElement("span");
      badge.className = result.exact_match ? "preview-badge" : "preview-badge alternative";
      badge.textContent = result.exact_match
        ? "Exact match"
        : `Shorter option · ${result.available_duration_minutes} min`;
      card.append(title, time, attendance, badge);
      resultsContainer.append(card);
    });
  };

  const requestPreview = async () => {
    const required = ["start_date", "end_date", "duration_minutes", "minimum_attendance"];
    if (required.some((name) => !planForm.elements[name]?.value)) {
      resultsContainer.replaceChildren();
      message.textContent = "Complete the scheduling fields to preview matching times.";
      return;
    }
    const thisRequest = ++requestNumber;
    message.textContent = "Calculating best times…";
    try {
      const response = await fetch(planForm.dataset.schedulePreviewUrl, {
        method: "POST",
        body: new URLSearchParams(new FormData(planForm)),
        headers: { "X-Requested-With": "fetch" },
      });
      const data = await response.json();
      if (thisRequest !== requestNumber) return;
      if (!data.success) {
        resultsContainer.replaceChildren();
        message.textContent = data.errors?.[0] || "Enter valid scheduling requirements.";
        return;
      }
      renderResults(data.results);
    } catch (_error) {
      if (thisRequest === requestNumber) {
        resultsContainer.replaceChildren();
        message.textContent = "Preview is temporarily unavailable. You can still save the plan.";
      }
    }
  };

  watchedNames.forEach((name) => {
    const field = planForm.elements[name];
    if (!field) return;
    field.addEventListener("input", () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(requestPreview, 400);
    });
  });
  requestPreview();
});
