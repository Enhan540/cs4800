document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("#availability-form");
  if (!form) return;

  form.addEventListener("submit", (event) => {
    const error = document.querySelector("#form-error");
    const fields = [...form.querySelectorAll("[required]")];
    const missing = fields.some((field) => !field.value.trim());
    const start = document.querySelector("#start_time").value;
    const end = document.querySelector("#end_time").value;

    error.textContent = "";
    if (missing) {
      event.preventDefault();
      error.textContent = "Please fill in every field.";
      return;
    }
    if (end <= start) {
      event.preventDefault();
      error.textContent = "End time must be later than start time.";
    }
  });
});
