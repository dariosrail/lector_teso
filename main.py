"""
Visor MySQL con Flet (web, escritorio y Android) - solo lectura.
Basado en el visor Tkinter: tablas, datos paginados, búsqueda, orden,
estructura, detalle de fila, consulta SQL y exportar CSV.

- Local:     python main.py
- Servidor:  usa la variable PORT (Railway la pone sola)
- Android:   flet build apk
"""
import os
import csv
import time
import uuid
from urllib.parse import urlparse, unquote

import flet as ft
import pymysql
from pymysql.cursors import DictCursor

PAGE_SIZE = 100          # filas por página en "Datos"
SQL_MAX_ROWS = 1000      # máximo de filas en la pestaña "SQL"
CSV_MAX_ROWS = 100000    # máximo de filas al exportar
ALLOWED = ("select", "show", "describe", "desc", "explain")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
EXPORT_DIR = os.path.join(ASSETS_DIR, "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

INK = "#1D3557"
AMBER = "#E9A23B"


def q(nombre):
    """Escapa un identificador (tabla/columna) para MySQL."""
    return "`" + str(nombre).replace("`", "``") + "`"


def fmt(valor, max_len=120):
    if valor is None:
        return "NULL"
    if isinstance(valor, (bytes, bytearray)):
        return f"<binario {len(valor)} bytes>"
    s = str(valor).replace("\n", " ⏎ ")
    return s if len(s) <= max_len else s[:max_len] + "…"


def parse_url(url):
    u = urlparse(url.strip())
    if u.scheme not in ("mysql", "mysql+pymysql"):
        raise ValueError("La URL debe empezar con mysql://")
    if not u.hostname:
        raise ValueError("Falta el host en la URL")
    return dict(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or "root"),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/") or "railway",
    )


def limpiar_exportes_viejos(max_edad=3600):
    ahora = time.time()
    for f in os.listdir(EXPORT_DIR):
        ruta = os.path.join(EXPORT_DIR, f)
        try:
            if ahora - os.path.getmtime(ruta) > max_edad:
                os.remove(ruta)
        except OSError:
            pass


def main(page: ft.Page):
    page.title = "Visor MySQL"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=INK)
    page.padding = 16

    st = dict(conn=None, cfg=None, tabla=None, columnas=[], pagina=0,
              total=0, filtro="", orden=None)

    # ------------------------------------------------------------ controles
    url_field = ft.TextField(
        label="URL de conexión",
        hint_text="mysql://usuario:contraseña@host:puerto/base",
        password=True, can_reveal_password=True, expand=True,
    )
    connect_btn = ft.FilledButton("Conectar", icon=ft.Icons.POWER)
    status = ft.Text("Pega tu URL de MySQL y toca Conectar.", size=13)

    tablas_dd = ft.Dropdown(label="Tabla", options=[], expand=True, disabled=True)
    refresh_btn = ft.IconButton(ft.Icons.REFRESH, tooltip="Recargar tablas", disabled=True)

    # Datos
    buscar_field = ft.TextField(label="Buscar en todas las columnas", expand=True,
                                dense=True, disabled=True)
    btn_buscar = ft.IconButton(ft.Icons.SEARCH, tooltip="Buscar", disabled=True)
    btn_limpiar = ft.IconButton(ft.Icons.CLEAR, tooltip="Limpiar búsqueda", disabled=True)
    btn_csv = ft.OutlinedButton("Exportar CSV", icon=ft.Icons.DOWNLOAD, disabled=True)
    btn_prev = ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Anterior", disabled=True)
    btn_next = ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Siguiente", disabled=True)
    lbl_pagina = ft.Text("Selecciona una tabla", size=13)
    datos_holder = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    # Estructura
    estr_holder = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    # SQL
    sql_field = ft.TextField(label="Consulta (solo SELECT, SHOW, DESCRIBE, EXPLAIN)",
                             multiline=True, min_lines=3, max_lines=7,
                             text_style=ft.TextStyle(font_family="monospace"),
                             disabled=True)
    btn_run = ft.FilledButton("Ejecutar", icon=ft.Icons.PLAY_ARROW, disabled=True)
    lbl_sql = ft.Text("", size=13)
    sql_holder = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True)

    def marco(c):
        return ft.Container(c, expand=True, padding=8, border_radius=6,
                            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT))

    tabs = ft.Tabs(
        selected_index=0, expand=True, animation_duration=150,
        tabs=[
            ft.Tab(text="Datos", icon=ft.Icons.TABLE_ROWS, content=ft.Container(
                padding=ft.padding.only(top=8),
                content=ft.Column(expand=True, controls=[
                    ft.Row([buscar_field, btn_buscar, btn_limpiar, btn_csv],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    marco(datos_holder),
                    ft.Row([btn_prev, lbl_pagina, btn_next,
                            ft.Text("Toca una fila para ver el detalle · toca un encabezado para ordenar",
                                    size=12, color=ft.Colors.OUTLINE)],
                           wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ]))),
            ft.Tab(text="Estructura", icon=ft.Icons.SCHEMA, content=ft.Container(
                padding=ft.padding.only(top=8), content=marco(estr_holder))),
            ft.Tab(text="SQL", icon=ft.Icons.CODE, content=ft.Container(
                padding=ft.padding.only(top=8),
                content=ft.Column(expand=True, controls=[
                    sql_field,
                    ft.Row([btn_run, lbl_sql], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    marco(sql_holder),
                ]))),
        ],
    )

    controles_conexion = [tablas_dd, refresh_btn, buscar_field, btn_buscar,
                          btn_limpiar, sql_field, btn_run]

    # ------------------------------------------------------------ utilidades
    def set_status(msg, error=False):
        status.value = msg
        status.color = ft.Colors.ERROR if error else None
        page.update()

    def ejecutar(sql, params=None):
        conn = st["conn"]
        if conn is None:
            raise RuntimeError("No hay conexión activa")
        conn.ping(reconnect=True)
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
            if cur.description:
                return [d[0] for d in cur.description], cur.fetchall()
            return [], []

    def ver_detalle(fila):
        items = []
        for k, v in fila.items():
            if v is None:
                val = "NULL"
            elif isinstance(v, (bytes, bytearray)):
                val = f"<binario {len(v)} bytes>"
            else:
                val = str(v)
            items.append(ft.Text(str(k), weight=ft.FontWeight.BOLD, color=INK))
            items.append(ft.Text(val, selectable=True))
            items.append(ft.Container(height=6))
        dlg = ft.AlertDialog(
            title=ft.Text("Detalle de fila"),
            content=ft.Container(ft.Column(items, scroll=ft.ScrollMode.AUTO),
                                 width=560, height=460),
            actions=[ft.TextButton("Cerrar", on_click=lambda e: page.close(dlg))],
        )
        page.open(dlg)

    def tabla_ui(cols, rows, ordenable=False, detalle=True):
        if not cols:
            return ft.Text("La consulta no devolvió columnas.")
        if not rows:
            return ft.Text("Sin filas.")
        sort_idx = None
        columnas = []
        for i, c in enumerate(cols):
            on_sort = (lambda e, c=c: ordenar(c)) if ordenable else None
            columnas.append(ft.DataColumn(ft.Text(str(c), weight=ft.FontWeight.BOLD),
                                          on_sort=on_sort))
            if ordenable and st["orden"] and st["orden"][0] == c:
                sort_idx = i
        filas = []
        for i, r in enumerate(rows):
            filas.append(ft.DataRow(
                cells=[ft.DataCell(ft.Text(fmt(r.get(c)))) for c in cols],
                color=ft.Colors.with_opacity(0.05, INK) if i % 2 else None,
                on_select_changed=(lambda e, r=r: ver_detalle(r)) if detalle else None,
            ))
        dt = ft.DataTable(
            columns=columnas, rows=filas,
            sort_column_index=sort_idx,
            sort_ascending=st["orden"][1] if sort_idx is not None else True,
            show_checkbox_column=False,
            heading_row_color=ft.Colors.with_opacity(0.18, AMBER),
            column_spacing=24, data_row_min_height=34, data_row_max_height=44,
        )
        return ft.Row([dt], scroll=ft.ScrollMode.ALWAYS)

    # ------------------------------------------------------------ conexión
    def conectar(e):
        try:
            cfg = parse_url(url_field.value or "")
        except ValueError as ex:
            return set_status(str(ex), error=True)
        set_status("Conectando...")
        try:
            if st["conn"]:
                st["conn"].close()
            conn = pymysql.connect(**cfg, cursorclass=DictCursor, connect_timeout=15,
                                   read_timeout=60, charset="utf8mb4", autocommit=True)
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            st["conn"], st["cfg"] = conn, cfg
            for c in controles_conexion:
                c.disabled = False
            cargar_tablas()
        except Exception as ex:
            st["conn"] = None
            set_status(f"No se pudo conectar: {ex}", error=True)

    def cargar_tablas(e=None):
        try:
            _, rows = ejecutar(
                "SELECT TABLE_NAME AS t, TABLE_ROWS AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME")
        except Exception as ex:
            return set_status(f"Error: {ex}", error=True)
        tablas_dd.options = [ft.dropdown.Option(key=r["t"], text=f"{r['t']}  (~{r['n'] or 0})")
                             for r in rows]
        set_status(f"Conectado · {len(rows)} tablas en '{st['cfg']['database']}'")

    # ------------------------------------------------------------ tablas
    def al_seleccionar_tabla(e):
        if not tablas_dd.value:
            return
        st.update(tabla=tablas_dd.value, pagina=0, filtro="", orden=None)
        buscar_field.value = ""
        try:
            cols, rows = ejecutar(f"DESCRIBE {q(st['tabla'])}")
            st["columnas"] = [r["Field"] for r in rows]
            estr_holder.controls = [tabla_ui(cols, rows, detalle=False)]
        except Exception as ex:
            return set_status(f"Error: {ex}", error=True)
        sql_field.value = f"SELECT * FROM {q(st['tabla'])} LIMIT 100"
        btn_csv.disabled = False
        cargar_pagina()

    def _where():
        if not st["filtro"] or not st["columnas"]:
            return "", []
        expr = "CONCAT_WS(' ', " + ", ".join(q(c) for c in st["columnas"]) + ")"
        return f" WHERE {expr} LIKE %s", [f"%{st['filtro']}%"]

    def _order():
        if not st["orden"]:
            return ""
        col, asc = st["orden"]
        return f" ORDER BY {q(col)} {'ASC' if asc else 'DESC'}"

    def cargar_pagina():
        t = st["tabla"]
        if not t:
            return
        where, params = _where()
        set_status(f"Cargando {t}...")
        try:
            _, r = ejecutar(f"SELECT COUNT(*) AS n FROM {q(t)}{where}", params)
            st["total"] = r[0]["n"]
            cols, rows = ejecutar(
                f"SELECT * FROM {q(t)}{where}{_order()} LIMIT %s OFFSET %s",
                params + [PAGE_SIZE, st["pagina"] * PAGE_SIZE])
        except Exception as ex:
            return set_status(f"Error: {ex}", error=True)
        datos_holder.controls = [tabla_ui(cols or st["columnas"], rows, ordenable=True)]
        paginas = max(1, -(-st["total"] // PAGE_SIZE))
        lbl_pagina.value = f"Página {st['pagina'] + 1} de {paginas} · {st['total']} filas"
        btn_prev.disabled = st["pagina"] == 0
        btn_next.disabled = (st["pagina"] + 1) * PAGE_SIZE >= st["total"]
        tabs.selected_index = 0
        extra = f" (filtro: '{st['filtro']}')" if st["filtro"] else ""
        set_status(f"{t}: {st['total']} filas{extra}")

    def pagina_anterior(e):
        if st["pagina"] > 0:
            st["pagina"] -= 1
            cargar_pagina()

    def pagina_siguiente(e):
        if (st["pagina"] + 1) * PAGE_SIZE < st["total"]:
            st["pagina"] += 1
            cargar_pagina()

    def ordenar(col):
        if st["orden"] and st["orden"][0] == col:
            st["orden"] = (col, not st["orden"][1])
        else:
            st["orden"] = (col, True)
        st["pagina"] = 0
        cargar_pagina()

    def buscar(e=None):
        st["filtro"] = (buscar_field.value or "").strip()
        st["pagina"] = 0
        cargar_pagina()

    def limpiar_busqueda(e):
        buscar_field.value = ""
        buscar()

    def exportar_csv(e):
        t = st["tabla"]
        if not t:
            return
        where, params = _where()
        set_status("Generando CSV...")
        try:
            cols, rows = ejecutar(f"SELECT * FROM {q(t)}{where}{_order()} LIMIT %s",
                                  params + [CSV_MAX_ROWS])
            nombre = f"{t}_{uuid.uuid4().hex[:10]}.csv"
            destino = EXPORT_DIR if page.web else os.path.join(os.path.expanduser("~"), "Downloads")
            os.makedirs(destino, exist_ok=True)
            ruta = os.path.join(destino, nombre)
            with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in rows:
                    w.writerow(["" if r[c] is None else r[c] for c in cols])
        except Exception as ex:
            return set_status(f"Error al exportar: {ex}", error=True)
        if page.web:
            limpiar_exportes_viejos()
            page.launch_url(f"/exports/{nombre}")
            set_status(f"CSV listo: {len(rows)} filas (se abre la descarga)")
        else:
            set_status(f"Exportadas {len(rows)} filas a {ruta}")

    # ------------------------------------------------------------ SQL
    def ejecutar_sql(e):
        sql = (sql_field.value or "").strip().rstrip(";").strip()
        if not sql:
            return
        primera = sql.split(None, 1)[0].lower()
        if primera not in ALLOWED or ";" in sql:
            lbl_sql.value, lbl_sql.color = "Solo una consulta SELECT, SHOW, DESCRIBE o EXPLAIN.", ft.Colors.ERROR
            return page.update()
        if primera == "select" and " limit " not in f" {sql.lower()} ":
            sql += f" LIMIT {SQL_MAX_ROWS + 1}"
        try:
            cols, rows = ejecutar(sql)
        except Exception as ex:
            lbl_sql.value, lbl_sql.color = f"Error: {ex}", ft.Colors.ERROR
            return page.update()
        aviso = ""
        if len(rows) > SQL_MAX_ROWS:
            rows = rows[:SQL_MAX_ROWS]
            aviso = f" (mostrando {SQL_MAX_ROWS})"
        sql_holder.controls = [tabla_ui(cols, rows)]
        lbl_sql.value, lbl_sql.color = f"{len(rows)} filas{aviso}", ft.Colors.GREEN_700
        page.update()

    # ------------------------------------------------------------ eventos
    connect_btn.on_click = conectar
    url_field.on_submit = conectar
    refresh_btn.on_click = cargar_tablas
    tablas_dd.on_change = al_seleccionar_tabla
    buscar_field.on_submit = buscar
    btn_buscar.on_click = buscar
    btn_limpiar.on_click = limpiar_busqueda
    btn_csv.on_click = exportar_csv
    btn_prev.on_click = pagina_anterior
    btn_next.on_click = pagina_siguiente
    btn_run.on_click = ejecutar_sql

    # ------------------------------------------------------------ layout
    page.add(ft.Column(
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[
            ft.Text("Visor MySQL", size=24, weight=ft.FontWeight.W_700, color=INK),
            ft.Row([url_field, connect_btn], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            status,
            ft.Row([tablas_dd, refresh_btn], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            tabs,
        ],
    ))


if __name__ == "__main__":
    port = os.getenv("PORT")
    if port:
        ft.app(target=main, view=None, host="0.0.0.0", port=int(port), assets_dir=ASSETS_DIR)
    else:
        ft.app(target=main, assets_dir=ASSETS_DIR)
