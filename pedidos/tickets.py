from decimal import Decimal

from django.utils import timezone

from .models import Precio

TICKET_HEADER_HEIGHT_MM = 7.67
TICKET_DATE_HEIGHT_MM = 5.82
TICKET_ITEM_HEIGHT_MM = 9.26
TICKET_SHORT_ITEM_HEIGHT_MM = 8.20
TICKET_PRINT_SAFETY_HEIGHT_MM = 6.0
TICKET_WIDTH_MM = 72
TICKET_MIN_HEIGHT_MM = TICKET_WIDTH_MM + 1
TICKET_COLUMN_WIDTHS_MM = {
    "product": 32.59,
    "quantity": 18.43,
    "blank": 20.98,
}
SPANISH_MONTH_ABBR = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)


def ticket_date(pedido):
    base_date = pedido.fecha_confirmacion or pedido.fecha_creacion
    return timezone.localtime(base_date).date()


def ticket_date_display(pedido):
    local_date = ticket_date(pedido)
    return f"{local_date.day}-{SPANISH_MONTH_ABBR[local_date.month - 1]}"


def format_ticket_quantity(value):
    decimal_value = Decimal(value).quantize(Decimal("0.001")).normalize()
    if decimal_value == decimal_value.to_integral():
        return str(decimal_value.quantize(Decimal("1")))
    return format(decimal_value, "f").rstrip("0").rstrip(".")


def ticket_label_for_item(item, fecha, pedido=None, etiqueta_ticket=None):
    if etiqueta_ticket is not None:
        return etiqueta_ticket.upper()

    pedido = pedido or item.pedido
    precio = (
        Precio.objects.filter(
            producto_id=item.producto_id,
            sucursal_cliente_id=pedido.sucursal_cliente_id,
            fecha_vigencia__lte=fecha,
        )
        .order_by("-fecha_vigencia")
        .first()
    )
    if precio is not None:
        return precio.etiqueta_ticket.upper()
    return item.producto.etiqueta_ticket.upper()


def format_ticket_quantity_with_unit(item):
    quantity = format_ticket_quantity(item.cantidad)
    return f"{quantity} {item.producto.unidad_corta}".strip()


def ticket_title(pedido):
    return pedido.sucursal_cliente.nombre.upper()


def ticket_items(pedido, items=None, etiquetas_por_item=None):
    fecha = ticket_date(pedido)
    items = items if items is not None else pedido.items.select_related("producto").all()
    etiquetas_por_item = etiquetas_por_item or {}
    return [
        {
            "producto": ticket_label_for_item(
                item,
                fecha,
                pedido=pedido,
                etiqueta_ticket=etiquetas_por_item.get(item.id),
            ),
            "cantidad": format_ticket_quantity_with_unit(item),
        }
        for item in items
    ]


def ticket_context(pedido, items=None, etiquetas_por_item=None):
    rows = []
    for index, row in enumerate(
        ticket_items(pedido, items=items, etiquetas_por_item=etiquetas_por_item),
        start=3,
    ):
        rows.append(
            {
                **row,
                "row_number": index,
                "height_mm": (
                    TICKET_SHORT_ITEM_HEIGHT_MM
                    if index == 7
                    else TICKET_ITEM_HEIGHT_MM
                ),
            }
        )
    content_height_mm = (
        TICKET_HEADER_HEIGHT_MM
        + TICKET_DATE_HEIGHT_MM
        + sum(row["height_mm"] for row in rows)
        + TICKET_PRINT_SAFETY_HEIGHT_MM
    )
    ticket_height_mm = max(content_height_mm, TICKET_MIN_HEIGHT_MM)
    return {
        "pedido": pedido,
        "title": ticket_title(pedido),
        "date": ticket_date(pedido),
        "date_display": ticket_date_display(pedido),
        "rows": rows,
        "ticket_width_mm": TICKET_WIDTH_MM,
        "ticket_height_mm": round(ticket_height_mm, 2),
        "column_widths_mm": TICKET_COLUMN_WIDTHS_MM,
        "header_height_mm": TICKET_HEADER_HEIGHT_MM,
        "date_height_mm": TICKET_DATE_HEIGHT_MM,
    }
