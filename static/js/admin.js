document.addEventListener("DOMContentLoaded", () => {
    const modal = document.getElementById("detailModal");
    const detailTitle = document.getElementById("detailTitle");
    const detailSubtitle = document.getElementById("detailSubtitle");
    const detailList = document.getElementById("detailList");
    const detailTotal = document.getElementById("detailTotal");
    const closeDetail = document.getElementById("closeDetail");

    function money(value) {
        return Number(value || 0).toLocaleString("es-MX", {
            style: "currency",
            currency: "MXN",
        });
    }

    function quantity(value) {
        return Number(value || 0).toLocaleString("es-MX", {
            minimumFractionDigits: 0,
            maximumFractionDigits: 3,
        });
    }

    document.querySelectorAll("[data-detail]").forEach((button) => {
        button.addEventListener("click", () => {
            const items = JSON.parse(button.dataset.items || "[]");
            detailTitle.textContent = `Pedido #${button.dataset.id}`;
            detailSubtitle.textContent = `${button.dataset.sucursal} · ${button.dataset.fecha}`;
            detailTotal.textContent = money(button.dataset.total);
            detailList.innerHTML = "";
            items.forEach((item) => {
                const row = document.createElement("article");
                row.className = "detail-item";
                row.innerHTML = `
                    <div>
                        <strong>${item.producto}</strong>
                        <div class="item-meta">${quantity(item.cantidad)} × ${money(item.precio_unitario)}</div>
                    </div>
                    <strong>${money(item.subtotal)}</strong>
                `;
                detailList.appendChild(row);
            });
            modal.hidden = false;
        });
    });

    document.querySelectorAll("[data-confirm-delete]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (!window.confirm("¿Eliminar este pedido del dashboard?")) {
                event.preventDefault();
            }
        });
    });

    closeDetail.addEventListener("click", () => {
        modal.hidden = true;
    });

    modal.addEventListener("click", (event) => {
        if (event.target === modal) modal.hidden = true;
    });
});
