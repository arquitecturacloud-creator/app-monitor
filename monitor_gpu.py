#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
Monitor GPU RTX 4070 Ti & Diagnóstico de Eventos del Sistema Windows
==============================================================================
Aplicación de escritorio portable para Windows 10/11 para monitorizar en tiempo
real una tarjeta gráfica NVIDIA RTX 4070 Ti y registrar eventos críticos del
sistema (Kernel-Power 41, WHEA-Logger, nvlddmkm, BugCheck 1001, etc.).

Diseñado para diagnosticar apagados repentinos, reinicios y pantallas negras
durante sesiones de juego.

Autor: Antigravity / Pair Programming
==============================================================================
"""

import os
import sys
import csv
import time
import shutil
import tempfile
import threading
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
import queue

# Configurar directorio de caché temporal para matplotlib antes de importarlo
# Esto evita fallos de permisos en directorios de usuario restringidos
if "MPLCONFIGDIR" not in os.environ:
    os.environ["MPLCONFIGDIR"] = os.path.join(tempfile.gettempdir(), "matplotlib_monitor_gpu")

import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import MaxNLocator

# ============================================================================
# CONSTANTES Y CONFIGURACIÓN
# ============================================================================
INTERVALO_GPU_SEGUNDOS = 2.0       # Frecuencia de muestreo de nvidia-smi (2s)
INTERVALO_EVENTOS_SEGUNDOS = 3.0   # Frecuencia de consulta del visor de eventos
MAX_PUNTOS_GRAFICA = 300           # Últimos 300 puntos (300 * 2s = 10 minutos)

# Umbrales de alerta visual
UMBRAL_TEMP_ALERTA = 90.0          # Alerta si Temp > 90 °C
UMBRAL_POTENCIA_ALERTA = 300.0     # Alerta si Potencia > 300 W

# Colores del tema oscuro (Gaming / Dark High-Tech)
COLOR_FONDO_APP = "#121214"
COLOR_TARJETA = "#1c1c21"
COLOR_TARJETA_BORDE = "#2a2a35"
COLOR_TARJETA_ALERTA = "#4a1217"
COLOR_BORDE_ALERTA = "#ef4444"
COLOR_TEXTO_PRINCIPAL = "#f3f4f6"
COLOR_TEXTO_SECUNDARIO = "#9ca3af"

# Colores de métricas
COLOR_TEMP = "#ff5555"             # Rojo coral
COLOR_POTENCIA = "#00e5ff"         # Cyan neón
COLOR_RELOJ = "#a78bfa"            # Púrpura suave
COLOR_USO = "#10b981"              # Verde esmeralda
COLOR_LIMITE = "#cbd5e1"           # Gris pizarra

# Flags de creación de subprocesos en Windows para no mostrar ventanas negras
CREATION_FLAGS = 0
if sys.platform == "win32":
    CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0x08000000


# ============================================================================
# FUNCIONES DE PERSISTENCIA Y CARPETAS
# ============================================================================
def obtener_directorio_logs():
    """
    Obtiene la ruta a la carpeta 'MonitorGPU_logs' en el Escritorio del usuario.
    Funciona tanto en sistemas en español (Escritorio) como en inglés (Desktop).
    """
    perfil = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    posibles_escritorios = [
        os.path.join(perfil, "Desktop"),
        os.path.join(perfil, "Escritorio"),
    ]
    escritorio = posibles_escritorios[0]
    for p in posibles_escritorios:
        if os.path.isdir(p):
            escritorio = p
            break

    directorio_logs = os.path.join(escritorio, "MonitorGPU_logs")
    try:
        os.makedirs(directorio_logs, exist_ok=True)
    except Exception as e:
        # Fallback a directorio local si el escritorio estuviera bloqueado
        directorio_logs = os.path.join(os.getcwd(), "MonitorGPU_logs")
        os.makedirs(directorio_logs, exist_ok=True)
    return directorio_logs


def escribir_linea_csv(ruta_csv, fila, encabezados):
    """
    Escribe una fila en el archivo CSV y lo cierra de inmediato para evitar
    pérdida de datos en caso de apagón o BSOD repentino.
    """
    try:
        archivo_nuevo = not os.path.exists(ruta_csv) or os.path.getsize(ruta_csv) == 0
        with open(ruta_csv, mode="a", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            if archivo_nuevo:
                escritor.writerow(encabezados)
            escritor.writerow(fila)
            f.flush()
            os.fsync(f.fileno())  # Forzar volcado físico al almacenamiento
    except Exception as e:
        print(f"[Error Escribir CSV] {e}", file=sys.stderr)


def escribir_linea_txt(ruta_txt, linea):
    """
    Escribe una línea en el archivo de texto y lo cierra de inmediato con fsync.
    """
    try:
        with open(ruta_txt, mode="a", encoding="utf-8") as f:
            f.write(linea + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"[Error Escribir TXT] {e}", file=sys.stderr)


# ============================================================================
# LECTOR DE MÉTRICAS NVIDIA-SMI
# ============================================================================
class LectorNvidiaSMI:
    """
    Gestiona la ejecución periódica de nvidia-smi para extraer métricas de la GPU.
    """
    def __init__(self):
        self.ruta_nvidia_smi = self._localizar_nvidia_smi()

    def _localizar_nvidia_smi(self):
        """Busca el ejecutable nvidia-smi en el PATH o en rutas estándar de Windows."""
        en_path = shutil.which("nvidia-smi")
        if en_path:
            return en_path

        rutas_comunes = [
            os.path.expandvars(r"%SystemRoot%\System32\nvidia-smi.exe"),
            r"C:\Windows\System32\nvidia-smi.exe",
            os.path.expandvars(r"%ProgramFiles%\NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
        for ruta in rutas_comunes:
            if os.path.isfile(ruta):
                return ruta
        return "nvidia-smi"

    def leer_metricas(self):
        """
        Ejecuta el comando exacto solicitado:
        nvidia-smi --query-gpu=temperature.gpu,power.draw,power.limit,clocks.gr,utilization.gpu --format=csv,noheader,nounits

        Retorna (datos_dict, error_msg)
        """
        cmd = [
            self.ruta_nvidia_smi,
            "--query-gpu=temperature.gpu,power.draw,power.limit,clocks.gr,utilization.gpu",
            "--format=csv,noheader,nounits"
        ]
        try:
            resultado = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3.0,
                creationflags=CREATION_FLAGS
            )
            if resultado.returncode != 0:
                err = resultado.stderr.strip() or f"Código de salida: {resultado.returncode}"
                return None, f"nvidia-smi no respondió correctamente ({err})"

            salida = resultado.stdout.strip()
            if not salida:
                return None, "nvidia-smi no devolvió métricas"

            # Si hay múltiples GPUs, tomamos la primera línea
            primera_linea = salida.splitlines()[0]
            partes = [p.strip() for p in primera_linea.split(",")]

            if len(partes) < 5:
                return None, f"Formato inesperado de nvidia-smi: '{salida}'"

            # Parseo tolerante a valores 'N/A' o 'Not Supported'
            def parse_num(valor, tipo=float):
                try:
                    return tipo(valor)
                except Exception:
                    return None

            temp = parse_num(partes[0], float)
            potencia = parse_num(partes[1], float)
            limite_pot = parse_num(partes[2], float)
            reloj = parse_num(partes[3], int)
            uso = parse_num(partes[4], float)

            return {
                "temperatura": temp,
                "potencia": potencia,
                "limite_potencia": limite_pot,
                "reloj": reloj,
                "uso": uso
            }, None

        except FileNotFoundError:
            return None, "nvidia-smi no encontrado en el sistema. ¿Están instalados los drivers de NVIDIA?"
        except subprocess.TimeoutExpired:
            return None, "nvidia-smi excedió el tiempo límite de respuesta (GPU ocupada o bloqueada)"
        except Exception as e:
            return None, f"Error al ejecutar nvidia-smi: {str(e)}"


# ============================================================================
# LECTOR DE EVENTOS DEL SISTEMA DE WINDOWS (WEVTUTIL / XML)
# ============================================================================
class LectorEventosWindows:
    """
    Consulta los eventos recientes del registro 'System' usando la herramienta nativa
    wevtutil con una consulta XPath estructurada. Filtra específicamente:
    - Kernel-Power (ID 41)
    - WHEA-Logger (IDs 17, 18, 19, 47)
    - nvlddmkm (Driver NVIDIA)
    - BugCheck (ID 1001)
    - Cualquier evento de Nivel 1 (Crítico) o Nivel 2 (Error)
    """
    def __init__(self):
        self.ids_vistos = set()
        self.permiso_denegado = False
        # Consulta XPath optimizada para el Visor de Eventos
        self.xpath_query = (
            "*[System["
            "(Level=1 or Level=2) "
            "or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) "
            "or (Provider[@Name='Microsoft-Windows-WHEA-Logger'] and (EventID=17 or EventID=18 or EventID=19 or EventID=47)) "
            "or (Provider[@Name='nvlddmkm']) "
            "or (EventID=1001)"
            "]]"
        )

    def _convertir_utc_a_local(self, iso_str):
        """Convierte una cadena SystemTime ISO UTC a hora local legible."""
        try:
            if not iso_str:
                return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Truncar fracciones de segundo
            base = iso_str.split(".")[0]
            dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone()
            return dt_local.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso_str[:19].replace("T", " ")

    def _generar_explicacion_diagnostica(self, proveedor, id_evento, nivel_str, detalle):
        """
        Genera un texto explicativo claro para ayudar al diagnóstico de apagados y pantallas negras.
        """
        prov_lower = proveedor.lower()
        if "kernel-power" in prov_lower and str(id_evento) == "41":
            return "APAGADO REPENTINO / CORTE DE ENERGÍA: El equipo se reinició sin apagarse limpiamente primero. Causa habitual: corte de corriente, caída de tensión o activación de protección de la fuente."
        elif "nvlddmkm" in prov_lower:
            return f"FALLO DE DRIVER NVIDIA (TDR): El controlador de pantalla nvlddmkm dejó de responder y se recuperó (posible pantalla negra en juego). Detalle: {detalle}"
        elif "whea-logger" in prov_lower:
            return f"ERROR DE HARDWARE WHEA (ID {id_evento}): Fallo detectado por la arquitectura de hardware de Windows (inestabilidad de CPU, bus PCIe o memoria). Detalle: {detalle}"
        elif str(id_evento) == "1001" or "bugcheck" in prov_lower:
            return f"PANTALLA AZUL (BSOD): El equipo se reinició tras un error grave del sistema. Parámetros: {detalle}"
        elif nivel_str == "Crítico":
            return f"EVENTO CRÍTICO DEL SISTEMA ({proveedor}): {detalle}"
        else:
            return f"{proveedor} (ID {id_evento}): {detalle}"

    def leer_nuevos_eventos(self, max_eventos=20):
        """
        Consulta los eventos más recientes del canal System.
        Retorna lista de diccionarios con eventos NO VISTOS previamente.
        """
        cmd = [
            "wevtutil", "qe", "System",
            f"/q:{self.xpath_query}",
            f"/c:{max_eventos}",
            "/f:xml",
            "/rd:true"
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=4.0,
                creationflags=CREATION_FLAGS
            )

            if proc.returncode != 0:
                err_text = proc.stderr.lower()
                if "acceso denegado" in err_text or "access is denied" in err_text:
                    if not self.permiso_denegado:
                        self.permiso_denegado = True
                        return [{
                            "record_id": -1,
                            "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "nivel": "Aviso",
                            "origen": "Permisos Windows",
                            "id_evento": 0,
                            "mensaje": "ACCESO LIMITADO: La lectura del registro 'System' del Visor de Eventos requiere privilegios de Administrador en este equipo. Ejecuta el programa como Administrador si deseas registrar Kernel-Power 41 y WHEA.",
                            "es_critico": False,
                            "es_aviso": True
                        }]
                return []

            salida_xml = proc.stdout.strip()
            if not salida_xml:
                return []

            # Envolver en un nodo raíz para parsear múltiples fragmentos <Event>
            xml_completo = f"<Events>{salida_xml}</Events>"
            root = ET.fromstring(xml_completo)
            ns = {"ns": "http://schemas.microsoft.com/win/2004/08/events/event"}

            nuevos_eventos = []

            for ev in root.findall("ns:Event", ns):
                sys_elem = ev.find("ns:System", ns)
                if sys_elem is None:
                    continue

                rec_id_elem = sys_elem.find("ns:EventRecordID", ns)
                record_id = rec_id_elem.text if rec_id_elem is not None else None

                # Evitar duplicados
                if record_id in self.ids_vistos:
                    continue
                if record_id:
                    self.ids_vistos.add(record_id)

                ev_id_elem = sys_elem.find("ns:EventID", ns)
                ev_id = ev_id_elem.text if ev_id_elem is not None else "0"

                level_elem = sys_elem.find("ns:Level", ns)
                level_raw = level_elem.text if level_elem is not None else "4"

                prov_elem = sys_elem.find("ns:Provider", ns)
                provider = prov_elem.attrib.get("Name", "Desconocido") if prov_elem is not None else "Desconocido"

                time_elem = sys_elem.find("ns:TimeCreated", ns)
                sys_time = time_elem.attrib.get("SystemTime", "") if time_elem is not None else ""
                hora_local = self._convertir_utc_a_local(sys_time)

                # Clasificar nivel
                # Nivel 1 = Crítico, Nivel 2 = Error, Nivel 3 = Advertencia
                if level_raw == "1" or ev_id == "41":
                    nivel_str = "Crítico"
                    es_critico = True
                    es_aviso = False
                elif level_raw == "2":
                    nivel_str = "Error"
                    es_critico = True
                    es_aviso = False
                elif level_raw == "3" or "nvlddmkm" in provider.lower() or "whea" in provider.lower():
                    nivel_str = "Aviso"
                    es_critico = False
                    es_aviso = True
                else:
                    nivel_str = "Info"
                    es_critico = False
                    es_aviso = False

                # Extraer texto adicional de EventData
                data_elem = ev.find("ns:EventData", ns)
                datos_lista = []
                if data_elem is not None:
                    for d in data_elem.findall("ns:Data", ns):
                        if d.text and d.text.strip():
                            datos_lista.append(d.text.strip())
                detalle_texto = " | ".join(datos_lista[:4]) if datos_lista else "Sin datos adicionales"

                mensaje_diagnostico = self._generar_explicacion_diagnostica(
                    provider, ev_id, nivel_str, detalle_texto
                )

                nuevos_eventos.append({
                    "record_id": record_id,
                    "hora": hora_local,
                    "nivel": nivel_str,
                    "origen": provider,
                    "id_evento": ev_id,
                    "mensaje": mensaje_diagnostico,
                    "es_critico": es_critico,
                    "es_aviso": es_aviso
                })

            # Los eventos se devuelven en orden cronológico (más antiguo a más nuevo)
            nuevos_eventos.reverse()
            return nuevos_eventos

        except Exception as e:
            # No fallar nunca por excepciones en lectura de eventos
            return []


# ============================================================================
# INTERFAZ GRÁFICA DE USUARIO (TKINTER + MATPLOTLIB)
# ============================================================================
class MonitorGPUApp(tk.Tk):
    """
    Ventana principal de la aplicación con tarjetas de métricas, gráfica
    de doble eje en tiempo real y visor de eventos críticos del sistema.
    """
    def __init__(self):
        super().__init__()

        self.title("Monitor GPU RTX 4070 Ti & Diagnóstico de Sistema")
        self.geometry("1120x800")
        self.minsize(980, 700)
        self.configure(bg=COLOR_FONDO_APP)

        # Estado de la aplicación
        self.pausado = False
        self.cola_datos = queue.Queue()
        self.lector_gpu = LectorNvidiaSMI()
        self.lector_eventos = LectorEventosWindows()

        # Rutas de persistencia
        self.directorio_logs = obtener_directorio_logs()
        fecha_str = datetime.now().strftime("%Y%m%d")
        self.ruta_csv = os.path.join(self.directorio_logs, f"gpu_metricas_{fecha_str}.csv")
        self.ruta_txt = os.path.join(self.directorio_logs, f"eventos_sistema_{fecha_str}.txt")

        # Buffers para gráfica (últimos 300 puntos)
        self.historial_tiempos = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.historial_temp = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.historial_potencia = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.contador_muestras = 0

        # Alertas activas
        self.alerta_termica_activa = False
        self.alerta_potencia_activa = False

        # Construcción de la interfaz
        self._configurar_estilos()
        self._construir_interfaz()

        # Iniciar hilos de fondo y temporizador de GUI
        self.hilo_activo = True
        self.hilo_trabajador = threading.Thread(target=self._bucle_muestreo, daemon=True)
        self.hilo_trabajador.start()

        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self.after(200, self._procesar_cola_gui)

    def _configurar_estilos(self):
        """Configuración de estilos ttk para modo oscuro y tablas."""
        estilo = ttk.Style(self)
        estilo.theme_use("clam")

        # Treeview (Tabla de eventos)
        estilo.configure(
            "Treeview",
            background="#18181b",
            foreground="#f3f4f6",
            fieldbackground="#18181b",
            rowheight=26,
            font=("Segoe UI", 9)
        )
        estilo.configure(
            "Treeview.Heading",
            background="#27272a",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold")
        )
        estilo.map(
            "Treeview.Heading",
            background=[("active", "#3f3f46")]
        )
        estilo.map(
            "Treeview",
            background=[("selected", "#3b82f6")],
            foreground=[("selected", "#ffffff")]
        )

    def _construir_interfaz(self):
        """Diseño visual completo con cabecera, tarjetas, gráfica y log."""
        # 1. BARRA SUPERIOR (Cabecera y Controles)
        frame_cabecera = tk.Frame(self, bg=COLOR_FONDO_APP, padx=16, pady=12)
        frame_cabecera.pack(fill=tk.X)

        # Título y Modelo
        frame_titulos = tk.Frame(frame_cabecera, bg=COLOR_FONDO_APP)
        frame_titulos.pack(side=tk.LEFT)

        lbl_titulo = tk.Label(
            frame_titulos,
            text="MONITOR GPU & REGISTRO DE EVENTOS",
            font=("Segoe UI", 14, "bold"),
            fg=COLOR_TEXTO_PRINCIPAL,
            bg=COLOR_FONDO_APP
        )
        lbl_titulo.pack(anchor="w")

        lbl_subtitulo = tk.Label(
            frame_titulos,
            text="NVIDIA GeForce RTX 4070 Ti | Diagnóstico de Apagados y Pantallas Negras",
            font=("Segoe UI", 9),
            fg=COLOR_TEXTO_SECUNDARIO,
            bg=COLOR_FONDO_APP
        )
        lbl_subtitulo.pack(anchor="w")

        # Botones de Control (Derecha)
        frame_botones = tk.Frame(frame_cabecera, bg=COLOR_FONDO_APP)
        frame_botones.pack(side=tk.RIGHT)

        self.btn_pausar = tk.Button(
            frame_botones,
            text="⏸ Pausar",
            font=("Segoe UI", 9, "bold"),
            bg="#2563eb",
            fg="#ffffff",
            activebackground="#1d4ed8",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._alternar_pausa
        )
        self.btn_pausar.pack(side=tk.LEFT, padx=4)

        btn_limpiar = tk.Button(
            frame_botones,
            text="🧹 Limpiar Vista",
            font=("Segoe UI", 9),
            bg="#374151",
            fg="#ffffff",
            activebackground="#4b5563",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._limpiar_vista
        )
        btn_limpiar.pack(side=tk.LEFT, padx=4)

        btn_abrir_logs = tk.Button(
            frame_botones,
            text="📂 Abrir Carpeta de Logs",
            font=("Segoe UI", 9),
            bg="#059669",
            fg="#ffffff",
            activebackground="#047857",
            activeforeground="#ffffff",
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._abrir_carpeta_logs
        )
        btn_abrir_logs.pack(side=tk.LEFT, padx=4)

        # 2. BANNER DE ESTADO Y ALERTAS
        self.frame_banner = tk.Frame(self, bg="#1f2937", padx=16, pady=4)
        self.frame_banner.pack(fill=tk.X)

        self.lbl_banner = tk.Label(
            self.frame_banner,
            text="● Sistema de monitorización activo | Muestreo cada 2s",
            font=("Segoe UI", 9, "bold"),
            fg="#10b981",
            bg="#1f2937"
        )
        self.lbl_banner.pack(side=tk.LEFT)

        self.lbl_12v_nota = tk.Label(
            self.frame_banner,
            text="Línea +12V Fuente: No disponible por software (requiere multímetro)",
            font=("Segoe UI", 8, "italic"),
            fg="#9ca3af",
            bg="#1f2937"
        )
        self.lbl_12v_nota.pack(side=tk.RIGHT)

        # 3. TARJETAS DE MÉTRICAS (Grid de 5 tarjetas)
        frame_tarjetas = tk.Frame(self, bg=COLOR_FONDO_APP, padx=16, pady=10)
        frame_tarjetas.pack(fill=tk.X)

        for col in range(5):
            frame_tarjetas.columnconfigure(col, weight=1)

        self.tarjetas = {}
        metricas_info = [
            ("temp", "TEMPERATURA", "-- °C", COLOR_TEMP, "Alerta > 90 °C"),
            ("potencia", "CONSUMO ACTUAL", "-- W", COLOR_POTENCIA, "Alerta > 300 W"),
            ("limite", "LÍMITE DE POTENCIA", "-- W", COLOR_LIMITE, "TGP Configurado"),
            ("reloj", "RELOJ DE GPU", "-- MHz", COLOR_RELOJ, "Frecuencia Núcleo"),
            ("uso", "USO DE GPU", "-- %", COLOR_USO, "Carga de Proceso")
        ]

        for idx, (clave, titulo, val_inicial, color_acento, pie) in enumerate(metricas_info):
            card_frame = tk.Frame(
                frame_tarjetas,
                bg=COLOR_TARJETA,
                highlightbackground=COLOR_TARJETA_BORDE,
                highlightthickness=1,
                padx=10,
                pady=8
            )
            card_frame.grid(row=0, column=idx, padx=5, sticky="nsew")

            lbl_t = tk.Label(
                card_frame,
                text=titulo,
                font=("Segoe UI", 8, "bold"),
                fg=COLOR_TEXTO_SECUNDARIO,
                bg=COLOR_TARJETA
            )
            lbl_t.pack(anchor="w")

            lbl_v = tk.Label(
                card_frame,
                text=val_inicial,
                font=("Segoe UI", 17, "bold"),
                fg=color_acento,
                bg=COLOR_TARJETA
            )
            lbl_v.pack(anchor="w", pady=2)

            lbl_p = tk.Label(
                card_frame,
                text=pie,
                font=("Segoe UI", 8),
                fg=COLOR_TEXTO_SECUNDARIO,
                bg=COLOR_TARJETA
            )
            lbl_p.pack(anchor="w")

            self.tarjetas[clave] = {
                "frame": card_frame,
                "label_valor": lbl_v,
                "label_pie": lbl_p,
                "color_base": color_acento
            }

        # 4. ÁREA CENTRAL DIVIDIDA (Gráfica arriba, Visor de eventos abajo)
        paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        # Panel Superior: Gráfica en tiempo real (Matplotlib con doble eje)
        frame_grafica = tk.Frame(paned, bg=COLOR_FONDO_APP)
        paned.add(frame_grafica, weight=3)

        self._inicializar_grafica(frame_grafica)

        # Panel Inferior: Visor de eventos del sistema de Windows
        frame_eventos = tk.Frame(paned, bg=COLOR_FONDO_APP)
        paned.add(frame_eventos, weight=2)

        lbl_tit_eventos = tk.Label(
            frame_eventos,
            text="LOG DE EVENTOS DEL SISTEMA (Kernel-Power 41, WHEA-Logger, nvlddmkm, BugCheck 1001, Críticos / Errores)",
            font=("Segoe UI", 9, "bold"),
            fg=COLOR_TEXTO_PRINCIPAL,
            bg=COLOR_FONDO_APP
        )
        lbl_tit_eventos.pack(anchor="w", pady=(4, 2))

        # Tabla Treeview para eventos con scrollbar
        frame_tabla = tk.Frame(frame_eventos, bg=COLOR_FONDO_APP)
        frame_tabla.pack(fill=tk.BOTH, expand=True)

        columnas = ("hora", "nivel", "origen", "id", "mensaje")
        self.tabla_eventos = ttk.Treeview(
            frame_tabla,
            columns=columnas,
            show="headings",
            selectmode="extended"
        )
        self.tabla_eventos.heading("hora", text="Hora Local")
        self.tabla_eventos.heading("nivel", text="Nivel")
        self.tabla_eventos.heading("origen", text="Origen / Proveedor")
        self.tabla_eventos.heading("id", text="ID Evento")
        self.tabla_eventos.heading("mensaje", text="Descripción y Diagnóstico")

        self.tabla_eventos.column("hora", width=140, minwidth=120, stretch=False)
        self.tabla_eventos.column("nivel", width=80, minwidth=70, stretch=False)
        self.tabla_eventos.column("origen", width=180, minwidth=140, stretch=False)
        self.tabla_eventos.column("id", width=80, minwidth=70, stretch=False)
        self.tabla_eventos.column("mensaje", width=550, minwidth=300, stretch=True)

        scrollbar_y = ttk.Scrollbar(frame_tabla, orient=tk.VERTICAL, command=self.tabla_eventos.yview)
        self.tabla_eventos.configure(yscrollcommand=scrollbar_y.set)

        self.tabla_eventos.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_y.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags de colores para la tabla de eventos
        self.tabla_eventos.tag_configure("critico", foreground="#ff4d4d", background="#2a1215")
        self.tabla_eventos.tag_configure("error", foreground="#f87171")
        self.tabla_eventos.tag_configure("aviso", foreground="#f59e0b")
        self.tabla_eventos.tag_configure("info", foreground="#9ca3af")

        # 5. BARRA DE ESTADO INFERIOR
        frame_pie = tk.Frame(self, bg="#18181b", padx=16, pady=4)
        frame_pie.pack(fill=tk.X)

        self.lbl_estado_logs = tk.Label(
            frame_pie,
            text=f"Directorio de logs: {self.directorio_logs}",
            font=("Segoe UI", 8),
            fg=COLOR_TEXTO_SECUNDARIO,
            bg="#18181b"
        )
        self.lbl_estado_logs.pack(side=tk.LEFT)

        self.lbl_contador_muestras = tk.Label(
            frame_pie,
            text="Muestras: 0 | Eventos: 0",
            font=("Segoe UI", 8),
            fg=COLOR_TEXTO_SECUNDARIO,
            bg="#18181b"
        )
        self.lbl_contador_muestras.pack(side=tk.RIGHT)

    def _inicializar_grafica(self, contenedor):
        """Configura la figura de Matplotlib con tema oscuro y dos ejes Y independientes."""
        self.fig, self.ax_temp = plt.subplots(figsize=(8, 3.2), dpi=95)
        self.fig.patch.set_facecolor("#18181b")
        self.ax_temp.set_facecolor("#18181b")

        # Eje Izquierdo: Temperatura (°C)
        self.ax_temp.set_ylabel("Temperatura (°C)", color=COLOR_TEMP, fontsize=9, fontweight="bold")
        self.ax_temp.tick_params(axis="y", labelcolor=COLOR_TEMP, labelsize=8)
        self.ax_temp.tick_params(axis="x", labelcolor="#9ca3af", labelsize=8)
        self.ax_temp.set_ylim(20, 105)
        self.ax_temp.grid(True, linestyle="--", alpha=0.15, color="#ffffff")

        # Eje Derecho: Potencia (W)
        self.ax_pot = self.ax_temp.twinx()
        self.ax_pot.set_ylabel("Potencia (W)", color=COLOR_POTENCIA, fontsize=9, fontweight="bold")
        self.ax_pot.tick_params(axis="y", labelcolor=COLOR_POTENCIA, labelsize=8)
        self.ax_pot.set_ylim(0, 360)

        # Líneas de umbral crítico
        self.ax_temp.axhline(
            UMBRAL_TEMP_ALERTA, color="#ef4444", linestyle=":", linewidth=1.2, alpha=0.7,
            label=f"Límite {int(UMBRAL_TEMP_ALERTA)}°C"
        )
        self.ax_pot.axhline(
            UMBRAL_POTENCIA_ALERTA, color="#eab308", linestyle=":", linewidth=1.2, alpha=0.7,
            label=f"Límite {int(UMBRAL_POTENCIA_ALERTA)}W"
        )

        # Curvas de datos
        self.linea_temp, = self.ax_temp.plot([], [], color=COLOR_TEMP, linewidth=1.8, label="Temp (°C)")
        self.linea_pot, = self.ax_pot.plot([], [], color=COLOR_POTENCIA, linewidth=1.8, label="Potencia (W)")

        # Leyenda combinada
        lineas = [self.linea_temp, self.linea_pot]
        etiquetas = [l.get_label() for l in lineas]
        self.ax_temp.legend(
            lineas, etiquetas,
            loc="upper left",
            facecolor="#27272a",
            edgecolor="#3f3f46",
            labelcolor="#f3f4f6",
            fontsize=8
        )

        self.fig.tight_layout(pad=1.2)

        self.canvas_grafica = FigureCanvasTkAgg(self.fig, master=contenedor)
        widget_canvas = self.canvas_grafica.get_tk_widget()
        widget_canvas.pack(fill=tk.BOTH, expand=True)

    # ========================================================================
    # HILO DE MUESTREO EN SEGUNDO PLANO
    # ========================================================================
    def _bucle_muestreo(self):
        """
        Bucle secundario continuo que no bloquea la interfaz:
        - Consulta nvidia-smi cada 2 segundos.
        - Consulta eventos de Windows cada 3 segundos.
        - Persiste en CSV y TXT de forma segura inmediatamente.
        """
        tiempo_ultimo_gpu = 0.0
        tiempo_ultimo_eventos = 0.0

        # Encabezados de CSV
        encabezados_csv = ["timestamp", "temperatura_c", "consumo_w", "limite_w", "reloj_mhz", "uso_pct"]

        while self.hilo_activo:
            ahora = time.time()

            if not self.pausado:
                # 1. Muestreo de GPU (cada 2 segundos)
                if ahora - tiempo_ultimo_gpu >= INTERVALO_GPU_SEGUNDOS:
                    tiempo_ultimo_gpu = ahora
                    ts_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    metricas, error_gpu = self.lector_gpu.leer_metricas()
                    if metricas:
                        # Guardar inmediatamente en CSV
                        fila_csv = [
                            ts_actual,
                            metricas["temperatura"] if metricas["temperatura"] is not None else "",
                            metricas["potencia"] if metricas["potencia"] is not None else "",
                            metricas["limite_potencia"] if metricas["limite_potencia"] is not None else "",
                            metricas["reloj"] if metricas["reloj"] is not None else "",
                            metricas["uso"] if metricas["uso"] is not None else ""
                        ]
                        escribir_linea_csv(self.ruta_csv, fila_csv, encabezados_csv)

                        # Enviar a la GUI
                        self.cola_datos.put(("GPU_OK", ts_actual, metricas))
                    else:
                        self.cola_datos.put(("GPU_ERROR", ts_actual, error_gpu))

                # 2. Muestreo de Eventos del Sistema (cada 3 segundos)
                if ahora - tiempo_ultimo_eventos >= INTERVALO_EVENTOS_SEGUNDOS:
                    tiempo_ultimo_eventos = ahora
                    nuevos_eventos = self.lector_eventos.leer_nuevos_eventos(max_eventos=15)
                    if nuevos_eventos:
                        for ev in nuevos_eventos:
                            # Escribir en TXT inmediatamente
                            linea_txt = f"[{ev['hora']}] [{ev['nivel']}] [{ev['origen']} (ID: {ev['id_evento']})] {ev['mensaje']}"
                            escribir_linea_txt(self.ruta_txt, linea_txt)

                        self.cola_datos.put(("EVENTOS_NUEVOS", nuevos_eventos))

            time.sleep(0.1)

    # ========================================================================
    # ACTUALIZACIÓN DE INTERFAZ GRÁFICA
    # ========================================================================
    def _procesar_cola_gui(self):
        """Procesa los mensajes pendientes en la cola desde el hilo principal."""
        actualizar_grafica = False

        while not self.cola_datos.empty():
            try:
                tipo, *payload = self.cola_datos.get_nowait()

                if tipo == "GPU_OK":
                    ts_str, metricas = payload
                    self._actualizar_tarjetas(metricas)
                    self._agregar_punto_grafica(metricas)
                    actualizar_grafica = True
                    self.contador_muestras += 1

                elif tipo == "GPU_ERROR":
                    ts_str, err_msg = payload
                    self._mostrar_error_gpu(err_msg)

                elif tipo == "EVENTOS_NUEVOS":
                    nuevos_eventos = payload[0]
                    self._insertar_eventos_en_tabla(nuevos_eventos)

            except queue.Empty:
                break
            except Exception as e:
                print(f"[Error GUI] {e}", file=sys.stderr)

        if actualizar_grafica and not self.pausado:
            self._redibujar_grafica()

        # Actualizar contador de muestras en barra de estado
        cant_eventos = len(self.tabla_eventos.get_children())
        self.lbl_contador_muestras.config(
            text=f"Muestras: {self.contador_muestras} | Eventos registrados: {cant_eventos}"
        )

        # Siguiente comprobación en 200 ms
        if self.hilo_activo:
            self.after(200, self._procesar_cola_gui)

    def _actualizar_tarjetas(self, m):
        """Actualiza los textos y resalta alertas térmicas y de potencia."""
        temp = m.get("temperatura")
        pot = m.get("potencia")
        lim = m.get("limite_potencia")
        clk = m.get("reloj")
        uso = m.get("uso")

        # 1. Temperatura
        if temp is not None:
            self.tarjetas["temp"]["label_valor"].config(text=f"{temp:.1f} °C")
            if temp >= UMBRAL_TEMP_ALERTA:
                self._aplicar_estilo_alerta("temp", True, f"¡CRÍTICO! > {UMBRAL_TEMP_ALERTA}°C")
                if not self.alerta_termica_activa:
                    self.alerta_termica_activa = True
                    self._notificar_alerta_en_log("TÉRMICA", f"Temperatura GPU superó {UMBRAL_TEMP_ALERTA}°C (actual: {temp:.1f}°C)")
            else:
                self._aplicar_estilo_alerta("temp", False, "Alerta > 90 °C")
                self.alerta_termica_activa = False
        else:
            self.tarjetas["temp"]["label_valor"].config(text="N/D")

        # 2. Potencia
        if pot is not None:
            self.tarjetas["potencia"]["label_valor"].config(text=f"{pot:.1f} W")
            if pot >= UMBRAL_POTENCIA_ALERTA:
                self._aplicar_estilo_alerta("potencia", True, f"¡CRÍTICO! > {UMBRAL_POTENCIA_ALERTA}W")
                if not self.alerta_potencia_activa:
                    self.alerta_potencia_activa = True
                    self._notificar_alerta_en_log("POTENCIA", f"Consumo GPU superó {UMBRAL_POTENCIA_ALERTA}W (actual: {pot:.1f}W)")
            else:
                self._aplicar_estilo_alerta("potencia", False, "Alerta > 300 W")
                self.alerta_potencia_activa = False
        else:
            self.tarjetas["potencia"]["label_valor"].config(text="N/D")

        # 3. Límite de Potencia
        if lim is not None:
            self.tarjetas["limite"]["label_valor"].config(text=f"{lim:.1f} W")
        else:
            self.tarjetas["limite"]["label_valor"].config(text="N/D")

        # 4. Reloj
        if clk is not None:
            self.tarjetas["reloj"]["label_valor"].config(text=f"{clk} MHz")
        else:
            self.tarjetas["reloj"]["label_valor"].config(text="N/D")

        # 5. Uso
        if uso is not None:
            self.tarjetas["uso"]["label_valor"].config(text=f"{uso:.0f} %")
        else:
            self.tarjetas["uso"]["label_valor"].config(text="N/D")

        # Actualizar banner de estado general
        if self.alerta_termica_activa or self.alerta_potencia_activa:
            self.frame_banner.config(bg="#7f1d1d")
            self.lbl_banner.config(
                text="⚠️ ¡ALERTA DE SEGURIDAD DETECTADA! (Temperatura > 90°C o Consumo > 300W)",
                fg="#fef2f2",
                bg="#7f1d1d"
            )
            self.lbl_12v_nota.config(bg="#7f1d1d", fg="#fecaca")
        else:
            self.frame_banner.config(bg="#1f2937")
            self.lbl_banner.config(
                text="● Sistema de monitorización activo | Muestreo cada 2s | Datos sincronizados",
                fg="#10b981",
                bg="#1f2937"
            )
            self.lbl_12v_nota.config(bg="#1f2937", fg="#9ca3af")

    def _aplicar_estilo_alerta(self, clave_tarjeta, activa, texto_pie):
        """Aplica colores de advertencia a la tarjeta correspondiente."""
        card = self.tarjetas[clave_tarjeta]
        frame = card["frame"]
        lbl_pie = card["label_pie"]

        if activa:
            frame.config(bg=COLOR_TARJETA_ALERTA, highlightbackground=COLOR_BORDE_ALERTA, highlightthickness=2)
            lbl_pie.config(text=texto_pie, fg="#ef4444", bg=COLOR_TARJETA_ALERTA)
        else:
            frame.config(bg=COLOR_TARJETA, highlightbackground=COLOR_TARJETA_BORDE, highlightthickness=1)
            lbl_pie.config(text=texto_pie, fg=COLOR_TEXTO_SECUNDARIO, bg=COLOR_TARJETA)

    def _mostrar_error_gpu(self, mensaje_error):
        """Indica claramente en la interfaz si nvidia-smi no está disponible."""
        self.frame_banner.config(bg="#451a03")
        self.lbl_banner.config(
            text=f"⚠️ Aviso GPU: {mensaje_error} (El registro de eventos de Windows sigue funcionando)",
            fg="#fbbf24",
            bg="#451a03"
        )
        self.lbl_12v_nota.config(bg="#451a03", fg="#fed7aa")

        for clave in ("temp", "potencia", "limite", "reloj", "uso"):
            self.tarjetas[clave]["label_valor"].config(text="N/D")

    def _notificar_alerta_en_log(self, tipo_alerta, descripcion):
        """Inserta y persiste una advertencia visual directa generada por la aplicación."""
        ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ev_local = {
            "record_id": f"ALERTA_{int(time.time())}",
            "hora": ahora_str,
            "nivel": "Crítico",
            "origen": f"Alerta {tipo_alerta} GPU",
            "id_evento": "ALERTA",
            "mensaje": descripcion,
            "es_critico": True,
            "es_aviso": False
        }
        self._insertar_eventos_en_tabla([ev_local])
        linea_txt = f"[{ahora_str}] [CRÍTICO] [ALERTA GPU] {descripcion}"
        escribir_linea_txt(self.ruta_txt, linea_txt)

    def _insertar_eventos_en_tabla(self, lista_eventos):
        """Inserta eventos nuevos en la tabla con colores según severidad."""
        for ev in lista_eventos:
            if ev.get("es_critico"):
                tag = "critico"
            elif ev.get("es_aviso"):
                tag = "aviso"
            elif ev.get("nivel") == "Error":
                tag = "error"
            else:
                tag = "info"

            self.tabla_eventos.insert(
                "",
                0,  # Insertar al principio para ver los más recientes arriba
                values=(ev["hora"], ev["nivel"], ev["origen"], ev["id_evento"], ev["mensaje"]),
                tags=(tag,)
            )

    def _agregar_punto_grafica(self, metricas):
        """Agrega un nuevo punto a los buffers circulares de la gráfica."""
        self.historial_tiempos.append(self.contador_muestras)
        self.historial_temp.append(metricas.get("temperatura") or 0.0)
        self.historial_potencia.append(metricas.get("potencia") or 0.0)

    def _redibujar_grafica(self):
        """Actualiza los datos de las líneas de Matplotlib de forma ultra eficiente."""
        if not self.historial_tiempos:
            return

        x = list(self.historial_tiempos)
        y_temp = list(self.historial_temp)
        y_pot = list(self.historial_potencia)

        self.linea_temp.set_data(x, y_temp)
        self.linea_pot.set_data(x, y_pot)

        self.ax_temp.set_xlim(min(x), max(x) + 1 if max(x) > min(x) else min(x) + 5)

        # Ajuste dinámico si la temperatura o potencia superan los rangos base
        max_t = max(y_temp) if y_temp else 80
        if max_t > 100:
            self.ax_temp.set_ylim(20, max_t + 10)
        else:
            self.ax_temp.set_ylim(20, 105)

        max_p = max(y_pot) if y_pot else 250
        if max_p > 350:
            self.ax_pot.set_ylim(0, max_p + 30)
        else:
            self.ax_pot.set_ylim(0, 360)

        self.canvas_grafica.draw_idle()

    # ========================================================================
    # ACCIONES DE CONTROL DEL USUARIO
    # ========================================================================
    def _alternar_pausa(self):
        """Pausa o reanuda la captura y el registro de métricas."""
        self.pausado = not self.pausado
        if self.pausado:
            self.btn_pausar.config(text="▶ Reanudar", bg="#16a34a", activebackground="#15803d")
            self.lbl_banner.config(
                text="⏸ MONITORIZACIÓN EN PAUSA (No se están registrando datos)",
                fg="#facc15",
                bg="#1f2937"
            )
        else:
            self.btn_pausar.config(text="⏸ Pausar", bg="#2563eb", activebackground="#1d4ed8")
            self.lbl_banner.config(
                text="● Sistema de monitorización activo | Muestreo cada 2s",
                fg="#10b981",
                bg="#1f2937"
            )

    def _limpiar_vista(self):
        """Limpia los elementos visibles en pantalla (no borra los archivos en disco)."""
        # Limpiar tabla de eventos
        for fila in self.tabla_eventos.get_children():
            self.tabla_eventos.delete(fila)

        # Limpiar buffers de la gráfica
        self.historial_tiempos.clear()
        self.historial_temp.clear()
        self.historial_potencia.clear()
        self.linea_temp.set_data([], [])
        self.linea_pot.set_data([], [])
        self.canvas_grafica.draw_idle()

        # Resetear tarjetas
        for clave in ("temp", "potencia", "limite", "reloj", "uso"):
            self.tarjetas[clave]["label_valor"].config(text="--")
            self._aplicar_estilo_alerta(clave, False, "Vista reiniciada")

        self.lbl_contador_muestras.config(text="Muestras: 0 | Eventos registrados: 0")

    def _abrir_carpeta_logs(self):
        """Abre la carpeta de persistencia en el Explorador de archivos de Windows."""
        try:
            if not os.path.exists(self.directorio_logs):
                os.makedirs(self.directorio_logs, exist_ok=True)

            if sys.platform == "win32":
                os.startfile(self.directorio_logs)
            else:
                subprocess.Popen(["xdg-open", self.directorio_logs])
        except Exception as e:
            messagebox.showerror(
                "Error",
                f"No se pudo abrir la carpeta de logs:\n{self.directorio_logs}\n\nDetalle: {e}"
            )

    def _al_cerrar(self):
        """Cierre ordenado de los hilos de fondo."""
        self.hilo_activo = False
        self.destroy()


# ============================================================================
# PUNTO DE ENTRADA PRINCIPAL
# ============================================================================
def main():
    app = MonitorGPUApp()
    app.mainloop()


if __name__ == "__main__":
    main()
