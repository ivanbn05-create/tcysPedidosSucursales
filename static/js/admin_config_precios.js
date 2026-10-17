// Columna "Todas" de la matriz de precios: copia su valor a cada sucursal de la fila.
// Un campo vacío no borra nada; cada celda sigue editable por separado.
(function () {
    "use strict";

    const tabla = document.querySelector(".price-settings-table");
    if (!tabla) return;

    tabla.addEventListener("input", (evento) => {
        const origen = evento.target;
        const esPrecio = origen.matches("[data-fila-precio]");
        if (!esPrecio && !origen.matches("[data-fila-ticket]")) return;
        if (origen.value.trim() === "") return;
        const fila = origen.closest("tr");
        const selector = esPrecio
            ? 'input[name^="precio_"]:not([name^="precio_ticket_"])'
            : 'input[name^="precio_ticket_"]';
        fila.querySelectorAll(selector).forEach((destino) => {
            destino.value = origen.value;
        });
    });
})();
