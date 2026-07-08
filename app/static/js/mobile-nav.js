(function () {
    function getPanel(btn) {
        var id = btn.getAttribute("aria-controls");
        return id ? document.getElementById(id) : null;
    }

    function closeAll() {
        document.querySelectorAll('[data-nav-toggle][aria-expanded="true"]').forEach(function (btn) {
            btn.setAttribute("aria-expanded", "false");
            var panel = getPanel(btn);
            if (panel) panel.classList.remove("is-open");
        });
        document.querySelectorAll("[data-nav-overlay]").forEach(function (overlay) {
            overlay.classList.remove("is-open");
        });
    }

    function openFor(btn) {
        closeAll();
        btn.setAttribute("aria-expanded", "true");
        var panel = getPanel(btn);
        if (panel) panel.classList.add("is-open");
        document.querySelectorAll("[data-nav-overlay]").forEach(function (overlay) {
            overlay.classList.add("is-open");
        });
    }

    document.addEventListener("click", function (event) {
        var toggleBtn = event.target.closest("[data-nav-toggle]");
        if (toggleBtn) {
            var isOpen = toggleBtn.getAttribute("aria-expanded") === "true";
            if (isOpen) {
                closeAll();
            } else {
                openFor(toggleBtn);
            }
            return;
        }

        if (event.target.closest("[data-nav-overlay]")) {
            closeAll();
            return;
        }

        document.querySelectorAll('[data-nav-toggle][aria-expanded="true"]').forEach(function (btn) {
            var panel = getPanel(btn);
            if (panel && !panel.contains(event.target)) {
                closeAll();
            }
        });
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeAll();
        }
    });
})();
