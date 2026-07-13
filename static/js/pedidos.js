document.addEventListener("DOMContentLoaded", () => {
    const initialData = JSON.parse(document.getElementById("initial-data").textContent);
    const products = initialData.productos || [];
    let order = initialData.pedido || { items: [], total: "0.00" };
    let selectedProduct = products[0] || null;
    let selectedItemId = null;
    let quantityInput = "";
    let replaceOnNextKey = false;
    let noticeTimer = null;

    const orderShell = document.querySelector(".order-shell");
    const productButtons = [...document.querySelectorAll(".product-button")];
    const selectedName = document.getElementById("selectedName");
    const quantityDisplay = document.getElementById("quantityDisplay");
    const calculatorDisplay = document.querySelector(".calculator-display");
    const focusProduct = document.getElementById("focusProduct");
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
    const mobilePanelTabs = [...document.querySelectorAll("[data-mobile-panel]")];

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

    function editableQuantity(value) {
        const numberValue = Number(value || 0);
        return Number.isFinite(numberValue) && numberValue > 0 ? String(numberValue) : "";
    }

    function productById(productId) {
        return products.find((product) => Number(product.id) === Number(productId)) || null;
    }

    function itemById(itemId) {
        return (order.items || []).find((item) => Number(item.id) === Number(itemId)) || null;
    }

    function itemForProduct(productId) {
        return (order.items || []).find((item) => Number(item.producto_id) === Number(productId)) || null;
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
            mensaje: "Respuesta invalida del servidor.",
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

    function setMobilePanel(panelName) {
        orderShell.classList.toggle("summary-open", panelName === "summary");
    }

    function renderQuantity() {
        quantityDisplay.textContent = quantityInput || "0";
    }

    function setQuantityConfirmed(isConfirmed) {
        calculatorDisplay.classList.toggle("confirmed", isConfirmed);
    }

    function refreshSelectedState() {
        productButtons.forEach((button) => {
            button.classList.toggle(
                "active",
                Number(button.dataset.productId) === Number(selectedProduct?.id),
            );
        });

        itemsList.querySelectorAll("[data-item-id]").forEach((row) => {
            row.classList.toggle("active", Number(row.dataset.itemId) === Number(selectedItemId));
            row.setAttribute("aria-pressed", String(Number(row.dataset.itemId) === Number(selectedItemId)));
        });

        selectedName.textContent = selectedProduct ? selectedProduct.nombre : "Producto";
        focusProduct.textContent = selectedProduct ? selectedProduct.nombre : "Selecciona producto";
        renderQuantity();
    }

    function selectProduct(productId, syncExisting = true) {
        selectedProduct = productById(productId) || products[0] || null;
        const existingItem = selectedProduct ? itemForProduct(selectedProduct.id) : null;
        selectedItemId = existingItem ? existingItem.id : null;

        if (syncExisting && existingItem) {
            quantityInput = editableQuantity(existingItem.cantidad);
            replaceOnNextKey = true;
        } else if (!existingItem) {
            quantityInput = "";
            replaceOnNextKey = false;
        }

        setQuantityConfirmed(false);
        refreshSelectedState();
    }

    function selectItem(itemId) {
        const item = itemById(itemId);
        if (!item) return;

        selectedItemId = item.id;
        selectedProduct = productById(item.producto_id) || selectedProduct;
        quantityInput = editableQuantity(item.cantidad);
        replaceOnNextKey = true;
        setQuantityConfirmed(false);
        refreshSelectedState();
    }

    function renderOrder() {
        const items = order.items || [];
        if (selectedItemId && !items.some((item) => Number(item.id) === Number(selectedItemId))) {
            selectedItemId = null;
        }

        itemsList.innerHTML = "";
        emptyState.hidden = items.length > 0;
        itemCount.textContent = String(items.length);
        totalAmount.textContent = money(order.total);

        items.forEach((item) => {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "order-item";
            row.dataset.itemId = item.id;
            row.setAttribute("aria-label", `Seleccionar ${item.producto}`);

            const content = document.createElement("div");
            const name = document.createElement("strong");
            const meta = document.createElement("div");
            name.textContent = item.producto;
            meta.className = "item-meta";
            meta.textContent = `Cantidad: ${quantity(item.cantidad)}`;
            content.append(name, meta);
            row.append(content);
            itemsList.appendChild(row);
        });

        refreshSelectedState();
    }

    function pressKey(key) {
        setQuantityConfirmed(false);
        if (key === "DEL") {
            quantityInput = replaceOnNextKey ? "" : quantityInput.slice(0, -1);
            replaceOnNextKey = false;
            renderQuantity();
            return;
        }

        if (replaceOnNextKey) {
            quantityInput = "";
            replaceOnNextKey = false;
        }

        if (key === ".") {
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
            const savedItem = itemForProduct(selectedProduct.id);
            selectedItemId = savedItem ? savedItem.id : null;
            quantityInput = savedItem ? editableQuantity(savedItem.cantidad) : "";
            replaceOnNextKey = true;
            renderOrder();
            setQuantityConfirmed(true);
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
            selectedItemId = null;
            quantityInput = "";
            replaceOnNextKey = false;
            setQuantityConfirmed(false);
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
        const item = selectedItemId ? itemById(selectedItemId) : itemForProduct(selectedProduct.id);
        if (!item) {
            showNotice("Ese producto no esta en el pedido.", "error");
            return;
        }
        await removeItem(item.id);
    }

    async function clearOrder() {
        if (!(order.items || []).length) {
            showNotice("El pedido ya esta vacio.", "error");
            return;
        }
        if (!window.confirm("Limpiar el pedido actual?")) return;
        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/limpiar/");
            order = data.pedido;
            selectedItemId = null;
            quantityInput = "";
            replaceOnNextKey = false;
            setQuantityConfirmed(false);
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
        if (!window.confirm(`Confirmar pedido por ${money(order.total)}?`)) return;
        try {
            setBusy(true);
            const data = await postJson("/api/pedidos/confirmar/");
            order = { items: [], total: "0.00" };
            selectedItemId = null;
            quantityInput = "";
            replaceOnNextKey = false;
            setQuantityConfirmed(false);
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

    mobilePanelTabs.forEach((button) => {
        button.addEventListener("click", () => setMobilePanel(button.dataset.mobilePanel));
    });

    document.getElementById("keypad").addEventListener("click", (event) => {
        const button = event.target.closest("[data-key]");
        if (button) pressKey(button.dataset.key);
    });

    itemsList.addEventListener("click", (event) => {
        const row = event.target.closest("[data-item-id]");
        if (row) selectItem(row.dataset.itemId);
    });

    addButton.addEventListener("click", addSelected);
    deleteSelectedButton.addEventListener("click", removeSelectedProduct);
    clearButton.addEventListener("click", clearOrder);
    confirmButton.addEventListener("click", confirmOrder);
    document.getElementById("closeSuccess").addEventListener("click", () => {
        modal.hidden = true;
    });

    selectProduct(selectedProduct?.id);
    renderOrder();
});
