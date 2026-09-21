# -*- coding: utf-8 -*-
"""Simulacion de las integraciones externas (capa 4 del documento).

UTP Assistant no posee credenciales reales de Jira, Google Calendar o
CRM en la demostracion. Este modulo reproduce el comportamiento de esos
sistemas con almacenamiento en memoria (se resetea al reiniciar la app)
e implementa los controles de diseno del informe:

- Grounding: verifica que cada cita_textual exista en la fuente.
- Idempotencia: no duplica tickets, contactos ni eventos por hilo.
- Lista blanca de destinatarios: solo se permite invitar a correos del
  hilo o del dominio corporativo (mitigacion del Riesgo 2).
- Aprobacion humana: las acciones de escritura no confirmadas entran
  a PENDIENTES y esperan un clic en el panel de revision.
- Trazabilidad: cada mutacion queda registrada con su run_id, lo que
  permite auditar y revertir (apartado 6).
"""

import re
import unicodedata
from datetime import datetime, date, timedelta

DOMINIO_CORPORATIVO = "utpconsult.com"

SECUENCIAL = {"jira": 482, "crm": 10327, "evento": 900001, "caso": 1, "pendiente": 1}

CRM = {
    "lucia.vela@acme.com": {"nombre": "Lucia Vela", "empresa": "Acme SAC", "cargo": "Gerenta de TI", "estado": "en_negociacion"},
}

JIRA = {}

AGENDA = {
    "pm@utpconsult.com": {},
    "ventas@utpconsult.com": {},
}

EVENTOS = {}

ESCALADOS = []

PENDIENTES = []

REGISTRO_HILOS = {}   # id_hilo_correo -> set de correos vistos en el hilo
MUTACIONES = {}       # run_id -> lista de mutaciones revertibles
RUNS = {}             # run_id -> metadatos del run


def _nuevo_id(prefijo, sector):
    n = SECUENCIAL[sector]
    SECUENCIAL[sector] += 1
    return f"{prefijo}-{n}"


def _normalizar(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip().lower()


def _extraer_correo(texto):
    coincidencias = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", texto or "")
    return coincidencias[0].lower() if coincidencias else ""


def _dias_laborales(inicio, fin):
    dias = []
    d = datetime.strptime(inicio, "%Y-%m-%d").date()
    hasta = datetime.strptime(fin, "%Y-%m-%d").date()
    while d <= hasta and len(dias) < 10:
        if d.weekday() < 5:
            dias.append(d)
        d += timedelta(days=1)
    return dias


# ---------------------------------------------------------------------------
# Trazabilidad: participantes del hilo, run_id, mutaciones y reversibilidad
# ---------------------------------------------------------------------------

def registrar_participante(id_hilo, remitente):
    if not id_hilo:
        return
    correo = _extraer_correo(remitente)
    if correo:
        REGISTRO_HILOS.setdefault(id_hilo, set()).add(correo)


def lista_blanca(id_hilo):
    base = set(REGISTRO_HILOS.get(id_hilo) or set())
    base.update({"pm@utpconsult.com", "ventas@utpconsult.com"})
    return {c for c in base if c}


def _permite_invitar(correo, id_hilo):
    if not correo:
        return False
    return correo in lista_blanca(id_hilo) or correo.endswith("@" + DOMINIO_CORPORATIVO)


def _registrar_mutacion(run_id, mut):
    if not run_id:
        return
    MUTACIONES.setdefault(run_id, []).append(mut)
    runs = RUNS.setdefault(run_id, {"fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "id_hilo": mut.get("id_hilo"), "mutaciones": 0})
    runs["mutaciones"] = len(MUTACIONES[run_id])
    runs["id_hilo"] = mut.get("id_hilo")


def revertir_run(run_id):
    """Revierte todas las mutaciones registradas para un run."""
    global ESCALADOS, PENDIENTES
    if run_id not in MUTACIONES:
        return False
    for mut in reversed(MUTACIONES[run_id]):
        tipo = mut.get("tipo")
        if tipo == "ticket":
            JIRA.pop(mut.get("clave"), None)
        elif tipo == "contacto_creado":
            CRM.pop(mut.get("correo"), None)
        elif tipo == "contacto_actualizado":
            if mut.get("estado_anterior") is not None:
                CRM[mut.get("correo")] = dict(mut["estado_anterior"])
        elif tipo == "evento":
            EVENTOS.pop(mut.get("clave"), None)
        elif tipo == "escalamiento":
            ESCALADOS = [e for e in ESCALADOS if e.get("caso") != mut.get("clave")]
        elif tipo == "pendiente":
            PENDIENTES = [p for p in PENDIENTES if p.get("id") != mut.get("clave")]
    MUTACIONES.pop(run_id, None)
    RUNS.pop(run_id, None)
    return True


def listar_runs():
    return [{"run_id": rid, **RUNS[rid]} for rid in reversed(list(RUNS.keys()))]


def auditoria_trazable(run_id):
    """Devuelve las mutaciones de un run para mostrarlas en el panel."""
    return list(MUTACIONES.get(run_id, []))


# ---------------------------------------------------------------------------
# Herramientas (catálogo del punto 4 del informe)
# ---------------------------------------------------------------------------

def consultar_disponibilidad_calendario(args):
    inicio = args.get("fecha_inicio")
    fin = args.get("fecha_fin")
    duracion = args.get("duracion_minutos", 60)
    participantes = args.get("correos_participantes") or ["pm@utpconsult.com", "ventas@utpconsult.com"]
    if not inicio or not fin:
        return {"ok": False, "error": "faltan fecha_inicio o fecha_fin"}
    objetivo = args.get("_semana_objetivo")
    if objetivo:
        li, lf = int(inicio.replace("-", "")), int(fin.replace("-", ""))
        oi, of = int(objetivo["inicio"].replace("-", "")), int(objetivo["fin"].replace("-", ""))
        if not (oi - 10 <= li <= lf <= of + 10):
            return {"ok": False, "error": "rango_de_fechas_invalido",
                    "sugerencia": f"Las franjas de 'la proxima semana' son del {objetivo['inicio']} al {objetivo['fin']}. Usa ese rango."}
    franjas = []
    for dia in _dias_laborales(inicio, fin):
        for hora in (9, 11, 15, 16):
            if hora + (duracion // 60) > 18:
                continue
            franjas.append({
                "inicio": f"{dia.isoformat()}T{hora:02d}:00:00-05:00",
                "duracion_minutos": duracion,
                "participantes": participantes,
            })
        if len(franjas) >= 3:
            break
    return {"ok": True, "franjas_disponibles": franjas[:3], "equipo": participantes}


def _hay_evento_para_hilo(id_hilo):
    if not id_hilo:
        return False
    for pid, pend in enumerate(PENDIENTES):
        if pend.get("tipo") == "reunion" and pend["datos"].get("id_hilo") == id_hilo and pend.get("estado") == "pendiente":
            return pend
    for eid in EVENTOS:
        if EVENTOS[eid].get("id_hilo") == id_hilo:
            return eid
    return False


def agendar_reunion_en_google_calendar(args):
    id_hilo = args.get("_id_hilo")
    requiere = args.get("requiere_confirmacion_humana", True)
    invitados = args.get("correos_invitados", [])
    titulo = args.get("titulo", "Reunion UTPConsult")
    inicio = args.get("fecha_hora_inicio", "")
    duracion = args.get("duracion_minutos", 60)

    existente = _hay_evento_para_hilo(id_hilo)
    if existente:
        return {"ok": True, "evento_duplicado": True,
                "detalle": "Ya hay una reunion (borrador o confirmada) para este hilo.", "ref": existente}

    fuera = [c for c in invitados if not _permite_invitar(c, id_hilo)]
    if fuera:
        caso = _registrar_escalamiento(
            "posible_inyeccion_de_prompt",
            f"Se intento invitar a direcciones fuera de la lista blanca: {', '.join(fuera)}.",
            "alta", id_hilo, args.get("_run_id"))
        return {"ok": False, "error": "destinatario_fuera_de_lista_blanca",
                "destinatarios_bloqueados": fuera,
                "permitidos": sorted(lista_blanca(id_hilo)),
                "detalle": "Solo se permite invitar a correos presentes en el hilo o del dominio corporativo.",
                "caso_id": caso}

    evento_id = _nuevo_id("EVT", "evento")
    if requiere:
        pid = _nuevo_id("P", "pendiente")
        PENDIENTES.append({
            "id": pid,
            "tipo": "reunion",
            "estado": "pendiente",
            "descripcion": f'Reunion en borrador: "{titulo}" ({inicio}, {duracion} min). Invitados: {", ".join(invitados)}.',
            "datos": {"evento_id": evento_id, "titulo": titulo, "fecha_hora_inicio": inicio,
                      "duracion_minutos": duracion, "correos_invitados": invitados,
                      "run_id": args.get("_run_id"), "id_hilo": id_hilo},
        })
        _registrar_mutacion(args.get("_run_id"),
                            {"tipo": "pendiente", "clave": pid, "id_hilo": id_hilo})
        return {"ok": True, "evento_id": evento_id, "estado": "borrador_pendiente_aprobacion",
                "pendiente_id": pid, "enlace_sugerido": "https://meet.google.com/abc-defg-hij"}
    EVENTOS[evento_id] = {"titulo": titulo, "inicio": inicio, "duracion": duracion,
                          "invitados": invitados, "estado": "confirmada", "id_hilo": id_hilo}
    _registrar_mutacion(args.get("_run_id"),
                        {"tipo": "evento", "clave": evento_id, "id_hilo": id_hilo})
    return {"ok": True, "evento_id": evento_id, "estado": "confirmada",
            "enlace": "https://meet.google.com/abc-defg-hij"}


def _verificar_citas(requisitos, fuente):
    fallidas = []
    fuente_norm = _normalizar(fuente)
    for req in requisitos or []:
        cita = req.get("cita_textual", "")
        if not cita or _normalizar(cita) not in fuente_norm:
            fallidas.append(cita or "(vacio)")
    return fallidas


def crear_ticket_en_jira(args):
    id_hilo = args.get("id_hilo_correo")
    for tid, ticket in JIRA.items():
        if ticket.get("id_hilo_correo") == id_hilo:
            return {"ok": True, "ticket": tid, "duplicado": True, "detalle": "Ya existe un ticket para este hilo."}
    fallidas = _verificar_citas(args.get("requisitos"), args.get("_fuente", ""))
    if fallidas:
        return {"ok": False, "error": "cita_no_verificada",
                "detalle": "Las siguientes citas textuales no existen en el correo/adjunto: " + "; ".join(fallidas[:3]) + ".",
                "instruccion": "Revisa la extraccion y vuelve a invocar la funcion con citas literales."}
    requisitos = []
    for req in args.get("requisitos", []):
        confianza = req.get("confianza", "baja")
        requisitos.append({"descripcion": req.get("descripcion"), "cita_textual": req.get("cita_textual"),
                           "confianza": confianza})
    ticket_id = _nuevo_id("UTPC", "jira")
    JIRA[ticket_id] = {
        "resumen": args.get("resumen"),
        "tipo": args.get("tipo_incidencia"),
        "prioridad": args.get("prioridad"),
        "empresa": args.get("empresa_cliente"),
        "id_hilo_correo": id_hilo,
        "requisitos": requisitos,
        "run_id": args.get("_run_id"),
    }
    _registrar_mutacion(args.get("_run_id"),
                        {"tipo": "ticket", "clave": ticket_id, "id_hilo": id_hilo})
    ids_pendientes = []
    for req in requisitos:
        if req["confianza"] != "alta":
            pid = _nuevo_id("P", "pendiente")
            PENDIENTES.append({
                "id": pid,
                "tipo": "validacion_requisito",
                "estado": "pendiente",
                "descripcion": f'Validar requisito (confianza {req["confianza"]}): {req["descripcion"]}',
                "datos": {"ticket": ticket_id, "run_id": args.get("_run_id"), "id_hilo": id_hilo, **req},
            })
            _registrar_mutacion(args.get("_run_id"),
                                {"tipo": "pendiente", "clave": pid, "id_hilo": id_hilo})
            ids_pendientes.append(pid)
    return {"ok": True, "ticket": ticket_id, "proyecto": "UTPC", "tipo": args.get("tipo_incidencia"),
            "requisitos_registrados": len(requisitos), "duplicado": False,
            "pendientes_validacion": ids_pendientes}


def actualizar_contacto_en_crm(args):
    correo = args.get("correo_electronico", "").strip().lower()
    campos = ("nombre_completo", "empresa", "cargo", "telefono", "estado_oportunidad", "resumen_interaccion")
    if correo in CRM:
        contacto_id = f"CRM-{list(CRM.keys()).index(correo) + 10000}"
        anterior = dict(CRM[correo])
        CRM[correo].update({k: v for k, v in args.items() if k in campos and v})
        CRM[correo]["estado"] = args.get("estado_oportunidad", CRM[correo].get("estado"))
        CRM[correo]["run_id"] = args.get("_run_id")
        _registrar_mutacion(args.get("_run_id"),
                            {"tipo": "contacto_actualizado", "correo": correo,
                             "estado_anterior": anterior, "id_hilo": args.get("id_hilo_correo")})
        return {"ok": True, "contacto_id": contacto_id, "accion": "actualizado", "correo": correo}
    contacto_id = _nuevo_id("CRM", "crm")
    CRM[correo] = {
        "nombre": args.get("nombre_completo"),
        "empresa": args.get("empresa"),
        "cargo": args.get("cargo"),
        "telefono": args.get("telefono"),
        "estado": args.get("estado_oportunidad"),
        "resumen": args.get("resumen_interaccion"),
        "origen": args.get("origen"),
        "run_id": args.get("_run_id"),
    }
    _registrar_mutacion(args.get("_run_id"),
                        {"tipo": "contacto_creado", "clave": contacto_id, "correo": correo,
                         "id_hilo": args.get("id_hilo_correo")})
    return {"ok": True, "contacto_id": contacto_id, "accion": "creado", "correo": correo}


def _registrar_escalamiento(motivo, detalle, prioridad, id_hilo, run_id):
    caso_id = _nuevo_id("ESC", "caso")
    pid = _nuevo_id("P", "pendiente")
    ESCALADOS.append({"caso": caso_id, "motivo": motivo, "prioridad": prioridad,
                      "detalle": detalle, "id_hilo": id_hilo, "run_id": run_id})
    PENDIENTES.append({
        "id": pid,
        "tipo": "escalamiento",
        "estado": "pendiente",
        "descripcion": f"[{prioridad.upper()}] {motivo}: {detalle}",
        "datos": {"caso_id": caso_id, "motivo": motivo, "prioridad": prioridad,
                  "detalle": detalle, "run_id": run_id, "id_hilo": id_hilo},
    })
    _registrar_mutacion(run_id, {"tipo": "escalamiento", "clave": caso_id, "id_hilo": id_hilo})
    _registrar_mutacion(run_id, {"tipo": "pendiente", "clave": pid, "id_hilo": id_hilo})
    return caso_id


def escalar_a_responsable_humano(args):
    caso_id = _registrar_escalamiento(
        args.get("motivo"), args.get("detalle", ""), args.get("prioridad", "media"),
        args.get("id_hilo_correo"), args.get("_run_id"))
    return {"ok": True, "caso_id": caso_id,
            "recibido_por": "responsable_comercial@utpconsult.com"}


REGISTRO = {
    "consultar_disponibilidad_calendario": consultar_disponibilidad_calendario,
    "agendar_reunion_en_google_calendar": agendar_reunion_en_google_calendar,
    "crear_ticket_en_jira": crear_ticket_en_jira,
    "actualizar_contacto_en_crm": actualizar_contacto_en_crm,
    "escalar_a_responsable_humano": escalar_a_responsable_humano,
}


def ejecutar_funcion(nombre, args, id_hilo_correo, fuentes_cita, run_id=None):
    func = REGISTRO.get(nombre)
    if func is None:
        return {"ok": False, "error": f"Funcion desconocida: {nombre}"}
    try:
        args = dict(args or {})
        args["_fuente"] = fuentes_cita
        args["_id_hilo"] = id_hilo_correo
        args["_run_id"] = run_id or f"run-{id_hilo_correo or 'desconocido'}"
        return func(args)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def aprobar_pendiente(pid):
    for p in PENDIENTES:
        if p["id"] == pid and p["estado"] == "pendiente":
            p["estado"] = "aprobado"
            datos = p["datos"]
            if p["tipo"] == "reunion" and "evento_id" in datos:
                EVT = datos["evento_id"]
                EVENTOS[EVT] = {"titulo": datos.get("titulo"), "inicio": datos.get("fecha_hora_inicio"),
                                "duracion": datos.get("duracion_minutos"), "invitados": datos.get("correos_invitados"),
                                "estado": "confirmada", "id_hilo": datos.get("id_hilo")}
            return True
    return False


def rechazar_pendiente(pid):
    for p in PENDIENTES:
        if p["id"] == pid and p["estado"] == "pendiente":
            p["estado"] = "rechazado"
            return True
    return False


def resumen_datos():
    return {
        "tickets_jira": len(JIRA),
        "contactos_crm": len(CRM),
        "eventos_agendados": len(EVENTOS),
        "escalamientos": len(ESCALADOS),
        "pendientes": len([p for p in PENDIENTES if p["estado"] == "pendiente"]),
        "runs": len(RUNS),
    }


def reiniciar_datos():
    global CRM, JIRA, AGENDA, EVENTOS, ESCALADOS, PENDIENTES, SECUENCIAL
    global REGISTRO_HILOS, MUTACIONES, RUNS
    SECUENCIAL.update({"jira": 482, "crm": 10327, "evento": 900001, "caso": 1, "pendiente": 1})
    CRM.clear()
    CRM.update({"lucia.vela@acme.com": {"nombre": "Lucia Vela", "empresa": "Acme SAC", "cargo": "Gerenta de TI", "estado": "en_negociacion"}})
    JIRA.clear()
    AGENDA.clear()
    AGENDA.update({"pm@utpconsult.com": {}, "ventas@utpconsult.com": {}})
    EVENTOS.clear()
    ESCALADOS.clear()
    PENDIENTES.clear()
    REGISTRO_HILOS.clear()
    MUTACIONES.clear()
    RUNS.clear()