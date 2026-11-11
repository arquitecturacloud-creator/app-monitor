El código fuente completo está en monitor_global.py y el ejecutable portable compilado listo para usar en dist/DiagnosticoGlobal_PC.exe.

¿Cómo diagnostica cada componente del equipo?
Componente	¿Qué monitoriza en tiempo real?	¿Cómo detecta si falla?
🎮 Tarjeta Gráfica (GPU)	Temp, Consumo (W), Reloj (MHz) y Carga (%)	Alerta visual si supera 90 °C o 300 W.
🧠 Memoria VRAM	Uso exacto de VRAM en GB y porcentaje (vram.used / vram.total)	Si un chip de VRAM falla o se corrompe una textura, registra eventos nvlddmkm (TDR) indicando que el driver de vídeo se colgó y reinició.
❄️ Refrigeración	Velocidad de los ventiladores (fan.speed %) y curva térmica	Si los ventiladores no giran, la bomba falla o la pasta térmica está seca, detecta subidas drásticas de temperatura y caídas de frecuencias (Thermal Throttling).
⚡ Procesador (CPU)	Carga global (%), hilos activos y frecuencia actual (MHz)	Detección de Throttling térmico: si la frecuencia de la CPU cae bruscamente bajo carga pesada (>40%), avisa de estrangulamiento térmico por disipador defectuoso.
🧬 Memoria RAM & Paginación	RAM física usada (GB / %) y saturación del archivo de intercambio (Swap/Pagefile)	Detecta fugas de memoria y saturación (>92%). Los errores de hardware en los módulos RAM disparan advertencias WHEA-Logger o pantallazos azules PAGE_FAULT_IN_NONPAGED_AREA.
💾 Disco Duro / SSD	Salud S.M.A.R.T. nativa, espacio libre y actividad I/O	Captura en tiempo real sectores dañados, fallos de lectura y Timeouts de controlador (Eventos disk 7, 11, 153 o stornvme/storahci) que congelan el PC por completo.
🔌 Fuente / Cables / Voltajes	Monitor de transitorios de potencia, integridad del bus PCIe y sensor de corte	Detecta falsos contactos en cables (12VHPWR) o caídas de tensión: Cuando un cable flojo o una fuente insuficiente provocan una caída en la línea de 12V, el bus PCIe se desestabiliza (eventos WHEA) o la fuente se apaga en seco (evento Kernel-Power 41 con Bugcheck 0).
¿Cómo sabe si la culpa es del Hardware o del Sistema Operativo?

La aplicación incorpora un Motor de Diagnóstico Inteligente que clasifica cada anomalía en la tabla inferior con etiquetas de colores muy claras:

🔴 [CRÍTICO / HARDWARE] (Fallo Físico de Componentes o Alimentación):
Kernel-Power 41: El equipo se apagó de golpe sin pasar por Windows. Causa: Salto de protección de la fuente (OCP/OPP), cable de corriente/12VHPWR flojo o fallo en condensadores.
WHEA-Logger: Error físico de la arquitectura de hardware (inestabilidad en procesador, bus PCIe o memoria RAM).
Errores Disk / Storage: Fallo físico en el SSD o cable SATA/NVMe.
🟠 [AVISO / GPU Y VRAM] (Fallo de Tarjeta Gráfica):
nvlddmkm TDR: Pantalla negra en juego. El procesador gráfico o la memoria de vídeo se congelaron y el driver intentó resetearse. Causa: Frecuencia inestable, caída de voltaje del rail GPU o chip VRAM degradado.
🔵 [SOFTWARE / SISTEMA OPERATIVO] (Fallo de Windows o del Juego):
Application Error 1000 / Application Hang 1002: El juego o programa se congeló o cerró al escritorio por un error de programación (.exe o .dll).
Veredicto: El hardware está 100% sano, el problema es un bug del juego, un archivo corrupto de Windows o un controlador incompatible.
Lo que verás en la Pantalla
Banner Superior de Seguridad:
Cambia dinámicamente de Verde (Sistema Óptimo) a Naranja (Inestabilidad detectada) o Rojo Intenso (¡Fallo de Alimentación o Corte Eléctrico detectado!).
5 Paneles de Estado:
Tarjeta GPU: Temperatura, potencia actual vs límite, reloj, uso de VRAM y ventiladores.
Tarjeta CPU: Carga, frecuencia actual y sensor de Throttling térmico.
Tarjeta RAM: Memoria física y virtual ocupada.
Tarjeta Disco: Estado de salud S.M.A.R.T. y espacio libre.
Tarjeta Alimentación: Estado de la línea 12V y contador de apagados súbitos.
Gráfica Multi-Variable en Tiempo Real:
Superpone la Temperatura de la GPU, el Consumo en Vatios, el Uso de la CPU y el Uso de la RAM de los últimos 10 minutos (300 muestras continuas).
Tabla de Veredicto Diagnóstico:
Explica en español claro qué componente ha fallado y qué cable o pieza debes revisar.
Dónde quedan guardados los Registros

Todo se guarda automáticamente en tu Escritorio: 📁 C:\Usuarios\<TuUsuario>\Escritorio\DiagnosticoGlobal_logs\

pc_metricas_globales_YYYYMMDD.csv: Tabla con todas las variables de GPU, CPU, RAM y Disco segundo a segundo.
pc_diagnostico_eventos_YYYYMMDD.txt: Registro explicativo de fallos con la distinción de Hardware vs Software.

🛡️ Protección ante apagones súbitos: Cada línea se graba y se vacía físicamente en el SSD con fsync de inmediato. Si el PC se apaga en seco por un cable flojo o un corte de energía, la última muestra antes del apagón queda intacta.

¿Cómo probarlo y verificarlo?
Ve a la carpeta dist\ del proyecto: …\Downloads\APP-GM\dist\DiagnosticoGlobal_PC.exe
Haz doble clic sobre DiagnosticoGlobal_PC.exe (no requiere instalar nada).
(Opcional): Para que Windows le conceda permiso de auditar todos los detalles del visor de eventos de hardware, puedes ejecutarlo como Administrador (clic derecho > Ejecutar como administrador).
Juega con normalidad; si ocurre un apagón o una pantalla negra, al volver a encender el equipo abre el programa o la carpeta de logs para ver el veredicto exacto.
