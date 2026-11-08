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
  const dialog = document.querySelector("[data-time-picker-dialog]");
  const dialogHour = dialog?.querySelector("[data-picker-hour]");
  const dialogMinute = dialog?.querySelector("[data-picker-minute]");
  const dialogPeriod = dialog?.querySelector("[data-picker-period]");
  let activeTimeInput = null;
  let originalTimeValue = "";
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
    dialog.style.width = `${Math.max(280, bounds.width)}px`;
    dialog.style.left = `${Math.max(8, Math.min(bounds.left, window.innerWidth - dialog.offsetWidth - 8))}px`;
    const spaceBelow = window.innerHeight - bounds.bottom;
    dialog.style.top = spaceBelow >= dialog.offsetHeight + 12
      ? `${bounds.bottom + 6}px`
      : `${Math.max(8, bounds.top - dialog.offsetHeight - 6)}px`;
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
      if (event.key === "Enter" && activeTimeInput === input) {
        event.preventDefault();
        const minutes = parseTime(input.value);
        if (minutes !== null) { setDialogTime(minutes); closePicker(true); }
      }
      if (event.key === "Escape" && activeTimeInput === input) {
        event.preventDefault(); event.stopPropagation(); closePicker(false);
      }
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
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activeTimeInput) {
      event.preventDefault(); closePicker(false);
    }
  });

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
    const canonical = /^(\d{1,2}):(\d{2})$/.exec(String(value || ""));
    if (!canonical) return parseTime(value);
    const hour = Number(canonical[1]), minute = Number(canonical[2]);
    return hour <= 23 && minute <= 59 ? hour * 60 + minute : null;
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
  const mergeAvailabilityRanges = (ranges) => ranges.sort((a, b) => a[0] - b[0]).reduce((merged, range) => {
    if (range[1] <= range[0]) return merged;
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
    return merged;
  }, []);
  const chartTime = (minutes) => minutes === 1440 ? "12:00 A.M." : formatTime(minutes);
  const refreshCombinedAvailability = (day) => {
    const column = availabilityColumnForDay(day);
    const track = column?.querySelector(".calendar-day-track");
    if (!track) return;
    const baseline = mergeAvailabilityRanges(availabilityBlocksForDay(day).map((block) => [
      availabilityMinutes(block.dataset.startTime), availabilityMinutes(block.dataset.endTime),
    ]).filter(([start, end]) => start !== null && end !== null));
    const busy = mergeAvailabilityRanges(Array.from(track.querySelectorAll("[data-calendar-mask]"))
      .map((mask) => [Number(mask.dataset.startMinute), Number(mask.dataset.endMinute)]));
    let free = baseline;
    busy.forEach(([start, end]) => {
      free = free.flatMap(([left, right]) => {
        if (end <= left || start >= right) return [[left, right]];
        return [[left, Math.min(start, right)], [Math.max(end, left), right]]
          .filter(([a, b]) => b > a);
      });
    });
    track.querySelectorAll("[data-effective-window]").forEach((block) => block.remove());
    free.forEach(([start, end]) => {
      const block = document.createElement("span");
      block.className = "calendar-effective-window";
      block.dataset.effectiveWindow = "true";
      block.setAttribute("role", "img");
      block.setAttribute("aria-label", `Actually free on ${column.dataset.date} from ${chartTime(start)} to ${chartTime(end)}`);
      block.style.top = `${start / 1440 * 100}%`;
      block.style.height = `${(end - start) / 1440 * 100}%`;
      track.appendChild(block);
    });
  };
  const updateAvailabilityBlock = (entry) => {
    const track = availabilityColumnForDay(entry.day)?.querySelector(".calendar-day-track");
    if (!track) return null;
    let block = track.querySelector(`[data-entry-id="${entry.id}"]`);
    if (!block) {
      block = document.createElement("button");
      block.type = "button";
      block.className = "calendar-window availability-window-block";
      block.dataset.availabilityWindow = "true";
      block.dataset.entryId = entry.id;
      block.setAttribute("aria-haspopup", "dialog");
      const label = document.createElement("span");
      label.className = "availability-window-label";
      block.appendChild(label);
      track.appendChild(block);
    }
    block.dataset.day = entry.day;
    block.dataset.startTime = entry.start_time;
    block.dataset.endTime = entry.end_time;
    const start = availabilityMinutes(entry.start_time), end = availabilityMinutes(entry.end_time);
    block.style.top = `${start / 1440 * 100}%`;
    block.style.height = `${(end - start) / 1440 * 100}%`;
    const label = `${availabilityTimeText(entry.start_time)} – ${availabilityTimeText(entry.end_time)}`;
    block.querySelector(".availability-window-label").textContent = label;
    block.title = label;
    block.setAttribute("aria-label", `${track.parentElement.dataset.date}, ${label}. View weekly availability.`);
    refreshCombinedAvailability(entry.day);
    return block;
  };

  const weeklyCalendar = document.querySelector(".weekly-calendar");
  document.querySelectorAll("[data-calendar-filter]").forEach((button) => button.addEventListener("click", () => {
    const selected = button.getAttribute("aria-pressed") === "true" ? null : button.dataset.calendarFilter;
    document.querySelectorAll("[data-calendar-filter]").forEach((item) =>
      item.setAttribute("aria-pressed", String(item.dataset.calendarFilter === selected)));
    document.querySelectorAll("[data-calendar-mask]").forEach((mask) => {
      mask.hidden = Boolean(selected && mask.dataset.calendarSource !== selected);
    });
  }));
  // One pixel per minute keeps small windows selectable, with 8 AM visible initially.
  if (weeklyCalendar) weeklyCalendar.scrollTop = 480;
  const availabilityPopover = document.querySelector("[data-availability-popover]");
  const availabilityForm = availabilityPopover?.querySelector("[data-availability-window-form]");
  const availabilityPreview = availabilityPopover?.querySelector("[data-availability-preview]");
  const availabilityError = availabilityPopover?.querySelector("[data-availability-error]");
  const availabilityStatus = document.querySelector("[data-availability-status]");
  const deleteDialog = document.querySelector("[data-availability-delete-dialog]");
  const deleteError = deleteDialog?.querySelector("[data-availability-delete-error]");
  let availabilitySelection = null;
  let availabilitySaving = false;
  const dateLabel = (column) => {
    // Construct in local civil time; parsing YYYY-MM-DD as UTC shifts the displayed day.
    const [year, month, day] = column.dataset.date.split("-").map(Number);
    return new Date(year, month - 1, day).toLocaleDateString(undefined, {
      weekday: "long", month: "long", day: "numeric", year: "numeric",
    });
  };
  const clearAvailabilityError = () => { availabilityError.hidden = true; availabilityError.textContent = ""; };
  const closeAvailabilityPopover = (restoreFocus = true) => {
    if (availabilitySaving || !availabilityPopover?.open) return;
    if (activeTimeInput && availabilityPopover.contains(activeTimeInput)) closePicker(false);
    const target = availabilitySelection?.focusTarget;
    availabilitySelection?.block?.classList.remove("is-selected");
    availabilitySelection?.draft?.remove();
    availabilityPopover.close();
    availabilitySelection = null;
    if (restoreFocus && target?.isConnected) target.focus({preventScroll: true});
  };
  const positionAvailabilityPopover = () => {
    if (!availabilityPopover?.open || !availabilitySelection) return;
    const {column, anchorMinute} = availabilitySelection;
    const track = column.querySelector(".calendar-day-track").getBoundingClientRect();
    const width = availabilityPopover.offsetWidth, height = availabilityPopover.offsetHeight;
    const mobile = window.innerWidth <= 600;
    let left = track.right + 12;
    if (left + width > window.innerWidth - 12) left = track.left - width - 12;
    left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
    const top = track.top + anchorMinute / 1440 * track.height;
    availabilityPopover.style.left = `${mobile ? (window.innerWidth - width) / 2 : left}px`;
    availabilityPopover.style.top = `${mobile
      ? Math.max(12, window.innerHeight - height - 12)
      : Math.max(12, Math.min(top, window.innerHeight - height - 12))}px`;
  };
  const renderAvailabilitySelection = () => {
    if (!availabilitySelection) return;
    const {column, block, mode} = availabilitySelection;
    availabilityPopover.querySelector("[data-availability-date]").textContent = dateLabel(column);
    availabilityPreview.hidden = mode !== "view";
    availabilityForm.hidden = mode === "view";
    availabilityForm.action = block
      ? availabilityPopover.dataset.editUrlTemplate.replace("ENTRY_ID", block.dataset.entryId)
      : availabilityPopover.dataset.createUrl;
    availabilityForm.elements.day.value = column.dataset.day;
    availabilityPopover.querySelector("[data-availability-time]").textContent = block
      ? `${availabilityTimeText(block.dataset.startTime)} – ${availabilityTimeText(block.dataset.endTime)}` : "";
    clearAvailabilityError();
    positionAvailabilityPopover();
  };
  const openAvailabilityPopover = (column, minute, block = null, focusTarget = null) => {
    if (!availabilityPopover || availabilitySaving) return;
    closeAvailabilityPopover(false);
    const start = block ? availabilityMinutes(block.dataset.startTime)
      : Math.max(0, Math.min(1435, Math.round(minute / 5) * 5));
    const end = block ? availabilityMinutes(block.dataset.endTime) : Math.min(start + 60, 1439);
    availabilitySelection = {
      column, block, anchorMinute: minute, mode: block ? "view" : "create",
      focusTarget: focusTarget || block || column.querySelector(".calendar-day-track"),
    };
    ["start_time", "end_time"].forEach((name, index) => {
      const input = availabilityForm.elements[name];
      input.value = formatTime(index ? end : start);
      input.dataset.lastAccepted = input.value;
      input.setCustomValidity("");
      input.classList.remove("time-input-invalid");
    });
    block?.classList.add("is-selected");
    if (!block) {
      const draft = document.createElement("span");
      draft.className = "calendar-draft-window";
      draft.style.top = `${start / 1440 * 100}%`;
      draft.style.height = `${(end - start) / 1440 * 100}%`;
      draft.setAttribute("aria-hidden", "true");
      column.querySelector(".calendar-day-track").appendChild(draft);
      availabilitySelection.draft = draft;
    }
    renderAvailabilitySelection();
    availabilityPopover.show();
    positionAvailabilityPopover();
    availabilityPopover.querySelector("[data-availability-close]").focus({preventScroll: true});
  };
  const recurringBlockAt = (column, minute) => availabilityBlocksForDay(column.dataset.day).find((block) => {
    const start = availabilityMinutes(block.dataset.startTime), end = availabilityMinutes(block.dataset.endTime);
    return start !== null && end !== null && start <= minute && minute < end;
  });
  weeklyCalendar?.addEventListener("click", (event) => {
    if (weeklyCalendar.dataset.availabilityEditable !== "true") return;
    const track = event.target.closest(".calendar-day-track");
    if (!track) return;
    const column = track.parentElement, bounds = track.getBoundingClientRect();
    const block = event.target.closest("[data-availability-window]");
    const minute = event.detail === 0 && block
      ? availabilityMinutes(block.dataset.startTime)
      : Math.max(0, Math.min(1439, (event.clientY - bounds.top) / bounds.height * 1440));
    // Masks have their own labels but never take ownership of the recurring window beneath.
    openAvailabilityPopover(column, minute, block || recurringBlockAt(column, minute));
  });
  weeklyCalendar?.querySelectorAll('.calendar-day-track[tabindex="0"]').forEach((track) => {
    let cursorMinute = 540;
    const cursor = document.createElement("span");
    cursor.className = "calendar-slot-cursor";
    cursor.setAttribute("aria-hidden", "true");
    track.appendChild(cursor);
    const moveCursor = () => {
      cursor.style.top = `${cursorMinute / 1440 * 100}%`;
      track.setAttribute("aria-label", `${dateLabel(track.parentElement)}, ${formatTime(cursorMinute)}. Enter to add weekly availability.`);
      const bounds = track.getBoundingClientRect(), viewport = weeklyCalendar.getBoundingClientRect();
      const y = bounds.top + cursorMinute / 1440 * bounds.height;
      if (y < viewport.top + 65) weeklyCalendar.scrollTop -= viewport.top + 65 - y;
      else if (y > viewport.bottom - 30) weeklyCalendar.scrollTop += y - viewport.bottom + 30;
    };
    track.addEventListener("pointerdown", (event) => {
      const bounds = track.getBoundingClientRect();
      cursorMinute = Math.max(0, Math.min(1435,
        Math.round((event.clientY - bounds.top) / bounds.height * 1440 / 5) * 5));
    });
    track.addEventListener("focus", moveCursor);
    track.addEventListener("keydown", (event) => {
      if (event.target !== track) return;
      const changes = {ArrowDown: 5, ArrowUp: -5, PageDown: 60, PageUp: -60};
      if (event.key in changes || event.key === "Home" || event.key === "End") {
        event.preventDefault();
        cursorMinute = event.key === "Home" ? 0 : event.key === "End" ? 1435
          : Math.max(0, Math.min(1435, cursorMinute + changes[event.key]));
        moveCursor();
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openAvailabilityPopover(track.parentElement, cursorMinute, recurringBlockAt(track.parentElement, cursorMinute), track);
      }
    });
  });
  availabilityPopover?.querySelector("[data-availability-close]").addEventListener("click", () => closeAvailabilityPopover());
  availabilityPopover?.querySelector("[data-availability-edit]").addEventListener("click", () => {
    const block = availabilitySelection.block;
    availabilityForm.elements.start_time.value = availabilityTimeText(block.dataset.startTime);
    availabilityForm.elements.end_time.value = availabilityTimeText(block.dataset.endTime);
    availabilityForm.querySelectorAll("[data-unified-time]").forEach((input) => {
      input.setCustomValidity("");
      input.classList.remove("time-input-invalid");
      input.dataset.lastAccepted = input.value;
    });
    availabilitySelection.mode = "edit";
    renderAvailabilitySelection();
    availabilityForm.elements.start_time.focus({preventScroll: true});
  });
  availabilityPopover?.querySelector("[data-availability-cancel]").addEventListener("click", () => {
    if (availabilitySaving) return;
    if (availabilitySelection.block) {
      closePicker(false);
      availabilitySelection.mode = "view";
      renderAvailabilitySelection();
      availabilityPopover.querySelector("[data-availability-edit]").focus({preventScroll: true});
    } else closeAvailabilityPopover();
  });
  availabilityPopover?.addEventListener("cancel", (event) => { event.preventDefault(); closeAvailabilityPopover(); });
  document.addEventListener("pointerdown", (event) => {
    if (availabilityPopover?.open && !availabilityPopover.contains(event.target)
      && !dialog?.contains(event.target) && !deleteDialog?.open) closeAvailabilityPopover(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !event.defaultPrevented && !deleteDialog?.open) closeAvailabilityPopover();
  });
  window.addEventListener("resize", positionAvailabilityPopover);
  window.addEventListener("scroll", positionAvailabilityPopover, true);
  const setAvailabilitySaving = (saving) => {
    availabilitySaving = saving;
    availabilityPopover.querySelectorAll("button, input").forEach((control) => { control.disabled = saving; });
    deleteDialog.querySelectorAll("button").forEach((button) => { button.disabled = saving; });
    availabilityPopover.setAttribute("aria-busy", String(saving));
  };
  const availabilityRequest = async (url, body) => {
    const response = await fetch(url, {
      method: "POST", body,
      headers: {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "The window could not be saved. Please try again.");
    return payload;
  };
  availabilityForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (availabilitySaving) return;
    if (activeTimeInput) closePicker(true);
    const start = parseTime(availabilityForm.elements.start_time.value);
    const end = parseTime(availabilityForm.elements.end_time.value);
    if (start === null || end === null || end <= start) {
      availabilityError.textContent = start === null || end === null
        ? "Enter valid start and end times." : "End time must be later than start time.";
      availabilityError.hidden = false;
      positionAvailabilityPopover();
      return;
    }
    const body = new FormData(availabilityForm), action = availabilityForm.action;
    clearAvailabilityError();
    setAvailabilitySaving(true);
    try {
      const payload = await availabilityRequest(action, body);
      const block = updateAvailabilityBlock(payload.entry);
      availabilitySelection.focusTarget = block;
      availabilityStatus.textContent = "Weekly availability saved.";
      setAvailabilitySaving(false);
      closeAvailabilityPopover();
    } catch (error) {
      availabilityError.textContent = error.message || "The window could not be saved. Please try again.";
      availabilityError.hidden = false;
      positionAvailabilityPopover();
    } finally { setAvailabilitySaving(false); }
  });
  availabilityPopover?.querySelector("[data-availability-delete]").addEventListener("click", () => {
    deleteDialog.querySelector("[data-availability-delete-message]").textContent =
      `${dateLabel(availabilitySelection.column)} · ${availabilityPopover.querySelector("[data-availability-time]").textContent}`;
    deleteError.hidden = true;
    showStableDialog(deleteDialog);
  });
  const cancelAvailabilityDelete = () => {
    if (availabilitySaving) return;
    closeStableDialog(deleteDialog);
    availabilityPopover.querySelector("[data-availability-delete]").focus({preventScroll: true});
  };
  deleteDialog?.querySelectorAll("[data-availability-delete-cancel]").forEach((button) =>
    button.addEventListener("click", cancelAvailabilityDelete));
  deleteDialog?.addEventListener("cancel", (event) => { event.preventDefault(); cancelAvailabilityDelete(); });
  deleteDialog?.querySelector("[data-availability-delete-confirm]").addEventListener("click", async () => {
    if (availabilitySaving || !availabilitySelection?.block) return;
    const {block, column} = availabilitySelection;
    const action = availabilityPopover.dataset.deleteUrlTemplate.replace("ENTRY_ID", block.dataset.entryId);
    const body = new URLSearchParams({return_to: availabilityForm.elements.return_to.value});
    setAvailabilitySaving(true);
    try {
      await availabilityRequest(action, body);
      block.remove();
      refreshCombinedAvailability(column.dataset.day);
      availabilitySelection.focusTarget = column.querySelector(".calendar-day-track");
      availabilityStatus.textContent = "Weekly availability deleted.";
      setAvailabilitySaving(false);
      closeStableDialog(deleteDialog);
      closeAvailabilityPopover();
    } catch (error) {
      deleteError.textContent = error.message || "The window could not be deleted. Please try again.";
      deleteError.hidden = false;
    } finally { setAvailabilitySaving(false); }
  });

  const planForm = document.querySelector("form[data-schedule-preview-url]");
  if (!planForm) return;
  const message = planForm.querySelector("[data-preview-message]");
  const resultsContainer = planForm.querySelector("[data-preview-results]");
  const watchedNames = ["start_date", "end_date", "duration_minutes", "minimum_attendance", "earliest_time", "latest_time"];
  let timer; let requestNumber = 0;
  const clearFieldErrors = () => watchedNames.forEach((name) => planForm.elements[name]?.removeAttribute("aria-invalid"));
  const showIssue = (issue) => {
    resultsContainer.replaceChildren();
    message.textContent = issue.message;
    if (issue.field && planForm.elements[issue.field]) planForm.elements[issue.field].setAttribute("aria-invalid", "true");
  };
  const previewIssue = () => {
    const fields = planForm.elements;
    const required = ["start_date", "end_date", "duration_minutes", "minimum_attendance"];
    if (required.some((name) => !fields[name]?.value)) return {message: "Complete the scheduling fields to preview matching times."};
    const duration = Number(fields.duration_minutes.value);
    const minimum = Number(fields.minimum_attendance.value);
    if (!Number.isInteger(duration) || duration < 1) return {field: "duration_minutes", message: "Meeting duration must be a positive whole number."};
    if (!Number.isInteger(minimum) || minimum < 1) return {field: "minimum_attendance", message: "Minimum attendance must be at least one."};
    if (minimum > Number(fields.minimum_attendance.max)) return {field: "minimum_attendance", message: "Minimum attendance cannot exceed the group size."};
    if (fields.end_date.value < fields.start_date.value) return {field: "end_date", message: "End date must be on or after the start date."};
    const days = (Date.parse(`${fields.end_date.value}T00:00:00Z`) - Date.parse(`${fields.start_date.value}T00:00:00Z`)) / 86400000;
    if (days > 366) return {field: "end_date", message: "Date range must be one year or less."};
    if (fields.earliest_time.value && fields.latest_time.value && fields.latest_time.value <= fields.earliest_time.value) return {field: "latest_time", message: "Latest allowed time must be later than earliest allowed time."};
    return null;
  };
  const renderResults = (results, diagnostic) => {
    resultsContainer.replaceChildren();
    if (!results.length) {
      if (diagnostic?.reason === "attendance_unmet") message.textContent = `The minimum is ${diagnostic.requested_attendance}/${diagnostic.total_members}, but at most ${diagnostic.maximum_attendance} can attend for the requested duration. Try lowering the minimum to ${diagnostic.maximum_attendance}, widening the dates, or adding availability. You can still save this plan.`;
      else if (diagnostic?.reason === "duration_unmet") message.textContent = `No full-duration time is available. A shorter ${diagnostic.best_shorter_minutes}-minute window can fit ${diagnostic.shorter_attendance}/${diagnostic.total_members} members. Try shortening the duration, widening the dates, or adding availability. You can still save this plan.`;
      else message.textContent = "No availability overlaps these requirements. Try changing the dates or allowed times, or adding availability. You can still save this plan.";
      return;
    }
    message.textContent = diagnostic?.reason === "duration_unmet"
      ? `No full-duration time meets these requirements. Showing shorter options; the best is ${diagnostic.best_shorter_minutes} minutes with ${diagnostic.shorter_attendance}/${diagnostic.total_members} available.`
      : "Preview only — save the plan before scheduling a time.";
    results.slice(0, 5).forEach((result, index) => {
      const card = document.createElement("article"); card.className = "preview-result";
      const title = document.createElement("strong"); title.textContent = `${result.day}, ${result.date}`;
      const time = document.createElement("span"); time.textContent = `${formatTime(Number(result.start.slice(0, 2)) * 60 + Number(result.start.slice(3))) } – ${formatTime(Number(result.end.slice(0, 2)) * 60 + Number(result.end.slice(3)))}`;
      const attendance = document.createElement("span"); attendance.textContent = `${result.attendance}/${result.group_size} available`;
      const explanation = document.createElement("span");
      const unavailable = result.group_size - result.attendance;
      explanation.textContent = unavailable ? `${unavailable} unavailable` : "Everyone available";
      const badge = document.createElement("span"); badge.className = result.exact_match ? "preview-badge" : "preview-badge alternative"; badge.textContent = index === 0 ? (result.exact_match ? "Best Match" : "Best shorter option") : result.exact_match ? "Exact match" : `Shorter option · ${result.available_duration_minutes} min`;
      card.append(title, time, attendance, explanation, badge); resultsContainer.append(card);
    });
  };
  const requestPreview = async () => {
    const thisRequest = ++requestNumber;
    clearFieldErrors();
    const issue = previewIssue();
    if (issue) { showIssue(issue); return; }
    message.textContent = "Calculating best times…";
    try {
      const response = await fetch(planForm.dataset.schedulePreviewUrl, { method: "POST", body: new URLSearchParams(new FormData(planForm)), headers: { "X-Requested-With": "fetch" } });
      const data = await response.json(); if (thisRequest !== requestNumber) return;
      if (!data.success) {
        const firstField = Object.keys(data.field_errors || {})[0];
        showIssue({field: firstField, message: data.field_errors?.[firstField] || data.errors?.[0] || "Enter valid scheduling requirements."});
        return;
      }
      renderResults(data.results, data.diagnostic);
      if (data.calendar_warnings?.length) message.textContent += " " + data.calendar_warnings.join(" ");
    } catch (_error) { if (thisRequest === requestNumber) { resultsContainer.replaceChildren(); message.textContent = "Preview is temporarily unavailable. You can still save the plan."; } }
  };
  watchedNames.forEach((name) => { const field = planForm.elements[name]; if (field) field.addEventListener("input", () => { window.clearTimeout(timer); timer = window.setTimeout(requestPreview, 400); }); });
  requestPreview();
});
