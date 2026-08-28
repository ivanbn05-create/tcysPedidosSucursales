document.addEventListener("DOMContentLoaded", () => {
    const initialData = JSON.parse(document.getElementById("initial-data").textContent);
    const products = initialData.productos || [];
    const apiUrls = initialData.api_urls || {};
    const isAdminOrder = Boolean(initialData.admin_order_mode);
    const selectedSucursalId = initialData.sucursal_id || null;
    let order = initialData.pedido || { items: [], total: "0.00" };
    let dailyProgress = initialData.progreso_diario || { cantidad: 0, maximo: 5 };
    let schedule = initialData.horario || { aplica: false, dentro_horario: true };
    let selectedProduct = products[0] || null;
    let selectedItemId = null;
    let quantityInput = "";
    let replaceOnNextKey = false;
    let noticeTimer = null;
    let resultModalTimer = null;

    const orderShell = document.querySelector(".order-shell");
    const productButtons = [...document.querySelectorAll(".product-button")];
    const selectedName = document.getElementById("selectedName");
    const selectedUnit = document.getElementById("selectedUnit");
    const quantityDisplay = document.getElementById("quantityDisplay");
    const calculatorPanel = document.querySelector(".calculator-panel");
    const calculatorDisplay = document.querySelector(".calculator-display");
    const calculatorControls = document.getElementById("calculatorControls");
    const keyboardToggle = document.getElementById("keyboardToggle");
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
    const successTitle = document.getElementById("successTitle");
    const successText = document.getElementById("successText");
    const closeSuccess = document.getElementById("closeSuccess");
    const confirmModal = document.getElementById("confirmModal");
    const confirmText = document.getElementById("confirmText");
    const confirmAccept = document.getElementById("confirmAccept");
    const confirmCancel = document.getElementById("confirmCancel");
    const mobilePanelTabs = [...document.querySelectorAll("[data-mobile-panel]")];
    const dailyOrderProgress = document.getElementById("dailyOrderProgress");
    const dailyOrderCount = document.getElementById("dailyOrderCount");
    const dailyOrderLimitMessage = document.getElementById("dailyOrderLimitMessage");
    const dailySegments = [...document.querySelectorAll("[data-daily-segment]")];

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

    function productTicketName(product) {
        return product?.nombre_ticket || product?.nombre || "Producto";
    }

    function productUnit(product) {
        return product?.unidad || "PZA";
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

    function trace(evento, extra = {}, intentoId = "") {
        // Cola durable: sobrevive recargas y recupera los eventos cuando vuelve
        // la red. El listener de captura vive en client_session.js.
        try {
            if (window.clientAudit) {
                window.clientAudit.record(evento, extra, intentoId);
                return;
            }
            if (!apiUrls.log_cliente) return;
            fetch(apiUrls.log_cliente, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                },
                body: JSON.stringify({ evento, detalle: extra, intento_id: intentoId, ua: navigator.userAgent }),
                keepalive: true,
            }).catch(() => {});
        } catch (error) {
            /* nunca romper la UI por telemetria */
        }
    }

    async function postJson(url, payload = {}, intentoId = "") {
        const requestPayload =
            isAdminOrder && selectedSucursalId
                ? { ...payload, sucursal_id: selectedSucursalId }
                : payload;

        // Sin timeout, un fetch colgado (cold start de Render, red movil mala)
        // deja los cuatro botones deshabilitados de forma indefinida.
        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 75000);

        let response;
        try {
            response = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                    "X-Client-Device": window.clientAudit?.deviceId || "",
                    "X-Order-Attempt-ID": intentoId,
                },
                body: JSON.stringify(requestPayload),
                signal: controller.signal,
            });
        } catch (error) {
            if (error.name === "AbortError") {
                throw new Error("El servidor tardo demasiado. Revisa tu conexion e intenta de nuevo.");
            }
            throw new Error("Sin conexion con el servidor. Intenta de nuevo.");
        } finally {
            window.clearTimeout(timeoutId);
        }

        // Sesion caducada: el fetch sigue el 302 y devuelve el HTML del login.
        if (response.redirected && response.url.includes("/login")) {
            throw new Error("Tu sesion expiro. Vuelve a iniciar sesion.");
        }

        const data = await response.json().catch(() => ({
            success: false,
            mensaje: "Respuesta invalida del servidor.",
        }));
        if (response.status === 401 && data.codigo === "sesion_reemplazada") {
            window.location.assign("/login/?sesion=reemplazada");
        }
        if (!response.ok || !data.success) {
            const error = new Error(data.mensaje || `Error ${response.status}`);
            error.status = response.status;
            error.codigo = data.codigo;
            error.horario = data.horario;
            error.progresoDiario = data.progreso_diario;
            throw error;
        }
        return data;
    }

    function showNotice(message, type = "success") {
        notice.textContent = message;
        notice.classList.toggle("error", type === "error");
        notice.hidden = false;
        window.clearTimeout(noticeTimer);
        if (type !== "error") {
            noticeTimer = window.setTimeout(() => {
                notice.hidden = true;
            }, 3200);
        }
    }

    function showResultModal(title, message, type = "success") {
        window.clearTimeout(resultModalTimer);
        successTitle.textContent = title;
        successText.textContent = message;
        modal.classList.toggle("error", type === "error");
        closeSuccess.textContent = type === "error" ? "Cerrar" : "Listo";
        modal.hidden = false;
        closeSuccess?.focus();
        if (type !== "error") {
            notice.hidden = true;
            resultModalTimer = window.setTimeout(() => {
                modal.hidden = true;
            }, 3000);
        }
    }

    async function refreshScheduleStatus() {
        if (!schedule.aplica || !apiUrls.horarios) return;
        try {
            const response = await fetch(apiUrls.horarios, {
                headers: { Accept: "application/json" },
                cache: "no-store",
            });
            if (!response.ok) return;
            schedule = { ...schedule, ...(await response.json()) };
        } catch (error) {
            // Conserva el ultimo estado conocido; el backend vuelve a validarlo.
        }
    }

    let pendingConfirm = null;
    let isKeyboardCollapsed = false;

    function askConfirm(message) {
        // Fallback: si el modal no existe en el DOM, no bloqueamos la accion.
        if (!confirmModal || !confirmText) return Promise.resolve(true);

        // Si quedara un dialogo abierto, lo cerramos como "cancelar".
        if (pendingConfirm) pendingConfirm(false);

        confirmText.textContent = message;
        confirmModal.hidden = false;

        return new Promise((resolve) => {
            pendingConfirm = (answer) => {
                confirmModal.hidden = true;
                pendingConfirm = null;
                resolve(answer);
            };
        });
    }

    let busyGuard = null;
    let isBusy = false;

    function dailyLimitReached() {
        return Number(dailyProgress.cantidad || 0) >= Number(dailyProgress.maximo || 5);
    }

    function applyActionAvailability() {
        [clearButton, deleteSelectedButton].forEach((button) => {
            if (button) button.disabled = isBusy;
        });
        [addButton, confirmButton].forEach((button) => {
            if (button) button.disabled = isBusy || dailyLimitReached();
        });
    }

    function renderDailyProgress(progress = dailyProgress) {
        dailyProgress = { ...dailyProgress, ...progress };
        const count = Number(dailyProgress.cantidad || 0);
        const maximum = Number(dailyProgress.maximo || 5);
        if (dailyOrderCount) dailyOrderCount.textContent = String(count);
        if (dailyOrderProgress) {
            dailyOrderProgress.setAttribute(
                "aria-label",
                `${count} de ${maximum} pedidos realizados hoy`,
            );
            dailyOrderProgress.classList.toggle("limit-reached", dailyLimitReached());
        }
        dailySegments.forEach((segment) => {
            segment.classList.toggle("filled", Number(segment.dataset.dailySegment) <= count);
        });
        if (dailyOrderLimitMessage) dailyOrderLimitMessage.hidden = !dailyLimitReached();
        applyActionAvailability();
    }

    function setBusy(nextBusy) {
        isBusy = nextBusy;
        applyActionAvailability();
        window.clearTimeout(busyGuard);
        if (nextBusy) {
            // Red de seguridad: si algo se cuelga sin pasar por el finally,
            // los botones no se quedan bloqueados para siempre.
            busyGuard = window.setTimeout(() => {
                setBusy(false);
                showNotice("La operacion tardo demasiado. Intenta de nuevo.", "error");
            }, 90000);
        }
    }

    function setMobilePanel(panelName) {
        orderShell.classList.toggle("summary-open", panelName === "summary");
    }

    function setKeyboardCollapsed(nextCollapsed) {
        isKeyboardCollapsed = nextCollapsed;
        orderShell?.classList.toggle("keyboard-collapsed", nextCollapsed);
        calculatorPanel?.classList.toggle("keyboard-collapsed", nextCollapsed);
        if (calculatorControls) calculatorControls.hidden = nextCollapsed;

        if (keyboardToggle) {
            const label = nextCollapsed ? "Mostrar teclado" : "Ocultar teclado";
            keyboardToggle.setAttribute("aria-expanded", String(!nextCollapsed));
            keyboardToggle.setAttribute("aria-label", label);
            keyboardToggle.title = label;
        }
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

        selectedName.textContent = selectedProduct ? productTicketName(selectedProduct) : "Producto";
        selectedUnit.textContent = selectedProduct ? productUnit(selectedProduct) : "PZA";
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
        setKeyboardCollapsed(false);
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
        setKeyboardCollapsed(false);
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
            meta.textContent = `Cantidad: ${quantity(item.cantidad)} ${item.unidad}`;
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
            const data = await postJson(apiUrls.crear_item || "/api/pedidos/crear-item/", {
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
            if (error.progresoDiario) renderDailyProgress(error.progresoDiario);
            showNotice(error.message, "error");
        } finally {
            setBusy(false);
        }
    }

    async function removeItem(itemId) {
        try {
            setBusy(true);
            const data = await postJson(apiUrls.eliminar_item || "/api/pedidos/eliminar-item/", {
                item_id: itemId,
            });
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
        trace("limpiar_click", { items: (order.items || []).length });
        if (!(order.items || []).length) {
            showNotice("El pedido ya esta vacio.", "error");
            return;
        }
        const aceptado = await askConfirm("¿Limpiar el pedido actual?");
        trace("limpiar_respuesta", { aceptado });
        if (!aceptado) return;
        try {
            setBusy(true);
            const data = await postJson(apiUrls.limpiar_pedido || "/api/pedidos/limpiar/");
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
        const intentoId = window.__pedidoConfirmAttemptId
            || window.clientAudit?.newAttemptId?.()
            || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        window.__pedidoConfirmAttemptId = intentoId;
        trace("confirmar_click", { items: (order.items || []).length }, intentoId);
        if (!(order.items || []).length) {
            showNotice("Agrega al menos un producto.", "error");
            return;
        }
        if (schedule.aplica && schedule.dentro_horario === false) {
            trace("confirmar_bloqueado_horario", {
                hora_actual: schedule.hora_actual,
                hora_fin: schedule.hora_fin,
            }, intentoId);
            showResultModal(
                "Pedidos cerrados",
                `${schedule.mensaje} Tu pedido sigue guardado en curso.`,
                "error",
            );
            return;
        }
        const confirmation = [
            `El total mostrado (${money(order.total)}) es tentativo.`,
            "Puede cambiar en el ticket final de la compra.",
            "",
            "¿Confirmar pedido?",
        ].join("\n");
        const aceptado = await askConfirm(confirmation);
        trace("confirmar_respuesta", { aceptado }, intentoId);
        if (!aceptado) return;
        try {
            setBusy(true);
            showNotice("Enviando pedido al servidor...", "success");
            trace("confirmar_envio_iniciado", {}, intentoId);
            const data = await postJson(
                apiUrls.confirmar_pedido || "/api/pedidos/confirmar/",
                {},
                intentoId,
            );
            trace("confirmar_respuesta_exitosa", { pedido_id: data.pedido_id }, intentoId);
            renderDailyProgress(data.progreso_diario);
            order = { items: [], total: "0.00" };
            selectedItemId = null;
            quantityInput = "";
            replaceOnNextKey = false;
            setQuantityConfirmed(false);
            renderOrder();
            showResultModal(
                "Pedido confirmado",
                `${data.mensaje} Total de este pedido: ${money(data.total)}.`,
            );
        } catch (error) {
            if (error.horario) {
                schedule = { ...schedule, ...error.horario };
            }
            if (error.progresoDiario) renderDailyProgress(error.progresoDiario);
            showNotice(error.message, "error");
            trace("confirmar_error", {
                status: error.status || "red",
                codigo: error.codigo || "sin_codigo",
                mensaje: error.message,
            }, intentoId);
            showResultModal("No se pudo confirmar", error.message, "error");
        } finally {
            setBusy(false);
            window.__pedidoConfirmAttemptId = "";
        }
    }

    productButtons.forEach((button) => {
        button.addEventListener("click", () => selectProduct(button.dataset.productId));
    });

    mobilePanelTabs.forEach((button) => {
        button.addEventListener("click", () => setMobilePanel(button.dataset.mobilePanel));
    });

    keyboardToggle?.addEventListener("click", () => setKeyboardCollapsed(!isKeyboardCollapsed));

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
    closeSuccess.addEventListener("click", () => {
        modal.hidden = true;
    });

    confirmAccept?.addEventListener("click", () => pendingConfirm?.(true));
    confirmCancel?.addEventListener("click", () => pendingConfirm?.(false));
    confirmModal?.addEventListener("click", (event) => {
        if (event.target === confirmModal) pendingConfirm?.(false);
    });

    selectProduct(selectedProduct?.id);
    renderDailyProgress();
    renderOrder();
    refreshScheduleStatus();
    if (schedule.aplica) window.setInterval(refreshScheduleStatus, 60000);
});
