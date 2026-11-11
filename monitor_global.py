#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
MONITOR GLOBAL DEL PC & DIAGNÓSTICO TOTAL DE FALLOS (HARDWARE VS SOFTWARE)
==============================================================================
Aplicación todo-en-uno para Windows 10/11 que monitoriza en tiempo real:
- GPU (NVIDIA RTX): Temperatura, Potencia, Relojes, Uso, VRAM y Ventiladores.
- CPU: Carga %, Frecuencia de núcleos, detección de Thermal Throttling.
- Memoria RAM: Uso de RAM física, saturación de archivo de paginación / Swap.
- Almacenamiento (Disco SSD / HDD): Salud S.M.A.R.T., espacio, actividad I/O.
- Alimentación / Cables: Detección de picos, caídas de carril PCIe (WHEA) y
  cortes bruscos de energía (Kernel-Power 41 con análisis de fallo eléctrico).
- Diagnóstico inteligente: Clasifica en tiempo real si el fallo es de
  HARDWARE (fuente, cables, refrigeración, VRAM, disco) o del SISTEMA OPERATIVO / JUEGO.

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

# Configurar directorio temporal seguro para matplotlib
if "MPLCONFIGDIR" not in os.environ:
    os.environ["MPLCONFIGDIR"] = os.path.join(tempfile.gettempdir(), "matplotlib_monitor_global")

import psutil
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ============================================================================
# CONSTANTES Y CONFIGURACIÓN
# ============================================================================
INTERVALO_MUESTREO = 2.0         # Muestreo de sensores cada 2 segundos
INTERVALO_EVENTOS = 3.0          # Muestreo de visor de eventos cada 3 segundos
MAX_PUNTOS_GRAFICA = 300         # Últimos 300 puntos (10 minutos de histórico)

# Umbrales críticos
UMBRAL_GPU_TEMP = 90.0           # Alerta si Temp GPU > 90 °C
UMBRAL_GPU_POTENCIA = 300.0      # Alerta si Potencia GPU > 300 W
UMBRAL_RAM_PCT = 92.0            # Alerta si RAM > 92% (posible fuga o saturación)
UMBRAL_CPU_TEMP_THROTTLE = 95.0  # Límite térmico estimado CPU

# Paleta de colores Dark Gaming
COLOR_FONDO = "#111113"
COLOR_TARJETA = "#1a1a20"
COLOR_TARJETA_BORDE = "#282834"
COLOR_TEXTO = "#f3f4f6"
COLOR_TEXTO_MUTED = "#9ca3af"

# Acentos temáticos
COLOR_GPU = "#ff5555"            # Rojo coral
COLOR_POTENCIA = "#00e5ff"       # Cyan neón
COLOR_CPU = "#f59e0b"            # Ámbar
COLOR_RAM = "#a855f7"            # Púrpura neón
COLOR_DISCO = "#10b981"          # Verde esmeralda
COLOR_ALIMENTACION = "#ec4899"   # Rosa fucsia

CREATION_FLAGS = 0
if sys.platform == "win32":
    CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0x08000000


# ============================================================================
# PERSISTENCIA Y CARPETAS
# ============================================================================
def obtener_carpeta_logs():
    """Ruta en el Escritorio: Escritorio\\DiagnosticoGlobal_logs."""
    perfil = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    escritorios = [os.path.join(perfil, "Desktop"), os.path.join(perfil, "Escritorio")]
    escritorio = escritorios[0]
    for p in escritorios:
        if os.path.isdir(p):
            escritorio = p
            break
    ruta_dir = os.path.join(escritorio, "DiagnosticoGlobal_logs")
    try:
        os.makedirs(ruta_dir, exist_ok=True)
    except Exception:
        ruta_dir = os.path.join(os.getcwd(), "DiagnosticoGlobal_logs")
        os.makedirs(ruta_dir, exist_ok=True)
    return ruta_dir


def escribir_seguro_csv(ruta_csv, fila, encabezados):
    """Escribe en CSV con fsync inmediato para proteger contra cortes de luz."""
    try:
        nuevo = not os.path.exists(ruta_csv) or os.path.getsize(ruta_csv) == 0
        with open(ruta_csv, mode="a", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            if nuevo:
                escritor.writerow(encabezados)
            escritor.writerow(fila)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"[Error CSV] {e}", file=sys.stderr)


def escribir_seguro_txt(ruta_txt, linea):
    """Escribe en TXT con fsync inmediato."""
    try:
        with open(ruta_txt, mode="a", encoding="utf-8") as f:
            f.write(linea + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"[Error TXT] {e}", file=sys.stderr)


# ============================================================================
# LECTOR GLOBAL DE HARDWARE (GPU, CPU, RAM, DISCO, SMART)
# ============================================================================
class LectorGlobalHardware:
    """Gestiona la lectura integral de todos los subsistemas del PC."""
    def __init__(self):
        self.ruta_smi = self._buscar_smi()
        self.disco_smart_estado = "Verificando..."
        self._actualizar_smart_disco()
        self.freq_base_cpu = None

    def _buscar_smi(self):
        s = shutil.which("nvidia-smi")
        if s:
            return s
        rutas = [
            os.path.expandvars(r"%SystemRoot%\System32\nvidia-smi.exe"),
            r"C:\Windows\System32\nvidia-smi.exe",
            os.path.expandvars(r"%ProgramFiles%\NVIDIA Corporation\NVSMI\nvidia-smi.exe"),
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
        for r in rutas:
            if os.path.isfile(r):
                return r
        return None

    def _actualizar_smart_disco(self):
        """Consulta el estado S.M.A.R.T. del disco principal mediante WMI/PowerShell."""
        try:
            cmd = ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_DiskDrive | Select-Object -ExpandProperty Status"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, creationflags=CREATION_FLAGS)
            if res.returncode == 0 and res.stdout.strip():
                estado = res.stdout.strip().splitlines()[0]
                self.disco_smart_estado = "SALUDABLE (OK)" if estado.upper() == "OK" else f"¡ALERTA! ({estado})"
            else:
                self.disco_smart_estado = "OK (Estándar)"
        except Exception:
            self.disco_smart_estado = "OK (No accesible)"

    def leer_todo(self):
        """Lee CPU, RAM, Disco y GPU de manera unificada."""
        # 1. CPU
        cpu_pct = psutil.cpu_percent(interval=None)
        cpu_freq_info = psutil.cpu_freq()
        cpu_freq_actual = cpu_freq_info.current if cpu_freq_info else 0.0

        if self.freq_base_cpu is None and cpu_freq_actual > 0:
            self.freq_base_cpu = cpu_freq_actual

        # Detectar Throttling si la CPU baja un 40% de su frecuencia con alta carga
        cpu_throttling = False
        if cpu_pct > 60 and self.freq_base_cpu and cpu_freq_actual < (self.freq_base_cpu * 0.6):
            cpu_throttling = True

        # 2. RAM & Paginación
        vmem = psutil.virtual_memory()
        ram_usada_gb = vmem.used / (1024**3)
        ram_total_gb = vmem.total / (1024**3)
        ram_pct = vmem.percent

        swap = psutil.swap_memory()
        swap_pct = swap.percent

        # 3. Disco C:
        try:
            disk = psutil.disk_usage("C:\\")
            disco_libre_gb = disk.free / (1024**3)
            disco_total_gb = disk.total / (1024**3)
            disco_pct = disk.percent
        except Exception:
            disco_libre_gb, disco_total_gb, disco_pct = 0, 0, 0

        # 4. GPU (nvidia-smi extendido)
        gpu_datos = {
            "temp": None,
            "potencia": None,
            "limite_potencia": None,
            "reloj": None,
            "uso": None,
            "vram_usada_mb": None,
            "vram_total_mb": None,
            "fan_pct": None,
            "disponible": False,
            "error": "nvidia-smi no detectado"
        }

        if self.ruta_smi:
            cmd = [
                self.ruta_smi,
                "--query-gpu=temperature.gpu,power.draw,power.limit,clocks.gr,utilization.gpu,memory.used,memory.total,fan.speed",
                "--format=csv,noheader,nounits"
            ]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.5, creationflags=CREATION_FLAGS)
                if res.returncode == 0 and res.stdout.strip():
                    partes = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
                    def to_float(val):
                        try: return float(val)
                        except Exception: return None

                    if len(partes) >= 8:
                        gpu_datos.update({
                            "temp": to_float(partes[0]),
                            "potencia": to_float(partes[1]),
                            "limite_potencia": to_float(partes[2]),
                            "reloj": to_float(partes[3]),
                            "uso": to_float(partes[4]),
                            "vram_usada_mb": to_float(partes[5]),
                            "vram_total_mb": to_float(partes[6]),
                            "fan_pct": to_float(partes[7]),
                            "disponible": True,
                            "error": None
                        })
                else:
                    gpu_datos["error"] = "Sin respuesta del driver"
            except Exception as e:
                gpu_datos["error"] = str(e)

        return {
            "cpu_pct": cpu_pct,
            "cpu_freq": cpu_freq_actual,
            "cpu_throttling": cpu_throttling,
            "ram_usada_gb": ram_usada_gb,
            "ram_total_gb": ram_total_gb,
            "ram_pct": ram_pct,
            "swap_pct": swap_pct,
            "disco_libre_gb": disco_libre_gb,
            "disco_total_gb": disco_total_gb,
            "disco_pct": disco_pct,
            "disco_smart": self.disco_smart_estado,
            "gpu": gpu_datos
        }


# ============================================================================
# MOTOR DE DIAGNÓSTICO INTELIGENTE: HARDWARE VS SISTEMA OPERATIVO
# ============================================================================
class MotorDiagnosticoEventos:
    """
    Lee los eventos de Windows (System y Application) y los clasifica
    rigurosamente entre Fallo de Hardware/Alimentación y Fallo del SO/Software.
    """
    def __init__(self):
        self.ids_vistos = set()
        # Consulta XPath que cubre:
        # - Kernel-Power 41 (Apagón brusco / caída de fuente)
        # - WHEA-Logger (Fallos CPU / bus PCIe / memoria)
        # - nvlddmkm (Fallo de núcleo GPU o VRAM)
        # - BugCheck 1001 (BSOD)
        # - Disk / Storage (Sectores dañados, Timeouts)
        # - Nivel 1 (Crítico) y Nivel 2 (Error)
        self.xpath = (
            "*[System["
            "(Level=1 or Level=2) "
            "or (Provider[@Name='Microsoft-Windows-Kernel-Power'] and EventID=41) "
            "or (Provider[@Name='Microsoft-Windows-WHEA-Logger']) "
            "or (Provider[@Name='nvlddmkm']) "
            "or (EventID=1001) "
            "or (Provider[@Name='disk'] or Provider[@Name='stornvme'] or Provider[@Name='storahci'] or Provider[@Name='ntfs'])"
            "]]"
        )

    def _convertir_hora(self, iso_str):
        try:
            if not iso_str:
                return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            base = iso_str.split(".")[0]
            dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return iso_str[:19].replace("T", " ")

    def clasificar_diagnostico(self, proveedor, id_evento, nivel_str, detalle):
        """
        Determina si la causa es:
        - [CRÍTICO/HW] HARDWARE: ALIMENTACIÓN / CABLES / FUENTE
        - [AVISO/GPU] HARDWARE: GPU / VRAM / REFRIGERACIÓN
        - [CRÍTICO/HW] HARDWARE: DISCO DURO / SSD
        - [CRÍTICO/HW] HARDWARE: CPU / BUS PCIE / MEMORIA FÍSICA
        - [SOFTWARE/SO] SISTEMA OPERATIVO / SOFTWARE / JUEGO
        """
        prov_l = proveedor.lower()
        id_s = str(id_evento)

        # 1. Alimentación y corte eléctrico brusco
        if "kernel-power" in prov_l and id_s == "41":
            return {
                "origen_causa": "[CRÍTICO/HW] HARDWARE / ALIMENTACIÓN",
                "tag_color": "critico_hardware",
                "veredicto": "CORTE DE ENERGÍA / APAGADO EN SECO",
                "explicacion": "El PC se apagó de golpe sin pasar por Windows. Causas habituales: disparo de protección de la fuente (OCP por pico de consumo), cable 12VHPWR con mal contacto o fallo en condensadores."
            }

        # 2. Hardware interno (CPU, PCIe, RAM física)
        if "whea-logger" in prov_l:
            return {
                "origen_causa": "[CRÍTICO/HW] HARDWARE / PROCESADOR O PCIE",
                "tag_color": "critico_hardware",
                "veredicto": f"ERROR FÍSICO WHEA (ID {id_s})",
                "explicacion": f"Error detectado por la arquitectura de hardware de Windows. Posible cable PCIe flojo, inestabilidad de carril en placa base o microcorte de voltaje en CPU/VRAM. ({detalle})"
            }

        # 3. Disco duro o SSD
        if any(d in prov_l for d in ["disk", "stornvme", "storahci", "ntfs"]) or id_s in ["7", "11", "51", "153"]:
            return {
                "origen_causa": "[CRÍTICO/HW] HARDWARE / ALMACENAMIENTO",
                "tag_color": "critico_hardware",
                "veredicto": f"ERROR EN DISCO/SSD ({proveedor} ID {id_s})",
                "explicacion": f"Fallo de lectura/escritura, bloque dañado o tiempo de espera agotado (Timeout) del disco. Puede congelar el PC por completo. ({detalle})"
            }

        # 4. GPU y Memoria VRAM
        if "nvlddmkm" in prov_l:
            return {
                "origen_causa": "[AVISO/GPU] HARDWARE / GPU Y VRAM",
                "tag_color": "aviso_gpu",
                "veredicto": "FALLO TDR NVIDIA (DRIVER/VRAM)",
                "explicacion": f"El procesador gráfico o la memoria VRAM se congelaron y el driver intentó recuperarse (pantalla negra en juego). Causas: memoria VRAM inestable, sobrecalentamiento o caída de tensión. ({detalle})"
            }

        # 5. Pantallazo Azul (BSOD)
        if id_s == "1001" or "bugcheck" in prov_l:
            return {
                "origen_causa": "[CRÍTICO/HW] CRÍTICO / BSOD",
                "tag_color": "critico_hardware",
                "veredicto": "PANTALLA AZUL DE WINDOWS",
                "explicacion": f"El sistema se detuvo por un fallo grave. Parámetros de parada: {detalle}"
            }

        # 6. Fallos de Aplicaciones y Sistema Operativo
        if "application error" in prov_l or "application hang" in prov_l or id_s in ["1000", "1002"]:
            return {
                "origen_causa": "[SOFTWARE/SO] SOFTWARE / SISTEMA OPERATIVO",
                "tag_color": "software_so",
                "veredicto": "CUELGUE DE APLICACIÓN O JUEGO",
                "explicacion": f"El fallo proviene del juego o programa (.exe/.dll), no del hardware. El hardware físico funcionó con normalidad. ({detalle})"
            }

        # Fallos genéricos de Windows
        if nivel_str == "Crítico":
            return {
                "origen_causa": "[CRÍTICO/HW] CRÍTICO GENERAL",
                "tag_color": "critico_hardware",
                "veredicto": f"EVENTO CRÍTICO ({proveedor})",
                "explicacion": detalle
            }
        elif nivel_str == "Error":
            return {
                "origen_causa": "[ERROR/SISTEMA] ERROR DE SISTEMA",
                "tag_color": "error_sistema",
                "veredicto": f"Error ({proveedor} ID {id_s})",
                "explicacion": detalle
            }
        else:
            return {
                "origen_causa": "[INFO] AVISO INFORMATIVO",
                "tag_color": "info",
                "veredicto": f"Aviso ({proveedor})",
                "explicacion": detalle
            }

    def leer_eventos(self, max_ev=15):
        """Consulta wevtutil en el canal System."""
        cmd = ["wevtutil", "qe", "System", f"/q:{self.xpath}", f"/c:{max_ev}", "/f:xml", "/rd:true"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3.5, creationflags=CREATION_FLAGS)
            if res.returncode != 0 or not res.stdout.strip():
                return []

            xml_str = f"<Events>{res.stdout.strip()}</Events>"
            root = ET.fromstring(xml_str)
            ns = {"ns": "http://schemas.microsoft.com/win/2004/08/events/event"}

            nuevos = []
            for ev in root.findall("ns:Event", ns):
                sys_elem = ev.find("ns:System", ns)
                if sys_elem is None:
                    continue

                rec_id = sys_elem.find("ns:EventRecordID", ns)
                record_id = rec_id.text if rec_id is not None else None

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
                hora_local = self._convertir_hora(sys_time)

                nivel_str = "Crítico" if level_raw == "1" else ("Error" if level_raw == "2" else "Aviso")

                # Extraer datos adicionales
                data_elem = ev.find("ns:EventData", ns)
                datos = []
                if data_elem is not None:
                    for d in data_elem.findall("ns:Data", ns):
                        if d.text and d.text.strip():
                            datos.append(d.text.strip())
                detalle = " | ".join(datos[:4]) if datos else "Sin detalles adicionales"

                # Clasificación inteligente
                diag = self.clasificar_diagnostico(provider, ev_id, nivel_str, detalle)

                nuevos.append({
                    "record_id": record_id,
                    "hora": hora_local,
                    "origen_causa": diag["origen_causa"],
                    "veredicto": diag["veredicto"],
                    "explicacion": diag["explicacion"],
                    "tag_color": diag["tag_color"],
                    "id_evento": ev_id,
                    "proveedor": provider
                })

            nuevos.reverse()
            return nuevos
        except Exception:
            return []


# ============================================================================
# INTERFAZ GRÁFICA TOTAL (TKINTER + MATPLOTLIB)
# ============================================================================
class MonitorGlobalApp(tk.Tk):
    """Ventana principal del monitor integral de diagnóstico."""
    def __init__(self):
        super().__init__()

        self.title("Monitor Global del PC & Diagnóstico Total (Hardware vs Software)")
        self.geometry("1200x860")
        self.minsize(1050, 720)
        self.configure(bg=COLOR_FONDO)

        # Estado y sincronización
        self.pausado = False
        self.hilo_activo = True
        self.cola = queue.Queue()

        self.lector_hw = LectorGlobalHardware()
        self.motor_diag = MotorDiagnosticoEventos()

        # Archivos de persistencia
        self.dir_logs = obtener_carpeta_logs()
        fecha_str = datetime.now().strftime("%Y%m%d")
        self.ruta_csv = os.path.join(self.dir_logs, f"pc_metricas_globales_{fecha_str}.csv")
        self.ruta_txt = os.path.join(self.dir_logs, f"pc_diagnostico_eventos_{fecha_str}.txt")

        # Históricos de gráfica (300 puntos)
        self.x_data = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.gpu_temp_data = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.gpu_pot_data = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.cpu_pct_data = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.ram_pct_data = deque(maxlen=MAX_PUNTOS_GRAFICA)
        self.muestras_total = 0

        # Construir interfaz y arrancar
        self._configurar_estilos()
        self._construir_ui()

        self.hilo = threading.Thread(target=self._bucle_fondo, daemon=True)
        self.hilo.start()

        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self.after(200, self._procesar_cola)

    def _configurar_estilos(self):
        estilo = ttk.Style(self)
        estilo.theme_use("clam")
        estilo.configure(
            "Treeview",
            background="#16161a",
            foreground="#f3f4f6",
            fieldbackground="#16161a",
            rowheight=26,
            font=("Segoe UI", 9)
        )
        estilo.configure(
            "Treeview.Heading",
            background="#23232c",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 9, "bold")
        )
        estilo.map("Treeview.Heading", background=[("active", "#373744")])
        estilo.map("Treeview", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])

    def _construir_ui(self):
        # 1. Cabecera y Controles
        frame_cabecera = tk.Frame(self, bg=COLOR_FONDO, padx=16, pady=10)
        frame_cabecera.pack(fill=tk.X)

        frame_tit = tk.Frame(frame_cabecera, bg=COLOR_FONDO)
        frame_tit.pack(side=tk.LEFT)

        lbl_t = tk.Label(frame_tit, text="DIAGNÓSTICO TOTAL DEL PC & CAJA NEGRA", font=("Segoe UI", 15, "bold"), fg=COLOR_TEXTO, bg=COLOR_FONDO)
        lbl_t.pack(anchor="w")

        lbl_sub = tk.Label(frame_tit, text="GPU • CPU • RAM • Disco SSD • Alimentación / Cables • Diagnóstico Hardware vs Sistema Operativo", font=("Segoe UI", 9), fg=COLOR_TEXTO_MUTED, bg=COLOR_FONDO)
        lbl_sub.pack(anchor="w")

        frame_btns = tk.Frame(frame_cabecera, bg=COLOR_FONDO)
        frame_btns.pack(side=tk.RIGHT)

        self.btn_pausa = tk.Button(frame_btns, text="⏸ Pausar", font=("Segoe UI", 9, "bold"), bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8", relief="flat", padx=12, pady=5, cursor="hand2", command=self._alternar_pausa)
        self.btn_pausa.pack(side=tk.LEFT, padx=4)

        btn_limpiar = tk.Button(frame_btns, text="🧹 Limpiar Vista", font=("Segoe UI", 9), bg="#374151", fg="#ffffff", activebackground="#4b5563", relief="flat", padx=12, pady=5, cursor="hand2", command=self._limpiar_vista)
        btn_limpiar.pack(side=tk.LEFT, padx=4)

        btn_logs = tk.Button(frame_btns, text="📂 Abrir Carpeta de Logs", font=("Segoe UI", 9), bg="#059669", fg="#ffffff", activebackground="#047857", relief="flat", padx=12, pady=5, cursor="hand2", command=self._abrir_logs)
        btn_logs.pack(side=tk.LEFT, padx=4)

        # 2. Banner de Estado Global Inteligente
        self.frame_banner = tk.Frame(self, bg="#1e293b", padx=16, pady=6)
        self.frame_banner.pack(fill=tk.X)

        self.lbl_banner = tk.Label(
            self.frame_banner,
            text="● ESTADO DEL SISTEMA: ÓPTIMO | Monitorizando GPU, CPU, RAM, Disco y Suministro Eléctrico",
            font=("Segoe UI", 9, "bold"),
            fg="#10b981",
            bg="#1e293b"
        )
        self.lbl_banner.pack(side=tk.LEFT)

        self.lbl_cable_diag = tk.Label(
            self.frame_banner,
            text="Sensor Eléctrico: Activo (Monitor de cortes Kernel-Power 41 y transitorios)",
            font=("Segoe UI", 8, "italic"),
            fg="#94a3b8",
            bg="#1e293b"
        )
        self.lbl_cable_diag.pack(side=tk.RIGHT)

        # 3. Dashboard de Tarjetas de Sensores (5 Tarjetas Temáticas)
        frame_tarjetas = tk.Frame(self, bg=COLOR_FONDO, padx=16, pady=8)
        frame_tarjetas.pack(fill=tk.X)

        for c in range(5):
            frame_tarjetas.columnconfigure(c, weight=1)

        self.tarjetas = {}

        def crear_tarjeta(col, titulo, color_borde, info_lineas):
            f = tk.Frame(frame_tarjetas, bg=COLOR_TARJETA, highlightbackground=color_borde, highlightthickness=1, padx=10, pady=8)
            f.grid(row=0, column=col, padx=4, sticky="nsew")

            lbl_tit = tk.Label(f, text=titulo, font=("Segoe UI", 9, "bold"), fg=color_borde, bg=COLOR_TARJETA)
            lbl_tit.pack(anchor="w")

            labels = {}
            for clave, texto_def, font_sz, es_bold in info_lineas:
                lbl = tk.Label(f, text=texto_def, font=("Segoe UI", font_sz, "bold" if es_bold else "normal"), fg=COLOR_TEXTO if es_bold else COLOR_TEXTO_MUTED, bg=COLOR_TARJETA)
                lbl.pack(anchor="w", pady=1)
                labels[clave] = lbl

            return {"frame": f, "labels": labels, "color_base": color_borde}

        # Tarjeta 1: GPU & VRAM
        self.tarjetas["gpu"] = crear_tarjeta(0, " TARJETA GPU & VRAM", COLOR_GPU, [
            ("temp", "-- °C", 14, True),
            ("potencia", "-- W / -- W", 9, False),
            ("reloj", "-- MHz (Uso: -- %)", 9, False),
            ("vram", "VRAM: -- / -- GB", 9, False),
            ("fan", "Ventilador: -- %", 8, False)
        ])

        # Tarjeta 2: CPU & Refrigeración
        self.tarjetas["cpu"] = crear_tarjeta(1, " PROCESADOR (CPU)", COLOR_CPU, [
            ("uso", "Carga: -- %", 14, True),
            ("freq", "Frecuencia: -- MHz", 9, False),
            ("throttle", "Throttling: Normal", 9, False),
            ("cores", f"Hilos: {psutil.cpu_count() or 8}", 8, False)
        ])

        # Tarjeta 3: Memoria RAM & Paginación
        self.tarjetas["ram"] = crear_tarjeta(2, " MEMORIA RAM & SWAP", COLOR_RAM, [
            ("ram_pct", "RAM: -- %", 14, True),
            ("ram_gb", "-- / -- GB Usados", 9, False),
            ("swap", "Paginación: -- %", 9, False),
            ("estado", "Presión: Normal", 8, False)
        ])

        # Tarjeta 4: Disco SSD / HDD
        self.tarjetas["disco"] = crear_tarjeta(3, " ALMACENAMIENTO (DISCO)", COLOR_DISCO, [
            ("smart", "S.M.A.R.T.: OK", 12, True),
            ("espacio", "C: -- GB Libres", 9, False),
            ("uso_disco", "Ocupado: -- %", 9, False),
            ("estado_io", "I/O: Normal (0 errores)", 8, False)
        ])

        # Tarjeta 5: Alimentación & Cables
        self.tarjetas["pwr"] = crear_tarjeta(4, " ENERGÍA / CABLES / PSU", COLOR_ALIMENTACION, [
            ("cables", "Línea 12V: Estable", 12, True),
            ("cortes", "Cortes Súbitos: 0", 9, False),
            ("bus_pcie", "Enlace PCIe: Estable", 9, False),
            ("picos", "Picos Potencia: Normal", 8, False)
        ])

        # 4. Paneles Centrales (Gráfica de doble eje + Visor de diagnóstico)
        paned = ttk.Panedwindow(self, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        # Gráfica
        frame_graf = tk.Frame(paned, bg=COLOR_FONDO)
        paned.add(frame_graf, weight=3)
        self._iniciar_grafica(frame_graf)

        # Visor de Diagnóstico
        frame_diag = tk.Frame(paned, bg=COLOR_FONDO)
        paned.add(frame_diag, weight=3)

        lbl_tit_tabla = tk.Label(
            frame_diag,
            text="MOTOR DE DIAGNÓSTICO EN TIEMPO REAL (Diferenciación de Fallo de Hardware vs Sistema Operativo / Software)",
            font=("Segoe UI", 9, "bold"),
            fg=COLOR_TEXTO,
            bg=COLOR_FONDO
        )
        lbl_tit_tabla.pack(anchor="w", pady=(4, 2))

        frame_tabla = tk.Frame(frame_diag, bg=COLOR_FONDO)
        frame_tabla.pack(fill=tk.BOTH, expand=True)

        cols = ("hora", "origen_causa", "veredicto", "explicacion")
        self.tabla = ttk.Treeview(frame_tabla, columns=cols, show="headings", selectmode="extended")
        self.tabla.heading("hora", text="Hora")
        self.tabla.heading("origen_causa", text="Clasificación / Veredicto")
        self.tabla.heading("veredicto", text="Evento Detectado")
        self.tabla.heading("explicacion", text="Diagnóstico y Causa Raíz Explicada")

        self.tabla.column("hora", width=130, minwidth=110, stretch=False)
        self.tabla.column("origen_causa", width=220, minwidth=180, stretch=False)
        self.tabla.column("veredicto", width=220, minwidth=170, stretch=False)
        self.tabla.column("explicacion", width=500, minwidth=300, stretch=True)

        scr_y = ttk.Scrollbar(frame_tabla, orient=tk.VERTICAL, command=self.tabla.yview)
        self.tabla.configure(yscrollcommand=scr_y.set)

        self.tabla.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scr_y.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags de colores por origen de causa
        self.tabla.tag_configure("critico_hardware", foreground="#ff4d4d", background="#2a1215")
        self.tabla.tag_configure("aviso_gpu", foreground="#f59e0b")
        self.tabla.tag_configure("software_so", foreground="#38bdf8")
        self.tabla.tag_configure("error_sistema", foreground="#f87171")
        self.tabla.tag_configure("info", foreground="#9ca3af")

        # 5. Barra inferior
        frame_pie = tk.Frame(self, bg="#16161a", padx=16, pady=4)
        frame_pie.pack(fill=tk.X)

        self.lbl_pie_logs = tk.Label(frame_pie, text=f"Directorio de logs: {self.dir_logs}", font=("Segoe UI", 8), fg=COLOR_TEXTO_MUTED, bg="#16161a")
        self.lbl_pie_logs.pack(side=tk.LEFT)

        self.lbl_pie_stats = tk.Label(frame_pie, text="Muestras: 0 | Eventos clasificados: 0", font=("Segoe UI", 8), fg=COLOR_TEXTO_MUTED, bg="#16161a")
        self.lbl_pie_stats.pack(side=tk.RIGHT)

    def _iniciar_grafica(self, contenedor):
        self.fig, self.ax1 = plt.subplots(figsize=(8, 3.2), dpi=95)
        self.fig.patch.set_facecolor("#18181b")
        self.ax1.set_facecolor("#18181b")

        # Eje Izquierdo: Temperaturas y porcentajes
        self.ax1.set_ylabel("Temp GPU (°C) / Carga CPU-RAM (%)", color=COLOR_GPU, fontsize=8, fontweight="bold")
        self.ax1.tick_params(axis="y", labelcolor=COLOR_GPU, labelsize=8)
        self.ax1.tick_params(axis="x", labelcolor="#9ca3af", labelsize=8)
        self.ax1.set_ylim(0, 110)
        self.ax1.grid(True, linestyle="--", alpha=0.12, color="#ffffff")

        # Eje Derecho: Potencia GPU
        self.ax2 = self.ax1.twinx()
        self.ax2.set_ylabel("Potencia GPU (W)", color=COLOR_POTENCIA, fontsize=8, fontweight="bold")
        self.ax2.tick_params(axis="y", labelcolor=COLOR_POTENCIA, labelsize=8)
        self.ax2.set_ylim(0, 360)

        # Líneas horizontales de alerta
        self.ax1.axhline(UMBRAL_GPU_TEMP, color="#ef4444", linestyle=":", linewidth=1.1, label="Alerta 90°C")
        self.ax2.axhline(UMBRAL_GPU_POTENCIA, color="#eab308", linestyle=":", linewidth=1.1, label="Alerta 300W")

        # Curvas de datos
        self.line_gpu_temp, = self.ax1.plot([], [], color=COLOR_GPU, linewidth=1.7, label="GPU Temp (°C)")
        self.line_cpu_pct, = self.ax1.plot([], [], color=COLOR_CPU, linewidth=1.2, alpha=0.8, label="CPU (%)")
        self.line_ram_pct, = self.ax1.plot([], [], color=COLOR_RAM, linewidth=1.2, alpha=0.8, label="RAM (%)")
        self.line_gpu_pot, = self.ax2.plot([], [], color=COLOR_POTENCIA, linewidth=1.7, label="GPU Potencia (W)")

        lines = [self.line_gpu_temp, self.line_gpu_pot, self.line_cpu_pct, self.line_ram_pct]
        self.ax1.legend(lines, [l.get_label() for l in lines], loc="upper left", facecolor="#27272a", edgecolor="#3f3f46", labelcolor="#f3f4f6", fontsize=7)

        self.fig.tight_layout(pad=1.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=contenedor)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ========================================================================
    # HILO DE MUESTREO EN SEGUNDO PLANO
    # ========================================================================
    def _bucle_fondo(self):
        t_hw = 0.0
        t_ev = 0.0

        encabezados_csv = [
            "timestamp", "gpu_temp_c", "gpu_potencia_w", "gpu_limite_w", "gpu_reloj_mhz", "gpu_uso_pct",
            "gpu_vram_usada_mb", "gpu_vram_total_mb", "gpu_fan_pct",
            "cpu_uso_pct", "cpu_freq_mhz", "cpu_throttling",
            "ram_usada_gb", "ram_total_gb", "ram_pct", "swap_pct",
            "disco_libre_gb", "disco_smart"
        ]

        while self.hilo_activo:
            ahora = time.time()

            if not self.pausado:
                # 1. Telemetría de sensores cada 2s
                if ahora - t_hw >= INTERVALO_MUESTREO:
                    t_hw = ahora
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    datos = self.lector_hw.leer_todo()

                    g = datos["gpu"]
                    fila = [
                        ts,
                        g["temp"] if g["temp"] is not None else "",
                        g["potencia"] if g["potencia"] is not None else "",
                        g["limite_potencia"] if g["limite_potencia"] is not None else "",
                        g["reloj"] if g["reloj"] is not None else "",
                        g["uso"] if g["uso"] is not None else "",
                        g["vram_usada_mb"] if g["vram_usada_mb"] is not None else "",
                        g["vram_total_mb"] if g["vram_total_mb"] is not None else "",
                        g["fan_pct"] if g["fan_pct"] is not None else "",
                        f"{datos['cpu_pct']:.1f}",
                        f"{datos['cpu_freq']:.0f}",
                        "SI" if datos["cpu_throttling"] else "NO",
                        f"{datos['ram_usada_gb']:.2f}",
                        f"{datos['ram_total_gb']:.2f}",
                        f"{datos['ram_pct']:.1f}",
                        f"{datos['swap_pct']:.1f}",
                        f"{datos['disco_libre_gb']:.1f}",
                        datos["disco_smart"]
                    ]
                    escribir_seguro_csv(self.ruta_csv, fila, encabezados_csv)
                    self.cola.put(("HW", ts, datos))

                # 2. Diagnóstico de Eventos cada 3s
                if ahora - t_ev >= INTERVALO_EVENTOS:
                    t_ev = ahora
                    nuevos = self.motor_diag.leer_eventos(15)
                    if nuevos:
                        for ev in nuevos:
                            linea = f"[{ev['hora']}] [{ev['origen_causa']}] [{ev['veredicto']}] {ev['explicacion']}"
                            escribir_seguro_txt(self.ruta_txt, linea)
                        self.cola.put(("EV", nuevos))

            time.sleep(0.1)

    # ========================================================================
    # ACTUALIZACIÓN DE INTERFAZ
    # ========================================================================
    def _procesar_cola(self):
        redibujar = False

        while not self.cola.empty():
            try:
                tipo, *payload = self.cola.get_nowait()
                if tipo == "HW":
                    ts, d = payload
                    self._actualizar_tarjetas_sensores(d)
                    self._actualizar_datos_grafica(d)
                    self.muestras_total += 1
                    redibujar = True
                elif tipo == "EV":
                    nuevos_eventos = payload[0]
                    self._insertar_eventos_diagnostico(nuevos_eventos)
            except queue.Empty:
                break
            except Exception as e:
                print(f"[Error GUI] {e}", file=sys.stderr)

        if redibujar and not self.pausado:
            self._redibujar_grafica()

        cant_ev = len(self.tabla.get_children())
        self.lbl_pie_stats.config(text=f"Muestras registradas: {self.muestras_total} | Eventos de diagnóstico: {cant_ev}")

        if self.hilo_activo:
            self.after(200, self._procesar_cola)

    def _actualizar_tarjetas_sensores(self, d):
        g = d["gpu"]
        c_labels = self.tarjetas["cpu"]["labels"]
        r_labels = self.tarjetas["ram"]["labels"]
        d_labels = self.tarjetas["disco"]["labels"]
        p_labels = self.tarjetas["pwr"]["labels"]
        g_labels = self.tarjetas["gpu"]["labels"]

        # GPU
        if g["disponible"]:
            temp = g["temp"]
            pot = g["potencia"]
            g_labels["temp"].config(text=f"{temp:.1f} °C" if temp else "N/D")
            g_labels["potencia"].config(text=f"Potencia: {pot:.1f} W (Lím: {g['limite_potencia'] or '--'} W)")
            g_labels["reloj"].config(text=f"Reloj: {g['reloj'] or '--'} MHz (Carga: {g['uso'] or '--'} %)")

            if g["vram_usada_mb"] and g["vram_total_mb"]:
                usada_gb = g["vram_usada_mb"] / 1024
                tot_gb = g["vram_total_mb"] / 1024
                vram_pct = (g["vram_usada_mb"] / g["vram_total_mb"]) * 100
                g_labels["vram"].config(text=f"VRAM: {usada_gb:.1f} / {tot_gb:.1f} GB ({vram_pct:.0f}%)")
            else:
                g_labels["vram"].config(text="VRAM: N/D")

            g_labels["fan"].config(text=f"Ventiladores: {g['fan_pct']:.0f} %" if g["fan_pct"] is not None else "Ventiladores: Auto")

            # Alerta GPU
            if (temp and temp >= UMBRAL_GPU_TEMP) or (pot and pot >= UMBRAL_GPU_POTENCIA):
                self.tarjetas["gpu"]["frame"].config(bg="#4a1217", highlightbackground="#ef4444", highlightthickness=2)
            else:
                self.tarjetas["gpu"]["frame"].config(bg=COLOR_TARJETA, highlightbackground=COLOR_GPU, highlightthickness=1)
        else:
            g_labels["temp"].config(text="NVIDIA-SMI N/D")
            g_labels["potencia"].config(text=f"Aviso: {g['error'][:25]}")
            g_labels["reloj"].config(text="Reloj: N/D")
            g_labels["vram"].config(text="VRAM: N/D")

        # CPU
        c_labels["uso"].config(text=f"Carga: {d['cpu_pct']:.0f} %")
        c_labels["freq"].config(text=f"Frecuencia: {d['cpu_freq']:.0f} MHz")
        if d["cpu_throttling"]:
            c_labels["throttle"].config(text="¡THROTTLING DETECTADO!", fg="#ef4444")
            self.tarjetas["cpu"]["frame"].config(bg="#4a1217", highlightbackground="#ef4444", highlightthickness=2)
        else:
            c_labels["throttle"].config(text="Throttling: Normal (Estable)", fg=COLOR_TEXTO_MUTED)
            self.tarjetas["cpu"]["frame"].config(bg=COLOR_TARJETA, highlightbackground=COLOR_CPU, highlightthickness=1)

        # RAM
        r_labels["ram_pct"].config(text=f"RAM: {d['ram_pct']:.0f} %")
        r_labels["ram_gb"].config(text=f"{d['ram_usada_gb']:.1f} / {d['ram_total_gb']:.1f} GB Ocupados")
        r_labels["swap"].config(text=f"Paginación / Swap: {d['swap_pct']:.0f} %")
        if d["ram_pct"] >= UMBRAL_RAM_PCT:
            r_labels["estado"].config(text="¡RAM SATURADA! (Cuelgues)", fg="#ef4444")
        else:
            r_labels["estado"].config(text="Presión: Normal", fg=COLOR_TEXTO_MUTED)

        # DISCO
        d_labels["smart"].config(text=f"S.M.A.R.T.: {d['disco_smart']}")
        d_labels["espacio"].config(text=f"C: {d['disco_libre_gb']:.0f} GB Libres ({d['disco_total_gb']:.0f} GB)")
        d_labels["uso_disco"].config(text=f"Espacio Usado: {d['disco_pct']:.0f} %")

    def _actualizar_datos_grafica(self, d):
        self.x_data.append(self.muestras_total)
        g = d["gpu"]
        self.gpu_temp_data.append(g["temp"] if g["temp"] else 0.0)
        self.gpu_pot_data.append(g["potencia"] if g["potencia"] else 0.0)
        self.cpu_pct_data.append(d["cpu_pct"])
        self.ram_pct_data.append(d["ram_pct"])

    def _redibujar_grafica(self):
        if not self.x_data:
            return
        x = list(self.x_data)
        self.line_gpu_temp.set_data(x, list(self.gpu_temp_data))
        self.line_gpu_pot.set_data(x, list(self.gpu_pot_data))
        self.line_cpu_pct.set_data(x, list(self.cpu_pct_data))
        self.line_ram_pct.set_data(x, list(self.ram_pct_data))

        self.ax1.set_xlim(min(x), max(x) + 1 if max(x) > min(x) else min(x) + 5)
        self.canvas.draw_idle()

    def _insertar_eventos_diagnostico(self, eventos):
        hay_corte_electrico = False
        hay_fallo_hardware = False

        for ev in eventos:
            if "ALIMENTACIÓN" in ev["origen_causa"]:
                hay_corte_electrico = True
            elif "HARDWARE" in ev["origen_causa"]:
                hay_fallo_hardware = True

            self.tabla.insert(
                "",
                0,
                values=(ev["hora"], ev["origen_causa"], ev["veredicto"], ev["explicacion"]),
                tags=(ev["tag_color"],)
            )

        # Actualizar banner superior según diagnóstico
        if hay_corte_electrico:
            self.frame_banner.config(bg="#7f1d1d")
            self.lbl_banner.config(
                text="[ALERTA] ¡FALLO DE ALIMENTACIÓN O CORTE ELÉCTRICO DETECTADO! (Verificar cable 12VHPWR o Fuente de alimentación)",
                fg="#fef2f2",
                bg="#7f1d1d"
            )
            self.tarjetas["pwr"]["labels"]["cortes"].config(text="¡Corte Súbito Detectado!", fg="#ef4444")
            self.tarjetas["pwr"]["frame"].config(bg="#4a1217", highlightbackground="#ef4444", highlightthickness=2)
        elif hay_fallo_hardware:
            self.frame_banner.config(bg="#7c2d12")
            self.lbl_banner.config(
                text="[AVISO] INESTABILIDAD DE HARDWARE REPORTADA (Verificar errores WHEA, VRAM o Refrigeración en la tabla)",
                fg="#fef3c7",
                bg="#7c2d12"
            )

    # ========================================================================
    # CONTROLES DE USUARIO
    # ========================================================================
    def _alternar_pausa(self):
        self.pausado = not self.pausado
        if self.pausado:
            self.btn_pausa.config(text="▶ Reanudar", bg="#16a34a")
            self.lbl_banner.config(text="⏸ MONITORIZACIÓN PAUSADA", fg="#facc15", bg="#1e293b")
        else:
            self.btn_pausa.config(text="⏸ Pausar", bg="#2563eb")
            self.lbl_banner.config(text="● SISTEMA ACTIVO | Monitorizando todos los componentes en tiempo real", fg="#10b981", bg="#1e293b")

    def _limpiar_vista(self):
        for f in self.tabla.get_children():
            self.tabla.delete(f)
        self.x_data.clear()
        self.gpu_temp_data.clear()
        self.gpu_pot_data.clear()
        self.cpu_pct_data.clear()
        self.ram_pct_data.clear()
        self.line_gpu_temp.set_data([], [])
        self.line_gpu_pot.set_data([], [])
        self.line_cpu_pct.set_data([], [])
        self.line_ram_pct.set_data([], [])
        self.canvas.draw_idle()
        self.lbl_pie_stats.config(text="Muestras: 0 | Eventos clasificados: 0")

    def _abrir_logs(self):
        try:
            if sys.platform == "win32":
                os.startfile(self.dir_logs)
            else:
                subprocess.Popen(["xdg-open", self.dir_logs])
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo abrir la carpeta:\n{e}")

    def _al_cerrar(self):
        self.hilo_activo = False
        self.destroy()


# ============================================================================
# ENTRADA PRINCIPAL
# ============================================================================
def main():
    app = MonitorGlobalApp()
    app.mainloop()


if __name__ == "__main__":
    main()
