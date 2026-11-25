"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const { request, adminHeaders, adminLoginUrl } = window.PortalApi;
  const base = "api/connections";
  let server = null;
  let source = null;
  let sourceIds = new Set();
  let confirmDelete = false;

  function status(id, message, error = false) {
    const target = $(id);
    target.textContent = message;
    target.classList.toggle("error", error);
  }
  async function api(path = "", method = "GET", data) {
    const options = method === "GET" ? {} : {
      method, headers: await adminHeaders(), body: JSON.stringify(data),
    };
    const response = await request(base + path, options);
    if (response.status === 204) return {};
    const body = await response.text();
    return body ? JSON.parse(body) : {};
  }
  function busy(form, value) {
    form.inert = value;
    form.setAttribute("aria-busy", String(value));
  }
  for (const dialog of document.querySelectorAll("dialog")) {
    const form = dialog.querySelector("form");
    for (const button of dialog.querySelectorAll("[data-close]")) {
      button.addEventListener("click", () => { if (!form.inert) dialog.close(); });
    }
    dialog.addEventListener("cancel", (event) => {
      if (form.inert) event.preventDefault();
    });
    dialog.addEventListener("close", () => form.reset());
  }

  function resourceList(id, items, empty, describe, edit) {
    const list = $(id);
    list.replaceChildren();
    if (!items.length) {
      const row = document.createElement("li");
      row.className = "empty";
      row.textContent = empty;
      list.append(row);
    }
    for (const item of items) {
      const [title, detail] = describe(item);
      const row = document.createElement("li");
      const content = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = title;
      const note = document.createElement("p");
      note.className = "detail";
      note.textContent = detail;
      content.append(name, note);
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.textContent = "Настроить";
      editButton.setAttribute("aria-label", "Настроить " + title);
      editButton.addEventListener("click", () => edit(item));
      row.append(content, editButton);
      list.append(row);
    }
  }

  async function refresh(workspace = false) {
    const info = await api();
    sourceIds = new Set(info.sources.map(item => item.id));
    if (workspace) {
      $("workspace-name").value = info.workspace.name || "";
      $("workspace-image").value = info.workspace.image || "";
      $("disable-workspace").hidden = !info.workspace.image;
    }
    resourceList("servers", info.servers, "Серверы MCP ещё не добавлены.",
      item => [item.name, item.url + " · Опубликовано инструментов: " + item.tools.length], openServer);
    resourceList("sources", info.sources, "Репозитории ещё не подключены.",
      item => [item.id, (item.kind === "git" ? "SSH" : "Maven") + " · " + item.url], openSource);
    $("panels").hidden = false;
    status("page-status", "");
  }
  async function images() {
    const button = $("refresh-images");
    button.disabled = true;
    status("workspace-status", "Проверяем образы Docker…");
    try {
      const { images } = await api("/images");
      const options = images.map(name => new Option(name, name));
      $("image-options").replaceChildren(new Option("Выберите образ…", ""), ...options);
      $("image-options").value = images.includes($("workspace-image").value) ? $("workspace-image").value : "";
      $("images-field").hidden = !images.length;
      status("workspace-status", images.length ? "" : "На Docker-хосте пока нет образов.");
    } catch (error) { status("workspace-status", error.message, true); }
    finally { button.disabled = false; }
  }
  $("image-options").addEventListener("change", () => {
    if ($("image-options").value) $("workspace-image").value = $("image-options").value;
  });
  $("refresh-images").addEventListener("click", images);

  async function saveWorkspace(disable) {
    const form = $("workspace-form");
    const name = disable ? "" : $("workspace-name").value.trim();
    const image = disable ? "" : $("workspace-image").value.trim();
    if (!disable && (!name || !image)) {
      status("workspace-status", "Укажите название и образ среды.", true);
      return;
    }
    busy(form, true);
    status("workspace-status", "Сохраняем среду…");
    try {
      await api("/workspace", "PUT", {name, image});
      await refresh(true);
      status("workspace-status", disable ? "Среда отключена." : "Среда сохранена.");
    } catch (error) { status("workspace-status", error.message, true); }
    finally { busy(form, false); }
  }
  $("workspace-form").addEventListener("submit", e => { e.preventDefault(); saveWorkspace(false); });
  $("disable-workspace").addEventListener("click", () => saveWorkspace(true));

  function showTools(names, enabled) {
    const list = $("tool-list");
    list.replaceChildren();
    for (const name of names) {
      const label = document.createElement("label");
      label.className = "check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = name;
      input.checked = enabled.includes(name);
      label.append(input, document.createTextNode(name));
      list.append(label);
    }
    if (!names.length) list.textContent = "Инструменты не выбраны.";
  }
  function selectedTools() {
    return Array.from($("tool-list").querySelectorAll("input:checked"), el => el.value);
  }
  function openServer(item) {
    server = item || null;
    const dialog = $("server-dialog");
    $("server-form").reset();
    for (const [id, hidden] of [["new-server-fields", !!server], ["server-fields", !server]]) {
      $(id).disabled = hidden;
      $(id).hidden = hidden;
    }
    $("server-title").textContent = item?.name || "Добавить сервер MCP";
    $("server-address").textContent = item?.url || "";
    $("token-state").textContent = item?.has_token ? "Сохранён" : "";
    $("clear-token-label").hidden = !item?.has_token;
    $("save-server").textContent = server ? "Сохранить" : "Добавить";
    showTools(item?.tools || [], item?.tools || []);
    status("server-status", "");
    if (!dialog.open) dialog.showModal();
  }
  function serverCredentials() {
    if ($("clear-token").checked) return {token:""};
    const token = $("server-token").value.trim();
    return token ? {token} : {};
  }
  function serverPath() { return "/servers/" + encodeURIComponent(server.id); }
  $("server-token").addEventListener("input", () => {
    if ($("server-token").value) $("clear-token").checked = false;
  });
  $("clear-token").addEventListener("change", () => {
    if ($("clear-token").checked) $("server-token").value = "";
  });
  $("discover-tools").addEventListener("click", async () => {
    busy($("server-form"), true);
    status("server-status", "Запрашиваем инструменты сервера…");
    try {
      const result = await api(serverPath() + "/discover", "POST", serverCredentials());
      showTools(result.tools, selectedTools());
      status("server-status", result.tools.length ? "Выберите инструменты и сохраните." : "Сервер не вернул инструментов чтения.");
    } catch (error) { status("server-status", error.message, true); }
    finally { busy($("server-form"), false); }
  });
  $("add-server").addEventListener("click", () => openServer(null));
  $("server-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = $("server-form");
    busy(form, true);
    status("server-status", "Сохраняем…");
    try {
      if (!server) {
        const name = $("server-name").value.trim(), url = $("server-url").value.trim();
        const {id} = await api("/servers", "POST", {name, url});
        openServer({id, name, url, tools:[]});
        status("server-status", "Сервер добавлен. Теперь можно найти его инструменты.");
        await refresh();
      } else {
        await api(serverPath(), "PUT", {...serverCredentials(), tools:selectedTools()});
        await refresh();
        $("server-dialog").close();
        status("page-status", "Инструменты сервера сохранены.");
      }
    } catch (error) { status("server-status", error.message, true); }
    finally { busy(form, false); }
  });

  function sourceFields() {
    const git = $("source-kind").value === "git";
    $("git-fields").hidden = $("git-fields").disabled = !git;
    $("maven-fields").hidden = $("maven-fields").disabled = git;
    $("source-url").placeholder = git ? "ssh://git@git.company.ru/payments/api.git" : "https://maven.company.ru/releases";
    $("ssh-state").textContent = source?.kind === "git" && source.has_secret ? "Сохранён" : "";
    $("password-state").textContent = source?.kind === "maven" && source.has_secret ? "Сохранён" : "";
  }
  function openSource(item) {
    source = item || null;
    confirmDelete = false;
    $("source-form").reset();
    $("source-title").textContent = item ? "Настроить источник" : "Добавить источник";
    $("source-id").value = item?.id || "";
    $("source-id").readOnly = !!item;
    $("source-kind").value = item?.kind || "git";
    $("source-url").value = item?.url || "";
    $("source-user").value = item?.username || "";
    $("source-retain").hidden = !item;
    $("delete-source").hidden = !item;
    $("delete-source").textContent = "Удалить источник";
    sourceFields();
    status("source-status", "");
    $("source-dialog").showModal();
  }
  $("source-kind").addEventListener("change", sourceFields);
  $("add-source").addEventListener("click", () => openSource(null));
  $("source-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = $("source-form");
    busy(form, true);
    status("source-status", "Сохраняем источник…");
    try {
      const id = $("source-id").value.trim();
      if (!source && sourceIds.has(id)) throw new Error("Такой идентификатор уже занят. Чтобы изменить подключение, откройте его из списка.");
      const data = {kind:$("source-kind").value, url:$("source-url").value.trim()};
      if (data.kind === "git") {
        const file = $("ssh-key").files[0];
        if (file) {
          if (!file.size || file.size > 64 * 1024) throw new Error("Выберите непустой файл ключа до 64 КБ.");
          data.secret = await file.text();
        }
        const hosts = $("known-hosts").value.trim();
        if (hosts) data.known_hosts = hosts;
      } else {
        data.username = $("source-user").value.trim();
        if ($("source-password").value) data.secret = $("source-password").value;
      }
      await api("/sources/" + encodeURIComponent(id), "PUT", data);
      await refresh();
      $("source-dialog").close();
      status("page-status", "Источник сохранён.");
    } catch (error) { status("source-status", error.message, true); }
    finally { busy(form, false); }
  });
  $("delete-source").addEventListener("click", async () => {
    if (!confirmDelete) {
      confirmDelete = true;
      $("delete-source").textContent = "Подтвердить удаление";
      status("source-status", "Агент потеряет доступ к этому источнику.");
      return;
    }
    busy($("source-form"), true);
    try {
      await api("/sources/" + encodeURIComponent(source.id), "DELETE", {});
      await refresh();
      $("source-dialog").close();
      status("page-status", "Источник удалён.");
    } catch (error) { status("source-status", error.message, true); }
    finally { busy($("source-form"), false); }
  });

  refresh(true).then(images).catch(async error => {
    status("page-status", error.status === 403 ? "Для управления подключениями войдите как администратор портала." : error.message, true);
    if ([401, 403].includes(error.status)) {
      $("login-link").href = await adminLoginUrl();
      $("login-link").hidden = false;
    }
  });
})();
