document.addEventListener("DOMContentLoaded", () => {
    const initialData = JSON.parse(document.getElementById("initial-data").textContent);
    const products = initialData.productos || [];
    let order = initialData.pedido || { items: [], total: "0.00" };
    let selectedProduct = products[0] || null;
    let quantityInput = "";
    let noticeTimer = null;

    const productButtons = [...document.querySelectorAll(".product-button")];
    const selectedName = document.getElementById("selectedName");
    const quantityDisplay = document.getElementById("quantityDisplay");
    const focusProduct = document.getElementById("focusProduct");
    const focusPrice = document.getElementById("focusPrice");
    const itemsList = document.getElementById("itemsList");
    const emptyState = document.getElementById("emptyState");
    const totalAmount = document.getElementById("totalAmount");
    const itemCount = document.getElementById("itemCount");
    const notice = document.getElementById("notice");
    const addButton = document.getElementById("addItem");
    const clearButton = document.getElementById("clearOrder");
    const confirmButton = document.getElementById("confirmOrder");
    const deleteSelectedButton = document.getElementById("deleteSelected");
    const modal = document.getElementById("successModal");
    const successText = document.getElementById("successText");

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

    function csrfToken() {
        const token = document.cookie
            .split("; ")
            .find((row) => row.startsWith("csrftoken="));
        return token ? decodeURIComponent(token.split("=")[1]) : "";
    }

    async function postJson(url, payload = {}) {
        const response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
            },
            body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({
            success: false,
            mensaje: "Respuesta inválida del servidor.",
        }));
        if (!response.ok || !data.success) {
            throw new Error(data.mensaje || `Error ${response.status}`);
        }
        return data;
    }

    function showNotice(message, type = "success") {
        notice.textContent = message;
        notice.classList.toggle("error", type === "error");
        notice.hidden = false;
        window.clearTimeout(noticeTimer);
        noticeTimer = window.setTimeout(() => {
            notice.hidden = true;
        }, 3200);
    }

    function setBusy(isBusy) {
        addButton.disabled = isBusy;
        clearButton.disabled = isBusy;
        confirmButton.disabled = isBusy;
        deleteSelectedButton.disabled = isBusy;
    }

    function selectProduct(productId) {
        selectedProduct = products.find((product) => Number(product.id) === Number(productId)) || products[0] || null;
        productButtons.forEach((button) => {
            button.classList.toggle("active", Number(button.dataset.productId) === Number(selectedProduct?.id));
        });
        selectedName.textContent = selectedProduct ? selectedProduct.nombre : "Producto";
        focusProduct.textContent = selectedProduct ? selectedProduct.nombre : "Selecciona producto";
        focusPrice.textContent = selectedProduct ? money(selectedProduct.precio) : "$0.00";
        renderQuantity();
    }

    function renderQuantity() {
        quantityDisplay.textContent = quantityInput || "0";
    }

    function renderPrices() {
        products.forEach((product) => {
            const label = document.querySelector(`[data-price-for="${product.id}"]`);
            if (label) label.textContent = money(product.precio);
        });
    }

    function renderOrder() {
        const items = order.items || [];
        itemsList.innerHTML = "";
        emptyState.hidden = items.length > 0;
        itemCount.textContent = String(items.length);
        totalAmount.textContent = money(order.total);

        items.forEach((item) => {
            const row = document.createElement("article");
            row.className = "order-item";
            row.innerHTML = `
                <div>
                    <strong>${item.producto}</strong>
                    <div class="item-meta">${quantity(item.cantidad)} × ${money(item.precio_unitario)}</div>
                    <div class="item-meta">Subtotal: ${money(item.subtotal)}</div>
                </div>
                <button class="item-remove" type="button" aria-label="Eliminar ${item.producto}" data-remove-item="${item.id}">×</button>
            `;
            itemsList.appendChild(row);
        });
    }

    function pressKey(key) {
        if (key === "DEL") {
            quantityInput = quantityInput.slice(0, -1);
        } else if (key === ".") {
            if (!quantityInput.includes(".")) quantityInput = quantityInput ? `${quantityInput}.` : "0.";
        } else if (/^\d$/.test(key)) {
            const candidate = quantityInput === "0" ? key : `${quantityInput}${key}`;
            if (candidate.length <= 8) quantityInput = candidate;
        }
        renderQuantity();
    }

    async function addSelected() {
        if (!selectedProduct) {
            showNotice("Selecciona un producto.", "error");
            return;
        }
        const value = Number(quantityInput);
        if (!Number.isFinite(value) || value <= 0) {
            showNotice("Captura una cantidad mayor a cero.", "error");
            return;
        }

        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/crear-item/", {
                producto_id: selectedProduct.id,
                cantidad: quantityInput,
            });
            order = data.pedido;
            quantityInput = "";
            renderQuantity();
            renderOrder();
            showNotice(data.mensaje);
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            setBusy(false);
        }
    }

    async function removeItem(itemId) {
        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/eliminar-item/", { item_id: itemId });
            order = data.pedido;
            renderOrder();
            showNotice("Producto eliminado.");
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            setBusy(false);
        }
    }

    async function removeSelectedProduct() {
        if (!selectedProduct) return;
        const item = (order.items || []).find((current) => Number(current.producto_id) === Number(selectedProduct.id));
        if (!item) {
            showNotice("Ese producto no está en el pedido.", "error");
            return;
        }
        await removeItem(item.id);
    }

    async function clearOrder() {
        if (!(order.items || []).length) {
            showNotice("El pedido ya está vacío.", "error");
            return;
        }
        if (!window.confirm("¿Limpiar el pedido actual?")) return;
        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/limpiar/");
            order = data.pedido;
            renderOrder();
            showNotice("Pedido limpio.");
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            setBusy(false);
        }
    }

    async function confirmOrder() {
        if (!(order.items || []).length) {
            showNotice("Agrega al menos un producto.", "error");
            return;
        }
        if (!window.confirm(`¿Confirmar pedido por ${money(order.total)}?`)) return;
        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/confirmar/");
            order = { items: [], total: "0.00" };
            renderOrder();
            successText.textContent = `Pedido #${data.pedido_id} por ${money(data.total)}.`;
            modal.hidden = false;
            window.setTimeout(() => {
                modal.hidden = true;
            }, 3000);
        } catch (error) {
            showNotice(error.message, "error");
        } finally {
            setBusy(false);
        }
    }

    productButtons.forEach((button) => {
        button.addEventListener("click", () => selectProduct(button.dataset.productId));
    });

    document.getElementById("keypad").addEventListener("click", (event) => {
        const button = event.target.closest("[data-key]");
        if (button) pressKey(button.dataset.key);
    });

    itemsList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-remove-item]");
        if (button) removeItem(button.dataset.removeItem);
    });

    addButton.addEventListener("click", addSelected);
    deleteSelectedButton.addEventListener("click", removeSelectedProduct);
    clearButton.addEventListener("click", clearOrder);
    confirmButton.addEventListener("click", confirmOrder);
    document.getElementById("closeSuccess").addEventListener("click", () => {
        modal.hidden = true;
    });

    renderPrices();
    selectProduct(selectedProduct?.id);
    renderOrder();
});
