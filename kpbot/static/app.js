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
