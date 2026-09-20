/* LeadPilot — кабинет: общий слой (этап 5, раздел 13 ТЗ).
   Без библиотек и inline-обработчиков (строгий CSP). Все изменения данных идут через
   JSON API тем же путём, что и у внешних клиентов, поэтому роли, изоляция компаний и
   аудит проверяются на сервере. Текст из ответов API выводится только через textContent. */
(function () {
  "use strict";

  var $ = function (selector, root) { return (root || document).querySelector(selector); };
  var $$ = function (selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); };

  /* ---------- Запросы к API ---------- */
  function api(method, url, body) {
    var options = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    return fetch(url, options).then(function (response) {
      if (response.status === 204) return { ok: true, status: 204, data: null };
      return response.json().catch(function () { return null; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    }, function () {
      return { ok: false, status: 0, data: null };
    });
  }

  var HAS_CYRILLIC = /[А-Яа-яЁё]/;

  /* Сообщение об ошибке для человека: русский текст сервера показываем как есть,
     технические англоязычные ответы заменяем понятной фразой. */
  function errorMessage(res) {
    if (res.status === 0) return "Нет связи с сервером. Проверьте интернет и повторите.";
    if (res.status === 401) return "Сессия истекла. Войдите заново.";
    if (res.status === 403) return (res.data && typeof res.data.detail === "string" && res.data.detail) || "Недостаточно прав для этого действия.";
    if (res.status === 404) return (res.data && typeof res.data.detail === "string" && HAS_CYRILLIC.test(res.data.detail) && res.data.detail) || "Не найдено: возможно, запись уже удалена.";
    if (res.status === 429) return "Слишком много попыток. Подождите немного и повторите.";
    if (res.status >= 500) return "Сервер не смог выполнить действие. Повторите чуть позже.";
    var detail = res.data && res.data.detail;
    if (typeof detail === "string") return HAS_CYRILLIC.test(detail) ? detail : "Проверьте введённые данные.";
    if (Array.isArray(detail) && detail.length) {
      var first = detail[0] || {};
      var msg = String(first.msg || "").replace(/^Value error, /, "");
      return HAS_CYRILLIC.test(msg) ? msg : "Проверьте введённые данные.";
    }
    return "Не удалось выполнить действие.";
  }

  function fieldErrors(res) {
    var out = {};
    if (res.status === 422 && res.data && Array.isArray(res.data.detail)) {
      res.data.detail.forEach(function (item) {
        var name = item.loc && item.loc[item.loc.length - 1];
        if (typeof name === "string") out[name] = String(item.msg || "").replace(/^Value error, /, "");
      });
    }
    return out;
  }

  /* ---------- Уведомления и подтверждение ---------- */
  function toast(text, kind) {
    var host = $("#toasts");
    if (!host) return;
    var node = document.createElement("div");
    node.className = "toast" + (kind === "error" ? " toast--error" : "");
    node.textContent = text;
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, kind === "error" ? 7000 : 3500);
  }

  function confirmDialog(text, okLabel) {
    var dialog = $("#confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(text));
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel || "Подтвердить";
    return new Promise(function (resolve) {
      dialog.addEventListener("close", function handler() {
        dialog.removeEventListener("close", handler);
        resolve(dialog.returnValue === "ok");
      });
      dialog.returnValue = "cancel";
      dialog.showModal();
    });
  }

  function setBusy(button, busy) {
    if (!button) return;
    if (busy) { button.setAttribute("aria-busy", "true"); button.disabled = true; }
    else { button.removeAttribute("aria-busy"); button.disabled = false; }
  }

  /* ---------- Значения форм ---------- */
  function convert(element) {
    var type = element.dataset.type;
    var value;
    if (element.type === "checkbox") return element.checked;
    value = element.value;
    if (typeof value === "string") value = value.trim();
    if (value === "") {
      if (element.dataset.empty === "null") return null;
      if (element.dataset.empty === "omit") return undefined;
    }
    if (type === "int") return value === "" ? null : parseInt(value, 10);
    return value;
  }

  function collect(form) {
    var data = {};
    Array.prototype.forEach.call(form.elements, function (element) {
      if (!element.name || element.disabled) return;
      if (element.type === "radio" && !element.checked) return;
      if (element.type === "submit" || element.type === "button") return;
      var value = convert(element);
      if (value !== undefined) data[element.name] = value;
    });
    return data;
  }

  function showFieldErrors(form, errors) {
    $$(".field-error", form).forEach(function (node) { node.remove(); });
    $$("[aria-invalid]", form).forEach(function (node) { node.removeAttribute("aria-invalid"); });
    Object.keys(errors).forEach(function (name) {
      var input = form.elements[name];
      if (!input || !input.setAttribute) return;
      input.setAttribute("aria-invalid", "true");
      var note = document.createElement("span");
      note.className = "field-error";
      note.textContent = HAS_CYRILLIC.test(errors[name]) ? errors[name] : "Проверьте это поле.";
      if (input.parentNode) input.parentNode.appendChild(note);
    });
  }

  function finish(target, res) {
    var mode = target.dataset.success || "";
    if (mode === "reload") { window.location.reload(); return; }
    if (mode === "redirect") {
      var to = (target.dataset.redirect || "/cabinet").replace("{id}", res.data && res.data.id != null ? res.data.id : "");
      window.location.assign(to);
      return;
    }
    if (mode.indexOf("message:") === 0) toast(mode.slice(8));
  }

  /* ---------- Формы: data-api-form ---------- */
  function submitForm(form, event) {
    event.preventDefault();
    var button = form.querySelector("[type=submit]");
    var status = $("[data-status]", form);
    if (status) status.textContent = "";
    showFieldErrors(form, {});

    var flow = form.dataset.flow;
    var body = collect(form);
    setBusy(button, true);

    var request;
    if (flow === "register") {
      request = api("POST", "/auth/register", body).then(function (res) {
        if (!res.ok) return res;
        return api("POST", "/auth/login", body);
      });
    } else {
      request = api(form.dataset.method || "POST", form.dataset.url, body);
    }
    request.then(function (res) {
      setBusy(button, false);
      if (res.ok) {
        if (flow === "register") { window.location.assign(form.dataset.redirect || "/cabinet"); return; }
        finish(form, res);
        return;
      }
      showFieldErrors(form, fieldErrors(res));
      var text = errorMessage(res);
      if (status) status.textContent = text; else toast(text, "error");
      if (res.status === 401 && flow !== "register" && form.dataset.url !== "/auth/login") window.location.assign("/login");
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target.closest && event.target.closest("form[data-api-form]");
    if (form) submitForm(form, event);
  });

  /* Ctrl+Enter в поле с data-submit-hotkey отправляет форму. */
  document.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target.matches && event.target.matches("[data-submit-hotkey]")) {
      var form = event.target.form;
      if (form) { event.preventDefault(); if (form.requestSubmit) form.requestSubmit(); }
    }
  });

  /* ---------- Списки выбора: data-api-change ---------- */
  document.addEventListener("focusin", function (event) {
    if (event.target.matches && event.target.matches("select[data-api-change]")) event.target.dataset.prev = event.target.value;
  });
  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select.matches || !select.matches("select[data-api-change]")) return;
    var body = {};
    body[select.dataset.field] = convert(select);
    var previous = select.dataset.prev;
    select.disabled = true;
    api(select.dataset.method || "PATCH", select.dataset.url, body).then(function (res) {
      select.disabled = false;
      if (res.ok) {
        select.dataset.prev = select.value;
        if (select.dataset.success === "reload") window.location.reload(); else toast("Сохранено");
      } else {
        select.value = previous != null ? previous : select.value;
        toast(errorMessage(res), "error");
      }
    });
  });

  /* ---------- Кнопки-действия: data-api-click ---------- */
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-api-click]");
    if (!button) return;
    event.preventDefault();
    var run = function () {
      setBusy(button, true);
      var body = button.dataset.body ? JSON.parse(button.dataset.body) : undefined;
      api(button.dataset.method || "POST", button.dataset.url, body).then(function (res) {
        setBusy(button, false);
        if (res.ok) finish(button, res); else toast(errorMessage(res), "error");
      });
    };
    if (button.dataset.confirm) {
      confirmDialog(button.dataset.confirm, button.dataset.confirmOk).then(function (ok) { if (ok) run(); });
    } else {
      run();
    }
  });

  /* ---------- Выход, приглашение, меню, время ---------- */
  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-logout]");
    if (target) {
      event.preventDefault();
      api("POST", "/auth/logout").then(function () { window.location.assign(target.dataset.redirect || "/login"); });
      return;
    }
    var accept = event.target.closest && event.target.closest("[data-accept-invite]");
    if (accept) {
      event.preventDefault();
      setBusy(accept, true);
      api("POST", "/invitations/accept", { token: accept.dataset.acceptInvite }).then(function (res) {
        setBusy(accept, false);
        if (res.ok) window.location.assign("/cabinet/" + res.data.business_id);
        else {
          var status = accept.parentNode.querySelector("[data-status]");
          if (status) status.textContent = errorMessage(res);
        }
      });
      return;
    }
    if (event.target.closest && event.target.closest("[data-reload]")) { window.location.reload(); return; }
    var toggle = event.target.closest && event.target.closest("[data-nav-toggle]");
    if (toggle) {
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (document.body.classList.contains("nav-open") && !(event.target.closest && event.target.closest(".rail"))) {
      document.body.classList.remove("nav-open");
    }
  });

  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches("[data-company-switch]")) {
      window.location.assign("/cabinet/" + encodeURIComponent(event.target.value));
    }
  });

  /* Время из UTC — в часовой пояс посетителя. Без JavaScript остаётся запасной вид (UTC). */
  function localizeTimes(root) {
    var now = new Date();
    $$("time[data-local]", root).forEach(function (node) {
      var date = new Date(node.getAttribute("datetime"));
      if (isNaN(date.getTime())) return;
      var sameYear = date.getFullYear() === now.getFullYear();
      var sameDay = date.toDateString() === now.toDateString();
      var options = sameDay ? { hour: "2-digit", minute: "2-digit" }
        : sameYear ? { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }
        : { day: "numeric", month: "short", year: "numeric" };
      node.textContent = date.toLocaleString("ru-RU", options);
      node.title = date.toLocaleString("ru-RU");
    });
  }

  /* ---------- Запуск ---------- */
  function ready() {
    localizeTimes(document);
    var log = $("#log");
    if (log) log.scrollTop = log.scrollHeight;
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready); else ready();

  window.LeadPilot = { api: api, toast: toast, errorMessage: errorMessage, confirmDialog: confirmDialog, setBusy: setBusy, collect: collect, showFieldErrors: showFieldErrors, fieldErrors: fieldErrors };
})();
