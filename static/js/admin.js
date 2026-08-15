document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-macro-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
            const detailId = button.getAttribute("aria-controls");
            const detailRow = detailId ? document.getElementById(detailId) : null;
            if (!detailRow) return;

            const expanded = button.getAttribute("aria-expanded") === "true";
            button.setAttribute("aria-expanded", String(!expanded));
            detailRow.hidden = expanded;
        });
    });

    document.querySelectorAll("[data-confirm-delete]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const label = form.dataset.deleteLabel || "pedido";
            if (!window.confirm(`¿Eliminar este ${label} del dashboard?`)) {
                event.preventDefault();
            }
        });
    });
});
