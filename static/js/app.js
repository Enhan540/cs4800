const NOTIFICATION_POLL_INTERVAL_MS = 45 * 1000;

document.addEventListener("DOMContentLoaded", () => {
  const sidebar = document.querySelector("[data-app-sidebar]");
  const closeSidebar = () => document.body.classList.remove("sidebar-open");
  document.querySelectorAll("[data-sidebar-open]").forEach((button) => button.addEventListener("click", () => document.body.classList.add("sidebar-open")));
  document.querySelectorAll("[data-sidebar-close]").forEach((button) => button.addEventListener("click", closeSidebar));
  sidebar?.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
  document.querySelectorAll("[data-group-availability]").forEach((groupView) => {
    const memberButtons = Array.from(groupView.querySelectorAll("[data-member-select]"));
    const overlays = Array.from(groupView.querySelectorAll("[data-member-overlay]"));
    const clearButton = groupView.querySelector("[data-member-clear]");
    const setSelectedMember = (memberId) => {
      const selected = memberId || "";
      groupView.dataset.selectedMember = selected;
      memberButtons.forEach((button) => {
        const isSelected = button.dataset.memberSelect === selected;
        button.setAttribute("aria-selected", String(isSelected));
      });
      overlays.forEach((overlay) => {
        const isSelected = Boolean(selected) && overlay.dataset.memberOverlay === selected;
        overlay.hidden = !isSelected;
        overlay.setAttribute("aria-hidden", String(!isSelected));
      });
      if (clearButton) clearButton.hidden = !selected;
    };
    memberButtons.forEach((button) => button.addEventListener("click", () => {
      const next = button.dataset.memberSelect === groupView.dataset.selectedMember ? "" : button.dataset.memberSelect;
      setSelectedMember(next);
    }));
    clearButton?.addEventListener("click", () => setSelectedMember(""));
    setSelectedMember("");
  });
  document.querySelectorAll(".user-menu").forEach((menu) => {
    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { menu.open = false; menu.querySelector("summary")?.focus(); }
    });
  });
  document.querySelectorAll("[data-notification-menu]").forEach((menu) => {
    const summary = menu.querySelector("summary");
    const list = menu.querySelector(".notification-list");
    const heading = menu.querySelector(".notification-heading");
    const refreshButton = document.createElement("button");
    refreshButton.type = "button";
    refreshButton.className = "icon-button notification-refresh";
    refreshButton.setAttribute("aria-label", "Refresh notifications");
    refreshButton.textContent = "↻";
    heading?.appendChild(refreshButton);
    let refreshing = false;
    const setBellState = (count) => {
      const existing = menu.querySelector(".notification-unread-dot");
      if (count && !existing) {
        const dot = document.createElement("i");
        dot.className = "notification-unread-dot";
        dot.setAttribute("aria-label", "Unread notifications");
        summary.appendChild(dot);
      } else if (!count) existing?.remove();
    };
    const renderNotifications = (items) => {
      if (!list) return;
      list.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "notification-empty";
        empty.textContent = "You're all caught up.";
        list.appendChild(empty);
        return;
      }
      items.forEach((item) => {
        const card = document.createElement("article");
        card.className = `notification-item${item.read ? " is-read" : ""}`;
        const title = document.createElement("strong");
        const text = document.createElement("p");
        const actions = document.createElement("div");
        actions.className = "notification-actions";
        if (item.type === "GROUP_INVITE") {
          title.textContent = item.group_name;
          text.textContent = `${item.actor_name} invited you to join this group.`;
          [["Decline", "secondary", `/invitations/${item.invite_id}/decline`], ["Accept", "", `/invitations/${item.invite_id}/accept`]].forEach(([label, style, action]) => {
            const form = document.createElement("form"); form.method = "post"; form.action = action;
            const button = document.createElement("button"); button.type = "submit"; button.className = `button ${style}`.trim(); button.textContent = label;
            form.appendChild(button); actions.appendChild(form);
          });
        } else if (item.type === "PLAN_SCHEDULED") {
          const link = document.createElement("a"); link.className = "notification-link"; link.href = `/plans/${item.plan_id}`;
          title.textContent = item.plan_title; link.appendChild(title);
          text.textContent = "The plan has been scheduled."; link.appendChild(text);
          const times = document.createElement("small"); times.textContent = `${item.scheduled_start.replace("T", " ")} – ${item.scheduled_end.replace("T", " ")}`; link.appendChild(times);
          card.appendChild(link);
          [["Not Going", "secondary", "not_attending"], ["Attending", "", "attending"]].forEach(([label, style, status]) => {
            const form = document.createElement("form"); form.method = "post"; form.action = `/notifications/${item.id}/rsvp`;
            const input = document.createElement("input"); input.type = "hidden"; input.name = "status"; input.value = status;
            const button = document.createElement("button"); button.type = "submit"; button.className = `button ${style}`.trim(); button.textContent = label;
            form.append(input, button); actions.appendChild(form);
          });
        }
        if (item.type === "GROUP_INVITE") card.append(title, text);
        card.appendChild(actions); list.appendChild(card);
      });
    };
    const refreshNotifications = async () => {
      if (refreshing) return;
      refreshing = true; refreshButton.disabled = true; refreshButton.classList.add("is-loading");
      try {
        const response = await fetch("/api/notifications", { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("notification refresh failed");
        const payload = await response.json(); renderNotifications(payload.notifications); setBellState(payload.unread_count);
      } catch (_error) {
        let status = menu.querySelector("[data-notification-refresh-status]");
        if (!status) { status = document.createElement("p"); status.className = "notification-refresh-status"; heading?.after(status); }
        status.textContent = "Couldn't refresh notifications.";
      } finally { refreshing = false; refreshButton.disabled = false; refreshButton.classList.remove("is-loading"); }
    };
    refreshButton.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); refreshNotifications(); });
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      refreshNotifications().then(() => fetch("/notifications/read", { method: "POST", headers: { "X-Requested-With": "fetch" } }).then(() => setBellState(0)).catch(() => {}));
    });
    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { menu.open = false; summary?.focus(); }
    });
    const pollNotifications = async () => {
      if (document.hidden) return;
      try {
        const response = await fetch("/api/notifications/summary", { headers: { Accept: "application/json" } });
        if (response.ok) setBellState((await response.json()).unread_count);
      } catch (_error) { /* A transient poll failure leaves the current UI intact. */ }
    };
    window.setInterval(pollNotifications, NOTIFICATION_POLL_INTERVAL_MS);
  });
  const drawer = document.querySelector("[data-notification-drawer]");
  if (drawer) {
    drawer.dataset.enhanced = "true";
    const backdrop = document.querySelector("[data-notification-close]");
    const list = drawer.querySelector("[data-notification-list]");
    const refresh = drawer.querySelector("[data-notification-refresh]");
    const count = drawer.querySelector("[data-notification-selected-count]");
    const deselect = drawer.querySelector("[data-notification-deselect]");
    const markSelected = drawer.querySelector("[data-notification-mark-selected]");
    const deleteSelected = drawer.querySelector("[data-notification-delete-selected]");
    const newCount = drawer.querySelector("[data-notification-new-count]");
    let items = [];
    let selected = new Set();
    let filter = "all";
    let loading = false;
    const setUnread = (value) => document.querySelectorAll(".sidebar-unread-count").forEach((badge) => { badge.textContent = value || ""; badge.hidden = !value; });
    const updateSelection = () => { const n = selected.size; count.textContent = `${n} selected`; [deselect, markSelected, deleteSelected].forEach((button) => { if (button) button.disabled = n === 0; }); };
    const categoryMatch = (item) => filter === "all" || (filter === "invites" && item.type === "GROUP_INVITE") || (filter === "activity" && item.type !== "GROUP_INVITE");
    const relativeTime = (value) => { const then = new Date(value); const seconds = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1000)); if (seconds < 60) return "just now"; if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`; if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`; if (seconds < 172800) return "Yesterday"; return `${Math.floor(seconds / 86400)}d ago`; };
    const initials = (value) => String(value || "Group").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "G";
    const postAction = (action, id, label) => { const form = document.createElement("form"); form.method = "post"; form.action = action; form.dataset.notificationAction = "true"; const button = document.createElement("button"); button.className = `button ${label === "Decline" || label === "Not Going" ? "secondary" : ""}`.trim(); button.type = "submit"; button.textContent = label; button.setAttribute("aria-label", label); form.appendChild(button); return form; };
    const render = () => {
      list.replaceChildren();
      const visible = items.filter(categoryMatch);
      if (!visible.length) { const empty = document.createElement("p"); empty.className = "notification-empty"; empty.textContent = "You're all caught up."; list.appendChild(empty); updateSelection(); return; }
      visible.forEach((item) => {
        const card = document.createElement("article"); card.className = `notification-item${item.read ? " is-read" : ""}`; card.dataset.notificationId = item.id;
        const check = document.createElement("input"); check.type = "checkbox"; check.checked = selected.has(item.id); check.setAttribute("aria-label", `Select notification ${item.group_name || item.plan_title || "update"}`); check.addEventListener("click", (event) => event.stopPropagation()); check.addEventListener("change", () => { if (check.checked) selected.add(item.id); else selected.delete(item.id); updateSelection(); });
        const body = document.createElement("div"); body.className = "notification-body";
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-button notification-delete"; remove.setAttribute("aria-label", "Delete notification"); remove.textContent = "×"; remove.addEventListener("click", async (event) => { event.stopPropagation(); if (!window.confirm("Delete this notification?")) return; const response = await fetch(`/api/notifications/${item.id}`, { method: "DELETE", headers: { "X-Requested-With": "fetch" } }); if (response.ok) { selected.delete(item.id); await refreshDrawer(); } });
        if (item.type === "GROUP_INVITE") { const titleRow = document.createElement("div"); titleRow.className = "notification-title-row"; const icon = document.createElement("span"); icon.className = "notification-group-icon"; icon.textContent = initials(item.group_name); const title = document.createElement("b"); title.textContent = item.group_name; const time = document.createElement("time"); time.className = "notification-time"; time.dateTime = item.created_at; time.textContent = relativeTime(item.created_at); const unread = document.createElement("span"); unread.className = "notification-unread-indicator"; unread.hidden = item.read; unread.textContent = "●"; titleRow.append(icon, title, unread, time); const text = document.createElement("p"); text.textContent = `${item.actor_name} invited you to join this group.`; body.append(titleRow, text); const actions = document.createElement("div"); actions.className = "notification-actions"; if (item.invite_status === "pending") actions.append(postAction(`/invitations/${item.invite_id}/decline`, item.id, "Decline"), postAction(`/invitations/${item.invite_id}/accept`, item.id, "Join Group")); else { const stale = document.createElement("small"); stale.className = "notification-stale"; stale.textContent = "Invitation no longer available"; actions.append(stale); } body.appendChild(actions); }
        if (item.type === "PLAN_SCHEDULED") { const link = document.createElement("a"); link.className = "notification-link"; link.href = `/plans/${item.plan_id}`; const title = document.createElement("b"); title.textContent = item.plan_title; const text = document.createElement("p"); text.textContent = "The plan has been scheduled."; const time = document.createElement("small"); time.textContent = `${item.scheduled_start.replace("T", " ")} – ${item.scheduled_end.replace("T", " ")}`; link.append(title, text, time); body.appendChild(link); const actions = document.createElement("div"); actions.className = "notification-actions"; actions.append(postAction(`/notifications/${item.id}/rsvp`, item.id, "Not Going"), postAction(`/notifications/${item.id}/rsvp`, item.id, "Attending")); actions.querySelectorAll("form").forEach((form, index) => { const input = document.createElement("input"); input.type = "hidden"; input.name = "status"; input.value = index === 0 ? "not_attending" : "attending"; form.prepend(input); }); body.appendChild(actions); }
        card.append(check, body, remove); card.querySelectorAll("form[data-notification-action]").forEach((form) => form.addEventListener("submit", async (event) => { event.preventDefault(); const buttons = card.querySelectorAll("button"); buttons.forEach((button) => { button.disabled = true; }); const active = form.querySelector("button"); if (active) active.textContent = active.textContent === "Join Group" ? "Joining…" : active.textContent === "Decline" ? "Declining…" : "Saving…"; try { const response = await fetch(form.action, { method: "POST", headers: { "X-Requested-With": "fetch", Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(new FormData(form)) }); const payload = await response.json(); if (!response.ok || !payload.ok) throw new Error(payload.error || "Action could not be completed."); selected.delete(item.id); await refreshDrawer(); } catch (error) { let message = card.querySelector(".notification-inline-error"); if (!message) { message = document.createElement("small"); message.className = "notification-inline-error"; body.appendChild(message); } message.textContent = error.message; buttons.forEach((button) => { button.disabled = false; }); if (active) active.textContent = active.textContent.includes("Joining") ? "Join Group" : active.textContent.includes("Declining") ? "Decline" : "Save"; } })); list.appendChild(card);
      });
      updateSelection();
    };
    const refreshDrawer = async () => { if (loading) return; loading = true; refresh.disabled = true; refresh.classList.add("is-loading"); try { const response = await fetch("/api/notifications", { headers: { Accept: "application/json" } }); if (!response.ok) throw new Error(); const payload = await response.json(); items = payload.notifications; const available = new Set(items.map((item) => item.id)); selected = new Set([...selected].filter((id) => available.has(id))); newCount.textContent = payload.unread_count ? `${payload.unread_count} new` : ""; newCount.hidden = !payload.unread_count; setUnread(payload.unread_count); render(); } catch (_error) { let status = drawer.querySelector("[data-notification-refresh-status]"); if (!status) { status = document.createElement("p"); status.className = "notification-refresh-status"; drawer.querySelector(".notification-drawer-actions")?.after(status); } status.textContent = "Couldn't refresh notifications."; } finally { loading = false; refresh.disabled = false; refresh.classList.remove("is-loading"); } };
    const batch = async (url) => { const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" }, body: JSON.stringify({ ids: [...selected] }) }); if (!response.ok) throw new Error(); const payload = await response.json(); setUnread(payload.unread_count); selected.clear(); await refreshDrawer(); };
    refresh?.addEventListener("click", (event) => { event.stopPropagation(); refreshDrawer(); });
    deselect?.addEventListener("click", () => { selected.clear(); render(); });
    markSelected?.addEventListener("click", () => batch("/api/notifications/mark-read").catch(() => {}));
    deleteSelected?.addEventListener("click", () => { if (window.confirm(`Delete ${selected.size} notification${selected.size === 1 ? "" : "s"}?`)) batch("/api/notifications/delete").catch(() => {}); });
    drawer.querySelectorAll("[data-notification-filter]").forEach((button) => button.addEventListener("click", () => { filter = button.dataset.notificationFilter; drawer.querySelectorAll("[data-notification-filter]").forEach((tab) => tab.classList.toggle("is-active", tab === button)); render(); }));
    const closeDrawer = () => { drawer.hidden = true; drawer.setAttribute("aria-hidden", "true"); if (backdrop) backdrop.hidden = true; document.body.classList.remove("notification-drawer-open"); };
    document.querySelectorAll("[data-notification-open]").forEach((button) => button.addEventListener("click", () => { drawer.hidden = false; drawer.setAttribute("aria-hidden", "false"); if (backdrop) backdrop.hidden = false; document.body.classList.add("notification-drawer-open"); refreshDrawer(); }));
    document.querySelectorAll("[data-notification-close]").forEach((button) => button.addEventListener("click", closeDrawer));
    drawer.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
    window.setInterval(async () => { if (document.hidden) return; try { const response = await fetch("/api/notifications/summary", { headers: { Accept: "application/json" } }); if (response.ok) setUnread((await response.json()).unread_count); } catch (_error) {} }, NOTIFICATION_POLL_INTERVAL_MS);
  }
  if (drawer && !drawer.dataset.enhanced) {
    const backdrop = document.querySelector("[data-notification-close]");
    const list = drawer.querySelector("[data-notification-list]");
    const refresh = drawer.querySelector("[data-notification-refresh]");
    const setUnread = (count) => {
      document.querySelectorAll(".sidebar-unread-count").forEach((badge) => {
        badge.textContent = count || ""; badge.hidden = !count;
      });
    };
    const renderDrawer = (items) => {
      list.replaceChildren();
      if (!items.length) { const empty = document.createElement("p"); empty.className = "notification-empty"; empty.textContent = "You're all caught up."; list.appendChild(empty); return; }
      items.forEach((item) => {
        const card = document.createElement("article"); card.className = "notification-item";
        if (item.type === "GROUP_INVITE") {
          const title = document.createElement("strong"); title.textContent = item.group_name;
          const text = document.createElement("p"); text.textContent = `${item.actor_name} invited you to join this group.`;
          card.append(title, text);
          const actions = document.createElement("div"); actions.className = "notification-actions";
          [["Decline", "secondary", `/invitations/${item.invite_id}/decline`], ["Join Group", "", `/invitations/${item.invite_id}/accept`]].forEach(([label, style, action]) => { const form = document.createElement("form"); form.method = "post"; form.action = action; const button = document.createElement("button"); button.className = `button ${style}`.trim(); button.type = "submit"; button.textContent = label; form.appendChild(button); actions.appendChild(form); });
          card.appendChild(actions);
        } else if (item.type === "PLAN_SCHEDULED") {
          const link = document.createElement("a"); link.className = "notification-link"; link.href = `/plans/${item.plan_id}`;
          const title = document.createElement("strong"); title.textContent = item.plan_title; const text = document.createElement("p"); text.textContent = "The plan has been scheduled."; const time = document.createElement("small"); time.textContent = `${item.scheduled_start.replace("T", " ")} – ${item.scheduled_end.replace("T", " ")}`; link.append(title, text, time); card.appendChild(link);
          const actions = document.createElement("div"); actions.className = "notification-actions";
          [["Not Going", "secondary", "not_attending"], ["Attending", "", "attending"]].forEach(([label, style, status]) => { const form = document.createElement("form"); form.method = "post"; form.action = `/notifications/${item.id}/rsvp`; const input = document.createElement("input"); input.type = "hidden"; input.name = "status"; input.value = status; const button = document.createElement("button"); button.className = `button ${style}`.trim(); button.type = "submit"; button.textContent = label; form.append(input, button); actions.appendChild(form); });
          card.appendChild(actions);
        }
        list.appendChild(card);
      });
    };
    let drawerRefreshing = false;
    const refreshDrawer = async () => {
      if (drawerRefreshing) return;
      drawerRefreshing = true; refresh.disabled = true; refresh.classList.add("is-loading");
      try { const response = await fetch("/api/notifications", { headers: { Accept: "application/json" } }); if (!response.ok) throw new Error(); const payload = await response.json(); renderDrawer(payload.notifications); setUnread(payload.unread_count); }
      catch (_error) { let status = drawer.querySelector("[data-notification-refresh-status]"); if (!status) { status = document.createElement("p"); status.className = "notification-refresh-status"; status.textContent = "Couldn't refresh notifications."; drawer.querySelector(".notification-drawer-actions")?.after(status); } }
      finally { drawerRefreshing = false; refresh.disabled = false; refresh.classList.remove("is-loading"); }
    };
    const closeDrawer = () => { drawer.hidden = true; drawer.setAttribute("aria-hidden", "true"); if (backdrop) backdrop.hidden = true; document.body.classList.remove("notification-drawer-open"); };
    document.querySelectorAll("[data-notification-open]").forEach((button) => button.addEventListener("click", () => { drawer.hidden = false; drawer.setAttribute("aria-hidden", "false"); if (backdrop) backdrop.hidden = false; document.body.classList.add("notification-drawer-open"); refreshDrawer().then(() => fetch("/notifications/read", { method: "POST" }).then(() => setUnread(0)).catch(() => {})); }));
    document.querySelectorAll("[data-notification-close]").forEach((button) => button.addEventListener("click", closeDrawer));
    refresh?.addEventListener("click", refreshDrawer);
    drawer.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
    window.setInterval(async () => { if (document.hidden) return; try { const response = await fetch("/api/notifications/summary", { headers: { Accept: "application/json" } }); if (response.ok) setUnread((await response.json()).unread_count); } catch (_error) {} }, NOTIFICATION_POLL_INTERVAL_MS);
  }

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
            const label = item.state === "member" ? "Member ✓" : item.state === "pending" ? "Invited ✓" : "Invite";
            const action = item.state === "available" ? `<button type="button" data-invite-id="${item.id}" data-invite-username="${item.username}">${label}</button>` : `<span class="muted small">${label}</span>`;
            return `<div class="invite-search-result"><span>${item.username}</span>${action}</div>`;
          }).join("") : '<p class="muted small">No matching accounts.</p>';
          results.querySelectorAll("[data-invite-username]").forEach((button) => button.addEventListener("click", () => {
            button.disabled = true;
            button.textContent = "Inviting…";
            fetch(input.closest("form").action, { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ username: button.dataset.inviteUsername }) })
              .then(async (inviteResponse) => { const data = await inviteResponse.json(); if (!inviteResponse.ok || !data.ok) throw new Error(data.error || "Invitation failed"); button.textContent = "Invited ✓"; button.classList.add("is-invited"); })
              .catch((error) => { button.disabled = false; button.textContent = "Retry / Invite"; const message = document.createElement("small"); message.className = "invite-error"; message.textContent = error.message; button.closest(".invite-search-result")?.appendChild(message); });
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
    const compact = String(value ?? "").trim().toUpperCase().replace(/[.\s]/g, "");
    if (!compact || !/^[0-9:APM]+$/.test(compact)) return null;
    const match = compact.match(/^(\d{1,4})(?::?(\d{1,2}))?([AP]M?|)$/);
    if (!match) return null;
    let hourText = match[1];
    let minuteText = match[2];
    const periodText = match[3];
    if (minuteText === undefined && hourText.length >= 3) {
      minuteText = hourText.slice(-2);
      hourText = hourText.slice(0, -2);
    }
    const hour = Number(hourText);
    const minute = Number(minuteText || "0");
    if (hour < 1 || hour > 12 || minute > 59) return null;
    if (periodText && !["A", "AM", "P", "PM"].includes(periodText)) return null;
    // Bare clock text is treated as morning in the manual-entry UI. Picker
    // selections and submitted values still carry an explicit period/24-hour value.
    return (hour % 12) * 60 + minute + (["P", "PM"].includes(periodText) ? 720 : 0);
  };
  const formatTime = (minutes) => {
    const hour = Math.floor((minutes % 720) / 60) || 12;
    return `${hour}:${String(minutes % 60).padStart(2, "0")} ${minutes >= 720 ? "P.M." : "A.M."}`;
  };
  const formatTypedTime = (value) => {
    const raw = String(value ?? "").trim().toUpperCase().replace(/[.\s]/g, "");
    if (!raw) return "";
    const suffixMatch = raw.match(/([AP]M?)$/);
    const suffix = suffixMatch ? (suffixMatch[1].length === 1 ? `${suffixMatch[1]}M` : suffixMatch[1]) : "";
    const body = suffixMatch ? raw.slice(0, -suffixMatch[1].length) : raw;
    if (!/^\d{1,6}:?\d{0,2}$/.test(body)) return null;
    let digits = body.replace(":", "");
    // A pasted/rapid sequence such as 17111 contains an illegal `7` in the
    // ambiguous 1x position. Drop only that illegal digit, yielding 11:11.
    if (digits.length > 4) {
      if (digits[0] === "1" && Number(digits[1]) > 5) digits = digits[0] + digits.slice(2);
      else return null;
    }
    let hourText = ""; let minuteText = "";
    if (body.includes(":")) {
      [hourText, minuteText] = body.split(":");
      if (hourText.length > 2 || (hourText && Number(hourText) > 12)) return null;
      if (minuteText && Number(minuteText[0]) > 5) return body.slice(0, -1);
    } else if (digits.length <= 2) {
      if (digits.length === 1) {
        if (digits === "0") return digits;
        return Number(digits) >= 2 ? `${digits}:` : digits;
      }
      const first = Number(digits[0]);
      const second = Number(digits[1]);
      if (first === 0) return second === 0 ? null : `${second}:`;
      if (first === 1) {
        if (second <= 2) return `${digits}:`;
        // 13–15 begin a compact hour/minute entry. 16–19 are not valid
        // minute-tens digits, so ignore that keystroke and keep the `1`.
        return second <= 5 ? `1:${second}` : "1";
      }
      // For 2–9 the first digit is already the hour. A second digit is
      // accepted only as a valid minute tens digit; 99 must not become 9:09.
      return second <= 5 ? `${first}:${second}` : `${first}:`;
    } else if (digits.length === 3) {
      if (Number(digits.slice(0, 2)) <= 12) { hourText = digits.slice(0, 2); minuteText = digits.slice(2); }
      else { hourText = digits[0]; minuteText = digits.slice(1); }
    } else { hourText = digits.slice(0, 2); minuteText = digits.slice(2); }
    if (!hourText) return body;
    const hour = Number(hourText);
    if (hour < 1 || hour > 12 || (minuteText && Number(minuteText) > 59)) return null;
    let result = `${Number(hourText)}:${minuteText}`;
    if (suffix) {
      if (!minuteText) result = `${Number(hourText)}:00`;
      else if (minuteText.length === 1) result = `${Number(hourText)}:${minuteText}0`;
      result += ` ${suffix === "PM" ? "P.M." : "A.M."}`;
    }
    return result;
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
    const minute = String(minutes % 60).padStart(2, "0");
    if (![...dialogMinute.options].some((option) => option.value === minute)) {
      const option = document.createElement("option"); option.value = minute; option.textContent = minute;
      dialogMinute.appendChild(option);
    }
    dialogMinute.value = minute;
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
      let rawValue = input.value;
      const deleting = input.dataset.deleting === "true";
      // When deleting the final character of AM/PM, do not immediately
      // reconstruct the suffix from the remaining lone A/P character.
      if (deleting && /\s*[AP](?:\.?M)?\.?\s*$/i.test(rawValue)) {
        rawValue = rawValue.replace(/\s*[AP](?:\.?M)?\.?\s*$/i, "");
      }
      input.dataset.deleting = "false";
      const formatted = deleting && /^\d{1,2}$/.test(rawValue)
        ? rawValue
        : formatTypedTime(rawValue);
      if (formatted === null) { input.value = input.dataset.lastAccepted || ""; return; }
      input.value = formatted;
      input.dataset.lastAccepted = formatted;
      input.setSelectionRange(input.value.length, input.value.length);
      const minutes = parseTime(input.value);
      if (minutes !== null && activeTimeInput === input) setDialogTime(minutes);
    });
    input.addEventListener("blur", () => {
      if (dialog && !dialog.hidden && dialog.contains(document.activeElement)) return;
      if (!input.value.trim()) return;
      const minutes = parseTime(input.value);
      if (minutes === null) {
        input.setCustomValidity("Please enter a valid time.");
        input.classList.add("time-input-invalid");
      } else {
        input.setCustomValidity("");
        input.value = formatTime(minutes);
        input.classList.remove("time-input-invalid");
      }
    });
    input.addEventListener("keydown", (event) => {
      input.dataset.deleting = event.key === "Backspace" || event.key === "Delete" ? "true" : "false";
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
          (startMinutes === null ? start : end).setCustomValidity("Please enter a valid time.");
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
        const message = current.querySelector("[data-availability-form-message]");
        if (message) message.textContent = "Please enter valid times before saving.";
        return;
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
  windowDialog?.querySelector("[data-window-dialog-delete]")?.addEventListener("click", () => {
    const id = windowDialog.dataset.entryId;
    if (!id) return;
    openAvailabilityDeleteConfirmation(id, windowDialog.dataset.entryDay, "specific");
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
      invalid.setCustomValidity(endMinutes <= startMinutes ? "End time must be later than start time." : "Please enter a valid time.");
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
  let pendingDeleteMode = null;
  let pendingDeleteDay = null;
  const openAvailabilityDeleteConfirmation = (id, day, mode = "day") => {
    const entry = mode === "day" ? stagedWindows.get(id) : null;
    pendingDeleteId = id;
    pendingDeleteMode = mode;
    pendingDeleteDay = day;
    const start = entry?.start_time || windowStart?.value || "";
    const end = entry?.end_time || windowEnd?.value || "";
    if (dayDeleteMessage) {
      dayDeleteMessage.textContent = `${stagedTimeText(start)} – ${stagedTimeText(end)}\n\nAre you sure you want to remove this availability window?`;
    }
    showStableDialog(dayDeleteDialog);
  };
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
      openAvailabilityDeleteConfirmation(entry.id, activeDay, "day");
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
    pendingDeleteMode = null;
    pendingDeleteDay = null;
    closeStableDialog(dayDeleteDialog);
  }));
  dayDeleteDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    pendingDeleteId = null;
    pendingDeleteMode = null;
    pendingDeleteDay = null;
    closeStableDialog(dayDeleteDialog);
  });
  dayDeleteDialog?.addEventListener("click", (event) => {
    if (event.target === dayDeleteDialog) {
      pendingDeleteId = null;
      pendingDeleteMode = null;
      pendingDeleteDay = null;
      closeStableDialog(dayDeleteDialog);
    }
  });
  dayDeleteConfirm?.addEventListener("click", async () => {
    if (!pendingDeleteId) return;
    if (pendingDeleteMode === "specific") {
      const id = pendingDeleteId;
      dayDeleteConfirm.disabled = true;
      try {
        const response = await fetch(windowDialog.dataset.deleteUrlTemplate.replace("ENTRY_ID", id), {
          method: "POST", headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
          body: new URLSearchParams({return_to: windowForm.querySelector('[name="return_to"]').value}),
        });
        if (!response.ok) {
          dayDeleteMessage.textContent = "This availability window could not be deleted.";
          return;
        }
        removeAvailabilityBlock(id, pendingDeleteDay || windowDialog.dataset.entryDay);
        pendingDeleteId = null; pendingDeleteMode = null; pendingDeleteDay = null;
        closeStableDialog(dayDeleteDialog);
        closeWindowDialog();
      } finally {
        dayDeleteConfirm.disabled = false;
      }
      return;
    }
    captureActiveWindow();
    stagedWindows.delete(pendingDeleteId);
    if (activeWindowId === pendingDeleteId) activeWindowId = null;
    pendingDeleteId = null;
    pendingDeleteMode = null;
    pendingDeleteDay = null;
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
    if (invalid) { message.textContent = "Enter valid times with the end after the start."; return; }
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
