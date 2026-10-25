"""El guardián: si hay una sesión abierta y su motor no está corriendo, la retoma.

Lo corre el Programador de tareas de Windows (LoudnessLab.exe --asegurar) al iniciar sesión y cada
2 minutos. Así una grabación sigue después de un reinicio, un apagado, un corte de luz o una caída
del motor, aunque nadie abra la ventana. Es rápido y no hace nada si no hay sesión abierta o si su
motor ya corre.

No es un servicio de Windows a propósito: el nombre de la canción (el panel multimedia) y los
reproductores solo existen dentro de la sesión del usuario. Por eso, después de reiniciar, la
grabación se retoma cuando alguien inicia sesión.
"""

import logging
import os
import subprocess
import sys
from xml.sax.saxutils import escape

from . import protocolo, sistema

NOMBRE_TAREA = r"\LoudnessLab\Guardian"
CADA = "PT2M"

log = logging.getLogger("loudness")


def asegurar():
    """Lo que hace la tarea programada. Devuelve la carpeta que mandó a retomar, o None."""
    ab = protocolo.sesion_abierta()
    if not ab or protocolo.motor_vivo():
        return None
    try:
        h = logging.FileHandler(sistema.carpeta_app() / "motor.log", encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.info("Guardián: la sesión «%s» está abierta y su motor no corre; se retoma.", ab["carpeta"])
    except OSError:
        pass
    sistema.lanzar_desacoplado(sistema.comando_motor("--motor", "--continuar", ab["carpeta"]))
    return ab["carpeta"]


def _xml(comando, argumentos, carpeta, usuario):
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Loudness Lab: si una grabación quedó abierta y su motor no está corriendo (reinicio, apagado, corte de luz o caída), la retoma. Si no hay una grabación abierta, no hace nada.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(usuario)}</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>{CADA}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(usuario)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(comando)}</Command>
      <Arguments>{escape(argumentos)}</Arguments>
      <WorkingDirectory>{escape(carpeta)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _comando():
    cmd = sistema.comando_propio("--asegurar")
    return cmd[0], subprocess.list2cmdline(cmd[1:])


def _texto(b):
    for cod in ("oem", "mbcs", "utf-8"):
        try:
            return b.decode(cod)
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("utf-8", errors="replace")


def registrar(nombre=NOMBRE_TAREA, comando=None, argumentos=None):
    """Crea (o reemplaza) la tarea programada del guardián para el usuario actual."""
    if comando is None:
        comando, argumentos = _comando()
    usuario = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    ruta = sistema.carpeta_app() / "guardian.xml"
    ruta.write_text(_xml(comando, argumentos or "", os.path.dirname(comando), usuario), encoding="utf-16")
    r = sistema.correr_oculto(["schtasks", "/Create", "/TN", nombre, "/XML", str(ruta), "/F"])
    if r.returncode != 0:
        raise RuntimeError(_texto(r.stderr or r.stdout).strip() or f"schtasks terminó con código {r.returncode}")


def registrada(nombre=NOMBRE_TAREA, comando=None):
    """La tarea existe y apunta a este programa."""
    if comando is None:
        comando, _ = _comando()
    r = sistema.correr_oculto(["schtasks", "/Query", "/TN", nombre, "/XML"])
    if r.returncode != 0:
        return False
    return escape(comando).lower() in _texto(r.stdout).lower()


def borrar(nombre=NOMBRE_TAREA):
    sistema.correr_oculto(["schtasks", "/Delete", "/TN", nombre, "/F"])


def al_dia():
    """Lo llama la ventana al abrirse: deja la tarea registrada y apuntando a este .exe. Solo con
    la app empaquetada (en desarrollo, la tarea apuntaría a un Python que puede cambiar)."""
    if not getattr(sys, "frozen", False):
        return "en desarrollo: el guardián no se registra"
    if os.environ.get("LOUDNESS_SIN_GUARDIAN"):   # (solo pruebas de un .exe fuera de su lugar)
        return "desactivado"
    if registrada():
        return "ok"
    registrar()
    return "registrada"


def main():
    asegurar()
    return 0
