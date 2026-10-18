document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-rsvp-form]").forEach((form) => {
    const status = form.querySelector("[data-rsvp-status]");
    const field = form.querySelector("[data-rsvp-time-field]");
    const input = form.querySelector("[data-rsvp-time]");
    const updateTimeField = () => {
      const needsTime = status.value === "arriving_late" || status.value === "leaving_early";
      field.hidden = !needsTime;
      input.required = needsTime;
      if (!needsTime) input.value = "";
    };
    status.addEventListener("change", updateTimeField);
    updateTimeField();
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
