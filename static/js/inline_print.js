document.addEventListener("DOMContentLoaded", () => {
    const printLinks = document.querySelectorAll("[data-inline-print]");
    const printSurface = document.getElementById("inlinePrintSurface");
    if (!printLinks.length || !printSurface) return;

    let printStyle = document.getElementById("inlinePrintStyle");
    let cleanupTimer = null;
    let cleanupHandler = null;

    function ensurePrintStyle() {
        if (printStyle) return printStyle;
        printStyle = document.createElement("style");
        printStyle.id = "inlinePrintStyle";
        document.head.appendChild(printStyle);
        return printStyle;
    }

    function clearPrintSurface() {
        if (cleanupTimer) {
            window.clearTimeout(cleanupTimer);
            cleanupTimer = null;
        }
        if (cleanupHandler) {
            window.removeEventListener("afterprint", cleanupHandler);
            cleanupHandler = null;
        }
        printSurface.innerHTML = "";
        printSurface.setAttribute("aria-hidden", "true");
        if (printStyle) {
            printStyle.textContent = "";
        }
    }

    function buildPrintCss(widthMm, heightMm) {
        return `
            @page {
                size: ${widthMm}mm ${heightMm}mm;
                margin: 0;
            }

            @media print {
                html,
                body {
                    width: ${widthMm}mm !important;
                    min-width: ${widthMm}mm !important;
                    height: ${heightMm}mm !important;
                    min-height: ${heightMm}mm !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    background: #fff !important;
                    color: #000 !important;
                    overflow: hidden !important;
                }

                body > :not(#inlinePrintSurface) {
                    display: none !important;
                }

                #inlinePrintSurface {
                    display: block !important;
                    width: ${widthMm}mm !important;
                    min-height: ${heightMm}mm !important;
                    margin: 0 !important;
                    padding: 0 !important;
                    background: #fff !important;
                    color: #000 !important;
                    font-family: Calibri, Arial, sans-serif !important;
                }

                .inline-print-sheet {
                    width: ${widthMm}mm !important;
                    margin: 0 !important;
                    background: #fff !important;
                    break-inside: avoid;
                    page-break-inside: avoid;
                }

                .inline-ticket-table,
                .inline-aguas-table {
                    width: 100%;
                    table-layout: fixed;
                    border-collapse: collapse;
                    border-spacing: 0;
                    break-inside: avoid;
                    page-break-inside: avoid;
                }

                .inline-ticket-table,
                .inline-ticket-table *,
                .inline-aguas-table,
                .inline-aguas-table * {
                    box-sizing: border-box;
                }

                .inline-ticket-title {
                    padding: 0;
                    border: 0;
                    font-size: 16pt;
                    font-weight: 700;
                    line-height: 1;
                    text-align: center;
                    vertical-align: middle;
                    white-space: nowrap;
                }

                .inline-ticket-date-row td {
                    padding: 0;
                    border: 0;
                }

                .inline-ticket-date-cell {
                    border: 1px solid #000 !important;
                    font-size: 11pt;
                    font-weight: 700;
                    line-height: 1;
                    text-align: center;
                    vertical-align: middle;
                }

                .inline-ticket-item-row {
                    break-inside: avoid;
                    page-break-inside: avoid;
                }

                .inline-ticket-item-row td {
                    padding: 0;
                    border: 1px solid #000;
                    line-height: 1;
                    vertical-align: middle;
                }

                .inline-ticket-product {
                    padding-left: 1mm !important;
                    font-size: 10pt;
                    font-weight: 700;
                    text-align: left;
                    white-space: nowrap;
                    overflow: hidden;
                }

                .inline-ticket-quantity {
                    padding: 0 0.5mm !important;
                    font-size: 9pt;
                    font-weight: 700;
                    text-align: center;
                    white-space: nowrap;
                    overflow: hidden;
                }

                .inline-aguas-sheet,
                .inline-aguas-table {
                    width: 72mm !important;
                    height: 72mm !important;
                }

                .inline-aguas-table col.label {
                    width: 16mm;
                }

                .inline-aguas-table col.branch,
                .inline-aguas-table col.blank {
                    width: 14mm;
                }

                .inline-aguas-table th,
                .inline-aguas-table td {
                    height: calc(72mm / 7);
                    padding: 0;
                    border: 0.35mm solid #000;
                    color: #000;
                    font-family: "Aptos Narrow", Arial, sans-serif;
                    font-size: 16pt;
                    font-weight: 400;
                    line-height: 1;
                    text-align: center;
                    vertical-align: middle;
                    white-space: nowrap;
                }
            }
        `;
    }

    function printTemplate(template) {
        const widthMm = Number(template.dataset.printWidth || 72);
        const heightMm = Number(template.dataset.printHeight || 73);
        const style = ensurePrintStyle();

        clearPrintSurface();
        printSurface.innerHTML = "";
        printSurface.appendChild(template.content.cloneNode(true));
        printSurface.setAttribute("aria-hidden", "false");
        style.textContent = buildPrintCss(widthMm, heightMm);

        cleanupHandler = () => {
            clearPrintSurface();
        };
        window.addEventListener("afterprint", cleanupHandler);
        cleanupTimer = window.setTimeout(cleanupHandler, 60000);
        window.print();
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

            const templateId = link.dataset.printTemplateId;
            const template = templateId ? document.getElementById(templateId) : null;
            if (!template) {
                return;
            }

            event.preventDefault();
            printTemplate(template);
        });
    });
});
