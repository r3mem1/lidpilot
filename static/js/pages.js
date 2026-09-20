/* LeadPilot — сценарии отдельных страниц кабинета (этап 5).
   Использует общий слой window.LeadPilot из app.js. */
(function () {
  "use strict";
  var LP = window.LeadPilot;
  if (!LP) return;
  var $ = function (selector, root) { return (root || document).querySelector(selector); };

  /* ---------- Диалоги ---------- */
  document.addEventListener("click", function (event) {
    if (event.target.closest && event.target.closest("[data-dialog-close]")) {
      var dialog = event.target.closest("dialog");
      var reload = event.target.closest("[data-reload-on-close]");
      if (dialog) dialog.close();
      if (reload) window.location.reload();
    }
  });

  /* ---------- Услуги: создание и правка (раздел 6.3) ---------- */
  var serviceDialog = $("#service-dialog");
  var serviceForm = serviceDialog && $("[data-service-form]", serviceDialog);
  var businessId = document.body.dataset.business;

  function openService(values) {
    serviceForm.reset();
    LP.showFieldErrors(serviceForm, {});
    $("[data-status]", serviceForm).textContent = "";
    serviceForm.elements.id.value = values.id || "";
    serviceForm.elements.name.value = values.name || "";
    serviceForm.elements.price.value = values.price ? String(parseFloat(values.price)) : "";
    serviceForm.elements.duration.value = values.duration || "";
    serviceForm.elements.description.value = values.description || "";
    serviceForm.elements.active.checked = values.active !== "false";
    $("#service-title").textContent = values.id ? "Изменить услугу" : "Новая услуга";
    serviceDialog.showModal();
    serviceForm.elements.name.focus();
  }

  document.addEventListener("click", function (event) {
    if (!serviceForm) return;
    if (event.target.closest("[data-service-new]")) openService({});
    var edit = event.target.closest("[data-service-edit]");
    if (edit) openService(edit.dataset);
  });

  if (serviceForm) {
    serviceForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var status = $("[data-status]", serviceForm);
      var id = serviceForm.elements.id.value;
      var price = serviceForm.elements.price.value.replace(",", ".").replace(/\s/g, "");
      var duration = serviceForm.elements.duration.value.trim();
      var body = {
        name: serviceForm.elements.name.value.trim(),
        price: price,
        duration: duration === "" ? null : parseInt(duration, 10),
        description: serviceForm.elements.description.value.trim() || null,
        active: serviceForm.elements.active.checked
      };
      if (!body.name || price === "" || isNaN(Number(price))) {
        status.textContent = "Укажите название и цену числом, например 1500.";
        return;
      }
      var button = $("[type=submit]", serviceForm);
      LP.setBusy(button, true);
      var request = id ? LP.api("PUT", "/services/" + id, body) : LP.api("POST", "/businesses/" + businessId + "/services", body);
      request.then(function (res) {
        LP.setBusy(button, false);
        if (res.ok) { window.location.reload(); return; }
        LP.showFieldErrors(serviceForm, LP.fieldErrors(res));
        status.textContent = LP.errorMessage(res);
      });
    });
  }

  /* ---------- Приглашение сотрудника (раздел 13) ---------- */
  var inviteDialog = $("#invite-dialog");
  var inviteForm = inviteDialog && $("[data-invite-form]", inviteDialog);
  if (inviteForm) {
    document.addEventListener("click", function (event) {
      if (event.target.closest("[data-invite-new]")) {
        inviteForm.reset();
        $("[data-invite-step=form]", inviteForm).hidden = false;
        $("[data-invite-step=done]", inviteForm).hidden = true;
        $("[data-status]", inviteForm).textContent = "";
        inviteDialog.showModal();
        inviteForm.elements.email.focus();
      }
      if (event.target.closest("[data-copy-link]")) {
        var field = $("[data-invite-link]", inviteForm);
        field.select();
        var done = function () { LP.toast("Ссылка скопирована"); };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(field.value).then(done, function () { document.execCommand("copy"); done(); });
        else { document.execCommand("copy"); done(); }
      }
    });
    inviteForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var status = $("[data-status]", inviteForm);
      var button = $("[type=submit]", inviteForm);
      LP.setBusy(button, true);
      LP.api("POST", inviteForm.dataset.url, { email: inviteForm.elements.email.value.trim(), role: inviteForm.elements.role.value }).then(function (res) {
        LP.setBusy(button, false);
        if (!res.ok) { status.textContent = LP.errorMessage(res); return; }
        $("[data-invite-link]", inviteForm).value = res.data.invite_url;
        $("[data-invite-step=form]", inviteForm).hidden = true;
        $("[data-invite-step=done]", inviteForm).hidden = false;
      });
    });
  }

  /* ---------- Проверка ответа AI (раздел 13: AI) ---------- */
  var previewForm = $("[data-ai-preview]");
  if (previewForm) {
    previewForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var text = previewForm.elements.text.value.trim();
      var status = $("[data-status]", previewForm);
      if (!text) { status.textContent = "Введите сообщение клиента."; return; }
      status.textContent = "Ассистент думает…";
      var button = $("[type=submit]", previewForm);
      LP.setBusy(button, true);
      LP.api("POST", previewForm.dataset.url, { text: text }).then(function (res) {
        LP.setBusy(button, false);
        status.textContent = "";
        if (!res.ok) { status.textContent = LP.errorMessage(res); return; }
        var d = res.data;
        var box = $("[data-preview-result]");
        box.hidden = false;
        $("[data-preview-decision]", box).textContent = d.decision === "SEND"
          ? "Ассистент ответил бы так:"
          : "Ассистент передал бы диалог вам";
        var reasons = {
          HOT_LEAD_CONFIRMATION: "запись требует подтверждения человеком", COMPLAINT: "жалоба клиента", MISSING_DATA: "не хватает данных для ответа",
          AMBIGUOUS_REQUEST: "неясный запрос", ACTION_NOT_ALLOWED: "просьба вне прав ассистента", EXTERNAL_API_ERROR: "сбой AI-сервиса",
          VALIDATION_FAILED: "ответ не прошёл проверку на выдуманные данные", SPAM_SUSPECTED: "похоже на спам", AUTO_REPLY_DISABLED: "автоответы отключены"
        };
        $("[data-preview-reply]", box).textContent = d.decision === "SEND"
          ? d.reply
          : "Причина: " + (reasons[d.escalation_reason] || d.escalation_reason || "нужен человек") + ".";
        var meta = "Намерение: " + d.intent + ", приоритет: " + d.priority + ".";
        if (d.model) meta += " Модель: " + d.model + (d.latency_ms != null ? ", " + d.latency_ms + " мс." : ".");
        $("[data-preview-meta]", box).textContent = meta;
      });
    });
  }

  /* ---------- Новые сообщения: тихий опрос вместо перезагрузки страницы ---------- */
  var inbox = $("[data-poll]");
  if (inbox) {
    var last = parseInt(inbox.dataset.lastMessage || "0", 10);
    setInterval(function () {
      if (document.hidden) return;
      LP.api("GET", inbox.dataset.poll).then(function (res) {
        if (res.ok && res.data && res.data.last_message_id > last) {
          var banner = $("[data-newer]");
          if (banner) banner.hidden = false;
        }
      });
    }, 12000);
  }
})();
