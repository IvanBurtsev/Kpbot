// Перетаскивание файлов: работает как дополнение к обычному input.
(function () {
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector('input[type="file"]');
    if (!input) return;

    var name = zone.querySelector(".dz-file");
    function showFile() {
      var file = input.files && input.files[0];
      if (!file) return;
      if (!name) {
        name = document.createElement("span");
        name.className = "dz-file small";
        zone.appendChild(name);
      }
      var size = file.size > 1024 * 1024
        ? (file.size / 1024 / 1024).toFixed(1) + " МБ"
        : Math.max(1, Math.round(file.size / 1024)) + " КБ";
      name.textContent = file.name + " · " + size;
      zone.classList.add("has-file");
    }

    ["dragenter", "dragover"].forEach(function (event) {
      zone.addEventListener(event, function (e) {
        e.preventDefault();
        zone.classList.add("drag");
      });
    });
    ["dragleave", "dragend", "drop"].forEach(function (event) {
      zone.addEventListener(event, function (e) {
        e.preventDefault();
        zone.classList.remove("drag");
      });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        showFile();
      }
    });
    input.addEventListener("change", showFile);
    showFile();
  });
})();

// Поиск позиции прайса в карточке заявки. Сервер отдаёт только топ-20
// совпадений, поэтому страница не зависит от размера прайса: раньше в каждой
// строке лежал <select> со всеми 10 000 позициями (десятки мегабайт HTML).
(function () {
  document.querySelectorAll("input[data-sku-search]").forEach(function (input) {
    var list = document.getElementById(input.getAttribute("list"));
    if (!list) return;
    var timer = null;
    var lastQuery = "";

    input.addEventListener("input", function () {
      var query = input.value.trim();
      if (query.length < 2 || query === lastQuery) return;
      clearTimeout(timer);
      timer = setTimeout(function () {
        lastQuery = query;
        fetch("/ui/prices/search?q=" + encodeURIComponent(query) + "&limit=20", {
          headers: { Accept: "application/json" },
          credentials: "same-origin"
        })
          .then(function (response) {
            return response.ok ? response.json() : [];
          })
          .then(function (items) {
            list.innerHTML = "";
            items.forEach(function (item) {
              var option = document.createElement("option");
              option.value = item.sku;
              option.label = item.name + " (" + item.unit + ") — " + item.price;
              list.appendChild(option);
            });
          })
          .catch(function () {
            /* подсказки не критичны: поле остаётся обычным вводом */
          });
      }, 180);
    });
  });
})();

// Подтверждение опасных действий. Inline-обработчики (onsubmit="...") запрещены
// политикой CSP (script-src 'self'), поэтому подтверждение живёт здесь: без него
// «Заблокировать пользователя» срабатывало сразу по клику.
(function () {
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var question = form.getAttribute("data-confirm");
      if (question && !window.confirm(question)) {
        event.preventDefault();
      }
    });
  });
})();
