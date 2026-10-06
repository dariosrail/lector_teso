"""
Lector MySQL con Flet (solo lectura).
- Local / escritorio:  python main.py
- Servidor (Railway, Render...): usa la variable PORT automáticamente.
- Android:  flet build apk
"""
import os
from urllib.parse import urlparse, unquote

import flet as ft
import pymysql

PAGE_SIZE = 50
ALLOWED = ("select", "show", "describe", "desc", "explain")

# Paleta: tinta azul acero + ámbar, pensada para leer tablas largas
INK = "#1D3557"
AMBER = "#E9A23B"


def parse_url(url: str) -> dict:
    u = urlparse(url.strip())
    if u.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("La URL debe empezar con mysql://")
    if not u.hostname:
        raise ValueError("Falta el host en la URL")
    return dict(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/") or None,
    )


def fmt(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray)):
        return f"<binario {len(value)} bytes>"
    s = str(value)
    return s if len(s) <= 120 else s[:117] + "..."


def main(page: ft.Page):
    page.title = "Lector MySQL"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed=INK)
    page.padding = 16

    state = {"conn": None, "table": None, "offset": 0, "total": 0}

    # ---------- Controles ----------
    url_field = ft.TextField(
        label="URL de conexión",
        hint_text="mysql://usuario:contraseña@host:puerto/base",
        password=True,
        can_reveal_password=True,
        expand=True,
    )
    connect_btn = ft.FilledButton("Conectar", icon=ft.Icons.POWER)
    status = ft.Text("Pega tu URL de MySQL y toca Conectar.", size=13)

    tables_dd = ft.Dropdown(label="Tabla", options=[], expand=True, disabled=True)
    sql_field = ft.TextField(
        label="Consulta (solo SELECT, SHOW, DESCRIBE)",
        multiline=True,
        min_lines=1,
        max_lines=4,
        expand=True,
        disabled=True,
    )
    run_btn = ft.OutlinedButton("Ejecutar", icon=ft.Icons.PLAY_ARROW, disabled=True)

    prev_btn = ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Anteriores", disabled=True)
    next_btn = ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Siguientes", disabled=True)
    page_label = ft.Text("", size=13)

    table_holder = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    def set_status(msg, error=False):
        status.value = msg
        status.color = ft.Colors.ERROR if error else None
        page.update()

    def get_conn():
        conn = state["conn"]
        if conn is None:
            raise RuntimeError("No hay conexión activa")
        conn.ping(reconnect=True)
        return conn

    def query(sql, params=None):
        with get_conn().cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, cur.fetchall()

    def render(cols, rows):
        if not cols:
            table_holder.controls = [ft.Text("La consulta no devolvió columnas.")]
            return
        if not rows:
            table_holder.controls = [ft.Text("Sin filas.")]
            return
        dt = ft.DataTable(
            columns=[ft.DataColumn(ft.Text(c, weight=ft.FontWeight.BOLD)) for c in cols],
            rows=[
                ft.DataRow(cells=[ft.DataCell(ft.Text(fmt(v), selectable=True)) for v in r])
                for r in rows
            ],
            heading_row_color=ft.Colors.with_opacity(0.15, AMBER),
            column_spacing=24,
        )
        # Scroll horizontal para tablas anchas (importante en el celular)
        table_holder.controls = [ft.Row([dt], scroll=ft.ScrollMode.ALWAYS)]

    def update_pager():
        t, off, total = state["table"], state["offset"], state["total"]
        if t:
            end = min(off + PAGE_SIZE, total)
            page_label.value = f"{off + 1 if total else 0}–{end} de {total}"
            prev_btn.disabled = off == 0
            next_btn.disabled = end >= total
        else:
            page_label.value = ""
            prev_btn.disabled = next_btn.disabled = True

    # ---------- Acciones ----------
    def connect(e):
        try:
            cfg = parse_url(url_field.value or "")
        except ValueError as ex:
            set_status(str(ex), error=True)
            return
        set_status("Conectando...")
        try:
            if state["conn"]:
                state["conn"].close()
            conn = pymysql.connect(
                **cfg,
                connect_timeout=10,
                read_timeout=30,
                charset="utf8mb4",
                autocommit=True,
            )
            # Capa extra de seguridad: la sesión no puede escribir
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            state["conn"] = conn
            _, rows = query("SHOW TABLES")
            names = [r[0] for r in rows]
            tables_dd.options = [ft.dropdown.Option(n) for n in names]
            tables_dd.value = None
            for c in (tables_dd, sql_field, run_btn):
                c.disabled = False
            set_status(f"Conectado a {cfg['host']} · {len(names)} tablas en '{cfg['database']}'.")
        except Exception as ex:
            state["conn"] = None
            set_status(f"No se pudo conectar: {ex}", error=True)

    def load_table_page():
        t = state["table"]
        try:
            _, cnt = query(f"SELECT COUNT(*) FROM `{t}`")
            state["total"] = cnt[0][0]
            cols, rows = query(
                f"SELECT * FROM `{t}` LIMIT %s OFFSET %s", (PAGE_SIZE, state["offset"])
            )
            render(cols, rows)
            update_pager()
            set_status(f"Tabla {t}")
        except Exception as ex:
            set_status(f"Error al leer {t}: {ex}", error=True)

    def pick_table(e):
        name = tables_dd.value
        if not name:
            return
        state["table"] = name.replace("`", "")
        state["offset"] = 0
        load_table_page()

    def go(delta):
        def handler(e):
            state["offset"] = max(0, state["offset"] + delta)
            load_table_page()
        return handler

    def run_sql(e):
        sql = (sql_field.value or "").strip().rstrip(";")
        if not sql:
            return
        first = sql.split(None, 1)[0].lower()
        if first not in ALLOWED or ";" in sql:
            set_status("Solo se permite una consulta SELECT, SHOW, DESCRIBE o EXPLAIN.", error=True)
            return
        if first == "select" and " limit " not in sql.lower():
            sql += f" LIMIT {PAGE_SIZE * 4}"
        try:
            cols, rows = query(sql)
            state["table"] = None
            update_pager()
            render(cols, rows)
            set_status(f"{len(rows)} filas.")
        except Exception as ex:
            set_status(f"Error en la consulta: {ex}", error=True)

    connect_btn.on_click = connect
    url_field.on_submit = connect
    tables_dd.on_change = pick_table
    prev_btn.on_click = go(-PAGE_SIZE)
    next_btn.on_click = go(PAGE_SIZE)
    run_btn.on_click = run_sql

    # ---------- Layout ----------
    page.add(
        ft.Column(
            expand=True,
            controls=[
                ft.Text("Lector MySQL", size=24, weight=ft.FontWeight.W_700, color=INK),
                ft.ResponsiveRow(
                    [
                        ft.Container(ft.Row([url_field]), col={"xs": 12, "md": 9}),
                        ft.Container(connect_btn, col={"xs": 12, "md": 3},
                                     alignment=ft.alignment.center_left),
                    ]
                ),
                status,
                ft.Divider(),
                ft.Row([tables_dd, prev_btn, page_label, next_btn]),
                ft.Row([sql_field, run_btn], vertical_alignment=ft.CrossAxisAlignment.START),
                ft.Container(table_holder, expand=True,
                             border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
                             border_radius=6, padding=8),
            ],
        )
    )


if __name__ == "__main__":
    port = os.getenv("PORT")
    if port:  # Modo servidor (Railway, Render, Fly.io)
        ft.app(target=main, view=None, host="0.0.0.0", port=int(port))
    else:     # Local o empaquetado como app
        ft.app(target=main)
