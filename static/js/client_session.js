(() => {
    const config = document.getElementById("clientSessionConfig");
    if (!config) return;

    const auditUrl = config.dataset.auditUrl;
    const heartbeatUrl = config.dataset.heartbeatUrl;
    const loginUrl = config.dataset.loginUrl || "/login/";
    const userId = config.dataset.userId || "anonimo";
    const queueKey = `pedidos_audit_queue:${userId}`;
    const deviceKey = "pedidos_device_id";
    let flushing = false;
    let memoryQueue = [];

    function randomId() {
        return window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }

    function getDeviceId() {
        try {
            let value = window.localStorage.getItem(deviceKey);
            if (!value) {
                value = randomId();
                window.localStorage.setItem(deviceKey, value);
            }
            return value;
        } catch (error) {
            if (!config.dataset.fallbackDeviceId) config.dataset.fallbackDeviceId = randomId();
            return config.dataset.fallbackDeviceId;
        }
    }

    function csrfToken() {
        const token = document.cookie.split("; ").find((row) => row.startsWith("csrftoken="));
        return token ? decodeURIComponent(token.split("=")[1]) : "";
    }

    function readQueue() {
        try {
            const parsed = JSON.parse(window.localStorage.getItem(queueKey) || "[]");
            return Array.isArray(parsed) ? parsed : [];
        } catch (error) {
            return memoryQueue;
        }
    }

    function saveQueue(queue) {
        const limited = queue.slice(-200);
        memoryQueue = limited;
        try {
            window.localStorage.setItem(queueKey, JSON.stringify(limited));
        } catch (error) {
            /* La cola en memoria conserva la evidencia durante esta página. */
        }
    }

    function sessionReplaced() {
        window.location.assign(`${loginUrl}?sesion=reemplazada`);
    }

    async function flush() {
        if (flushing || !auditUrl || navigator.onLine === false) return;
        const queue = readQueue();
        if (!queue.length) return;
        const batch = queue.slice(0, 50);
        flushing = true;
        try {
            const response = await fetch(auditUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken(),
                },
                body: JSON.stringify({ eventos: batch }),
                credentials: "same-origin",
                keepalive: true,
            });
            if (response.status === 401) {
                sessionReplaced();
                return;
            }
            if (!response.ok) return;
            const sentIds = new Set(batch.map((event) => event.evento_id));
            saveQueue(readQueue().filter((event) => !sentIds.has(event.evento_id)));
        } catch (error) {
            /* Se conserva en localStorage y se reintenta al recuperar conexión. */
        } finally {
            flushing = false;
        }
    }

    function record(evento, detalle = {}, intentoId = "") {
        const event = {
            evento_id: randomId(),
            evento,
            intento_id: intentoId || "",
            dispositivo_id: getDeviceId(),
            ocurrido_en: new Date().toISOString(),
            detalle: {
                ...detalle,
                pagina: window.location.pathname,
                visible: document.visibilityState,
                en_linea: navigator.onLine,
            },
            ua: navigator.userAgent,
        };
        saveQueue([...readQueue(), event]);
        void flush();
        return event;
    }

    async function heartbeat() {
        if (!heartbeatUrl || document.visibilityState !== "visible" || navigator.onLine === false) return;
        try {
            const response = await fetch(heartbeatUrl, {
                method: "POST",
                headers: { "X-CSRFToken": csrfToken() },
                credentials: "same-origin",
                cache: "no-store",
            });
            if (response.status === 401) sessionReplaced();
        } catch (error) {
            /* La siguiente renovación o navegación volverá a validar la sesión. */
        }
    }

    window.clientAudit = {
        deviceId: getDeviceId(),
        flush,
        newAttemptId: randomId,
        record,
    };

    // Captura antes del listener funcional de pedidos.js. Así distinguimos el
    // toque físico de la ejecución (o ausencia) del controlador principal.
    document.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.id === "confirmOrder") {
            const attemptId = randomId();
            window.__pedidoConfirmAttemptId = attemptId;
            record(
                "confirmar_click_captura",
                { deshabilitado: button.disabled },
                attemptId,
            );
        } else if (button.id === "confirmAccept") {
            record(
                "confirmar_aceptar_captura",
                { deshabilitado: button.disabled },
                window.__pedidoConfirmAttemptId || "",
            );
        } else if (button.id === "confirmCancel") {
            record("confirmar_cancelar_captura", {}, window.__pedidoConfirmAttemptId || "");
        }
    }, true);

    window.addEventListener("online", () => {
        void flush();
        void heartbeat();
    });
    window.addEventListener("pageshow", () => {
        void flush();
        void heartbeat();
    });
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            void flush();
            void heartbeat();
        }
    });
    window.setInterval(() => void heartbeat(), 60000);
    window.setInterval(() => void flush(), 30000);
    void flush();
})();
