document.addEventListener("DOMContentLoaded", () => {
    const ticketPreviewModal = document.getElementById("ticketPreviewModal");
    const ticketPreviewContent = document.getElementById("ticketPreviewContent");
    const ticketPreviewTitle = document.getElementById("ticketPreviewTitle");
    let lastPreviewTrigger = null;

    function closeTicketPreview() {
        if (!ticketPreviewModal || !ticketPreviewContent) return;
        ticketPreviewModal.hidden = true;
        ticketPreviewContent.innerHTML = "";
        if (lastPreviewTrigger) lastPreviewTrigger.focus();
        lastPreviewTrigger = null;
    }

    function openTicketPreview(trigger) {
        if (!ticketPreviewModal || !ticketPreviewContent || !ticketPreviewTitle) return;

        const templateId = trigger.dataset.printTemplateId;
        const template = templateId ? document.getElementById(templateId) : null;
        if (!template) return;

        const widthMm = Number(template.dataset.printWidth || 72);
        const heightMm = Number(template.dataset.printHeight || 73);
        ticketPreviewTitle.textContent = trigger.dataset.ticketPreviewTitle || "Detalles";
        ticketPreviewContent.innerHTML = "";
        ticketPreviewContent.style.setProperty("--ticket-preview-width", `${widthMm}mm`);
        ticketPreviewContent.style.setProperty("--ticket-preview-height", `${heightMm}mm`);
        ticketPreviewContent.appendChild(template.content.cloneNode(true));
        ticketPreviewModal.hidden = false;
        lastPreviewTrigger = trigger;
    }

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

    document.querySelectorAll("[data-ticket-preview]").forEach((button) => {
        button.addEventListener("click", () => openTicketPreview(button));
    });

    document.querySelectorAll("[data-ticket-preview-close]").forEach((button) => {
        button.addEventListener("click", closeTicketPreview);
    });

    ticketPreviewModal?.addEventListener("click", (event) => {
        if (event.target === ticketPreviewModal) closeTicketPreview();
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && ticketPreviewModal && !ticketPreviewModal.hidden) {
            closeTicketPreview();
        }
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
