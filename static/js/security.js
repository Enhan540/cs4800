"use strict";

// Share the same CSRF protection across forms and asynchronous mutations.
window.va = window.va || function () { (window.vaq = window.vaq || []).push(arguments); };
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const original = input instanceof Request ? input : undefined;
    const url = new URL(original ? original.url : input, window.location.href);
    const method = (options.method || original?.method || "GET").toUpperCase();
    if (url.origin === window.location.origin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      const headers = new Headers(options.headers || original?.headers);
      headers.set("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]')?.content || "");
      options = { ...options, headers };
    }
    return originalFetch(input, options);
  };
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.method?.toUpperCase() !== "POST" || new URL(form.action, window.location.href).origin !== window.location.origin) return;
    let field = form.querySelector('[name="csrf_token"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = "csrf_token";
      form.appendChild(field);
    }
    field.value = document.querySelector('meta[name="csrf-token"]')?.content || "";
  }, true);
})();
