document.addEventListener("DOMContentLoaded", () => {
    const printLinks = document.querySelectorAll("[data-inline-print]");
    if (!printLinks.length) return;

    let frame = null;
    let cleanupTimer = null;

    function printableUrl(url) {
        const nextUrl = new URL(url, window.location.href);
        nextUrl.searchParams.set("embedded", "1");
        return nextUrl.toString();
    }

    function ensureFrame() {
        if (frame) return frame;
        frame = document.createElement("iframe");
        frame.title = "Documento de impresion";
        frame.setAttribute("aria-hidden", "true");
        frame.dataset.inlinePrintFrame = "true";
        Object.assign(frame.style, {
            position: "fixed",
            right: "0",
            bottom: "0",
            width: "0",
            height: "0",
            border: "0",
            opacity: "0",
            pointerEvents: "none",
        });
        document.body.appendChild(frame);
        return frame;
    }

    function cleanupFrame() {
        if (cleanupTimer) {
            window.clearTimeout(cleanupTimer);
            cleanupTimer = null;
        }
        if (frame) {
            frame.remove();
            frame = null;
        }
    }

    function printInsideFrame(url) {
        cleanupFrame();
        const targetFrame = ensureFrame();
        targetFrame.onload = () => {
            window.setTimeout(() => {
                const printWindow = targetFrame.contentWindow;
                if (!printWindow) return;
                printWindow.focus();
                printWindow.onafterprint = cleanupFrame;
                cleanupTimer = window.setTimeout(cleanupFrame, 60000);
                printWindow.print();
            }, 150);
        };
        targetFrame.src = printableUrl(url);
    }

    printLinks.forEach((link) => {
        link.addEventListener("click", (event) => {
            if (
                event.defaultPrevented ||
                event.button !== 0 ||
                event.metaKey ||
                event.ctrlKey ||
                event.shiftKey ||
                event.altKey
            ) {
                return;
            }
            event.preventDefault();
            printInsideFrame(link.href);
        });
    });
});
