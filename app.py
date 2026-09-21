# -*- coding: utf-8 -*-
"""Panel Streamlit de UTP Assistant (capa 5 del documento).

Ejecuta el agente sobre correos entrantes (pegados o en crudo EML), muestra
la trazabilidad del Run en vivo y permite la aprobacion humana de las
acciones de escritura y la reversal de runs con run_id.
"""

import io
import json
import os
import queue
import threading
import time
import urllib.parse
import uuid
from datetime import datetime

import streamlit as st

import agent as agente_mod
import ingesta
import tools_sim

_PAGE_ICON = "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='#6366f1' "
    "<rect x='3.5' y='8.5' width='17' height='11' rx='2.5'/> "
    "<circle cx='8.5' cy='14' r='1.7' fill='#fff'/> "
    "<circle cx='15.5' cy='14' r='1.7' fill='#fff'/> "
    "<path d='M12 7V4.5' stroke='#6366f1' stroke-width='2'/> "
    "<circle cx='12' cy='3' r='1.9' fill='#6366f1'/></svg>",
    safe="/:'%,")

st.set_page_config(page_title="UTP Assistant", page_icon=_PAGE_ICON, layout="wide")
st.markdown('<style>div.block-container{padding-top:1.2rem}</style>', unsafe_allow_html=True)


MODELOS = {
    "moonshotai/kimi-k3": "kimi-k3 — Recomendado (calidad alta)",
    "deepseek-ai/deepseek-v4-flash-0731": "deepseek-v4-flash — Alternativa (puede dañar acentos)",
    "meta/llama-3.2-11b-vision-instruct": "llama-3.2-11b-vision — Rápida (menos fiable)",
}


def leer_clave_api():
    try:
        v = st.secrets.get("NVIDIA_API_KEY", None)
        if v:
            return v
    except Exception:
        pass
    return os.environ.get("NVIDIA_API_KEY", "") or ""


def formato_evento(ev):
    t = ev.get("tipo")
    if t == "estado":
        return f"• {ev['texto']}"
    if t == "clasificacion":
        return f"• Clasificación de ingesta: {', '.join(ev.get('categorias', []))}"
    if t == "ronda":
        return f"── Ronda {ev['ronda']} — {ev['texto']}"
    if t == "llamada":
        return f"→ {ev['nombre']}({json.dumps(ev['args'], ensure_ascii=False)[:200]})"
    if t == "ejecucion":
        marcador = "OK " if ev["ok"] else "ERR"
        return f"   [{marcador}] {ev['nombre']} -> {json.dumps(ev['resultado'], ensure_ascii=False)[:300]}"
    if t == "final":
        return f"Respuesta: {ev.get('contenido','')[:1500]}"
    return ""


def render_evento(ev, caja):
    t = ev.get("tipo")
    if t == "estado":
        caja.write(ev["texto"])
        return
    if t == "clasificacion":
        cats = ", ".join(ev.get("categorias", []))
        caja.markdown(f":material/block: **Clasificación de ingesta:** {cats}")
        return
    if t == "ronda":
        caja.markdown(f"**── Ronda {ev['ronda']}** — {ev['texto']}")
        return
    if t == "llamada":
        args = json.dumps(ev["args"], ensure_ascii=False)
        caja.markdown(f":material/construction: **{ev['nombre']}**  \n`{args[:600]}`")
        return
    if t == "ejecucion":
        res = json.dumps(ev["resultado"], ensure_ascii=False)
        marcador = ":material/done:" if ev["ok"] else ":material/warning_amber:"
        caja.markdown(f"{marcador} `{ev['nombre']}` → `{res[:800]}`")
        return
    if t == "final":
        caja.markdown(":material/description: **Respuesta final del asistente**")
        caja.markdown(ev["contenido"][:3000])


def iniciar_proceso(clave, modelo, remitente, id_hilo, cuerpo, adjunto):
    st.session_state.procesando = True
    st.session_state.cola = queue.Queue()
    ctx = {"hecho": False, "resultado": None, "error": None, "ejecuciones": []}
    st.session_state.ctx = ctx
    historial = list(st.session_state.hilos.get(id_hilo, {}).get("mensajes_api", []))

    def trabaja():
        try:
            cl = agente_mod.Agente(clave, modelo)

            def on_ev(ev):
                st.session_state.cola.put(ev)
                if ev.get("tipo") == "ejecucion":
                    ctx["ejecuciones"].append(ev)

            res = cl.procesar_correo(
                list(historial), remitente, id_hilo, cuerpo, adjunto,
                on_evento=on_ev,
            )
            ctx["resultado"] = res
        except Exception as e:
            ctx["error"] = str(e)
        finally:
            ctx["hecho"] = True

    threading.Thread(target=trabaja, daemon=True).start()


def mostrar_proceso():
    cola = st.session_state.cola
    ctx = st.session_state.ctx
    traza = []
    with st.status("Ejecutando UTP Assistant…", expanded=True) as caja:
        caja.caption("Llamando al modelo en NVIDIA. Cada ronda puede tardar 1-3 minutos.")
        while not (ctx["hecho"] and cola.empty()):
            try:
                ev = cola.get(timeout=0.4)
            except queue.Empty:
                continue
            render_evento(ev, caja)
            linea = formato_evento(ev)
            if linea:
                traza.append(linea)
        if ctx["error"]:
            caja.update(label="Run finalizado con error", state="error")
        else:
            caja.update(label="Run completado", state="complete")
        caja.caption(f"Fin: {datetime.now().strftime('%H:%M:%S')}")
    st.session_state.procesando = False
    id_hilo = st.session_state.proceso_hilo
    if ctx["error"]:
        st.error(f"Ocurrió un error: {ctx['error']}")
        st.session_state.hilos.setdefault(id_hilo, {})["traza"] = traza
        return
    res = ctx["resultado"]
    hilo = st.session_state.hilos.setdefault(id_hilo, {"mensajes_api": [], "correos": [], "traza": []})
    hilo["mensajes_api"] = res["historial"]
    hilo["correos"] = hilo.get("correos", [])
    hilo["traza"] = traza
    hilo["ultimo_resultado"] = {"respuesta_final": res["respuesta_final"], "llamadas": res["llamadas"],
                                "estado": res.get("estado"), "run_id": res.get("run_id")}
    for ej in ctx["ejecuciones"]:
        st.session_state.auditoria.append({
            "hora": datetime.now().strftime("%H:%M:%S"),
            "hilo": id_hilo,
            "run_id": res.get("run_id"),
            "funcion": ej["nombre"],
            "args": ej["args"],
            "resultado": ej["resultado"],
        })
    st.session_state.hilo_actual = id_hilo
    estado_icon = ":material/check_circle:" if res.get("estado") == "completed" else ":material/warning_amber:"
    st.success(f"{estado_icon} Correo procesado. Run `{res.get('run_id')}` finalizó en estado `{res.get('estado')}`.")


def inicializar_estado():
    if "hilos" not in st.session_state:
        st.session_state.hilos = {}
    if "auditoria" not in st.session_state:
        st.session_state.auditoria = []
    if "procesando" not in st.session_state:
        st.session_state.procesando = False
    if "hilo_actual" not in st.session_state:
        st.session_state.hilo_actual = None
    if "prefill" not in st.session_state:
        st.session_state.prefill = {}


def pestaña_nuevo_correo(clave, modelo):
    st.markdown(":material/forward_to_inbox: **Correo entrante de cliente**")
    with st.expander("Cargar un correo en crudo (EML / RFC 5322)", expanded=False):
        eml = st.file_uploader("Archivo .eml", type=["eml"], key="eml_carga")
        st.caption("El agente resolverá el hilo con Message-ID / References e insertará "
                   "cuerpo y adjuntos en las etiquetas del prompt.")
        if st.button("Aplicar correo EML", type="secondary"):
            if eml is None:
                st.warning("Selecciona primero un archivo .eml.")
            else:
                try:
                    parsed = ingesta.parsear_eml(eml.getvalue())
                    thread_id = ingesta.resolver_hilo(parsed["message_id"],
                                                      parsed["references"],
                                                      parsed["in_reply_to"])
                    if not thread_id:
                        thread_id = "auto-" + uuid.uuid4().hex[:8]
                    adjunto_texto = "\n".join(a["texto"] for a in parsed["adjuntos"][:3])
                    st.session_state.prefill = {
                        "remitente": parsed["remitente"],
                        "id_hilo": thread_id,
                        "cuerpo": parsed["cuerpo"],
                        "adjunto": adjunto_texto,
                    }
                    st.success(f"EML aplicado. Hilo resuelto: `{thread_id}` "
                               f"({len(parsed['adjuntos'])} adjunto(s)). Revisa los campos y procesa.")
                except Exception as e:
                    st.error(f"No se pudo interpretar el EML: {e}")

    pre = st.session_state.get("prefill", {})
    if pre:
        with st.container(border=True):
            st.caption(f":material/schedule: {pre.get('fecha') or ''}  ·  "
                       f":material/mark_email_unread: Hilo `{pre.get('id_hilo')}`")
    with st.form("form_correo"):
        c1, c2 = st.columns(2)
        remitente = c1.text_input("Remitente", value=pre.get("remitente", ""),
                                  placeholder="cliente@empresa.com")
        id_hilo = c2.text_input("ID del hilo de correo (dejar vacío = nuevo hilo)",
                                value=pre.get("id_hilo", ""), placeholder="auto")
        cuerpo = st.text_area("Cuerpo del correo", height=180,
                              value=pre.get("cuerpo", ""),
                              placeholder="Pegue aquí el correo recibido del cliente…")
        c3, c4 = st.columns(2)
        archivo = c3.file_uploader("Adjunto (txt, md o pdf)", type=["txt", "md", "pdf"])
        adjunto_pegado = c4.text_area("O pegar texto del adjunto (opcional)",
                                      value=pre.get("adjunto", ""), height=120)
        enviar = st.form_submit_button("Procesar con UTP Assistant",
                                       type="primary", disabled=st.session_state.procesando)
    if archivo is not None:
        st.caption(f":material/attach_file: Adjunto \"{archivo.name}\" será incluido en el contexto del agente.")
    if not clave:
        st.error(":material/key: Falta la clave de API de NVIDIA. Configúrala "
                 "en `.streamlit/secrets.toml` o en los Secrets de Streamlit Cloud.")
        return
    if enviar:
        if not cuerpo.strip():
            st.warning("Pega el cuerpo del correo antes de procesar.")
            return
        cuerpo_texto = cuerpo.strip()
        adjunto_texto = ""
        if archivo is not None:
            adjunto_texto = ingesta.extraer_texto_adjunto(archivo.name, archivo.getvalue())
        if adjunto_pegado.strip():
            adjunto_texto += ("\n" if adjunto_texto else "") + adjunto_pegado.strip()
        if not id_hilo.strip():
            id_hilo = "auto-" + uuid.uuid4().hex[:8]
        id_hilo = id_hilo.strip()
        hilo = st.session_state.hilos.setdefault(id_hilo, {"mensajes_api": [], "correos": [], "traza": []})
        hilo["correos"].append({
            "hora": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "remitente": remitente or "(no indicado)",
            "cuerpo": cuerpo_texto,
            "adjunto": adjunto_texto[:2000],
        })
        st.session_state.proceso_hilo = id_hilo
        st.session_state.hilo_actual = id_hilo
        iniciar_proceso(clave, modelo, remitente or "(no indicado)", id_hilo, cuerpo_texto, adjunto_texto)
        mostrar_proceso()


def pestaña_resultados():
    st.markdown(":material/fact_check: **Resultado del último run**")
    hilo_id = st.session_state.hilo_actual
    if not hilo_id or hilo_id not in st.session_state.hilos or "ultimo_resultado" not in st.session_state.hilos[hilo_id]:
        st.info("Aún no hay resultados. Procesa un correo en la pestaña «Nuevo correo».")
        return
    res = st.session_state.hilos[hilo_id]["ultimo_resultado"]
    st.caption(f":material/mark_email_unread: Hilo `{hilo_id}` · "
               f":material/manage_search: run `{res.get('run_id')}` · "
               f"estado `{res.get('estado')}`")
    st.markdown("---")
    c_resp, c_acc = st.columns([3, 2])
    with c_resp:
        st.markdown("**Respuesta del asistente (para el equipo interno)**")
        st.markdown(res["respuesta_final"] or "_Sin respuesta._")
    with c_acc:
        st.markdown("**Funciones invocadas**")
        if not res["llamadas"]:
            st.caption("Ninguna.")
        for i, llamada in enumerate(res["llamadas"], 1):
            args = json.dumps(llamada["args"], ensure_ascii=False)
            with st.expander(f"{i}. {llamada['nombre']}", expanded=False):
                st.code(args[:1500], language="json")
    st.markdown("---")
    st.markdown(":material/verified_user: **Bandeja de aprobación humana**")
    pendientes = [p for p in tools_sim.PENDIENTES if p["estado"] == "pendiente"]
    if not pendientes:
        st.success("No hay acciones pendientes de aprobación.")
    for p in pendientes:
        with st.container(border=True):
            c1, c2 = st.columns([4, 1.2])
            c1.markdown(f"**{p['descripcion']}**  \n`{p['tipo']}` · `{p['id']}`")
            b1, b2 = c2.columns(2)
            if b1.button("Aprobar", key=f"ap_{p['id']}"):
                if tools_sim.aprobar_pendiente(p["id"]):
                    st.session_state.auditoria.append({
                        "hora": datetime.now().strftime("%H:%M:%S"), "hilo": hilo_id,
                        "run_id": p["datos"].get("run_id"),
                        "funcion": "APROBACION", "args": {"pendiente": p["id"]},
                        "resultado": {"aprobado": True}})
                    st.success(f"{p['id']} aprobado y ejecutado.")
                    st.rerun()
            if b2.button("Rechazar", key=f"rj_{p['id']}"):
                if tools_sim.rechazar_pendiente(p["id"]):
                    st.session_state.auditoria.append({
                        "hora": datetime.now().strftime("%H:%M:%S"), "hilo": hilo_id,
                        "run_id": p["datos"].get("run_id"),
                        "funcion": "RECHAZO", "args": {"pendiente": p["id"]},
                        "resultado": {"rechazado": True}})
                    st.success(f"{p['id']} rechazado.")
                    st.rerun()


def pestaña_auditoria():
    st.markdown(":material/manage_search: **Auditoría y estado de los sistemas simulados**")
    r = tools_sim.resumen_datos()
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Tickets en Jira", r["tickets_jira"])
    c2.metric("Contactos en CRM", r["contactos_crm"])
    c3.metric("Eventos agendados", r["eventos_agendados"])
    c4.metric("Escalamientos", r["escalamientos"])
    c5.metric("Pendientes", r["pendientes"])
    c6.metric("Runs", r["runs"])
    st.markdown("---")
    with st.expander("Runs y reversión (run_id)", expanded=False):
        runs = tools_sim.listar_runs()
        if not runs:
            st.caption("Sin runs registrados.")
        for run in runs:
            with st.container(border=True):
                ca, cb = st.columns([4, 1])
                ca.markdown(f"**{run['run_id']}** · hilo `{run['id_hilo']}` · {run['fecha']} · "
                            f"{run['mutaciones']} mutación(es)")
                if cb.button("Revertir run", key=f"rev_{run['run_id']}"):
                    if tools_sim.revertir_run(run["run_id"]):
                        st.success(f"{run['run_id']} revertido (opuesto del registro de auditoría).")
                        st.rerun()
                    else:
                        st.info("No hay mutaciones revertibles para ese run.")
                detalle = tools_sim.auditoria_trazable(run["run_id"])
                if detalle:
                    st.caption("Mutaciones: " + ", ".join(
                        f"`{d['tipo']}` → `{d.get('clave') or d.get('correo')}`" for d in detalle))
    with st.expander("Auditoría del Run (ejecuciones)", expanded=False):
        if not st.session_state.auditoria:
            st.caption("Sin registros.")
        for a in reversed(st.session_state.auditoria):
            st.markdown(f"**{a['hora']}** · `{a['funcion']}` · hilo `{a['hilo']}` · run `{a.get('run_id')}`")
            st.code(json.dumps({"args": a["args"], "resultado": a["resultado"]}, ensure_ascii=False, indent=1)[:900])
    with st.expander("Jira (simulado)", expanded=False):
        if not tools_sim.JIRA:
            st.caption("Sin tickets.")
        for tid, t in tools_sim.JIRA.items():
            st.markdown(f"**{tid}** · {t['tipo']} · {t['prioridad']} · {t['empresa']}  \n_run: `{t.get('run_id')}`_")
            st.markdown(f"_{t['resumen'] or ''}_  \n" + "  \n".join(
                f"- {x['descripcion']} _(confianza {x['confianza']})_" for x in t.get("requisitos", [])))
    with st.expander("CRM (simulado)", expanded=False):
        if not tools_sim.CRM:
            st.caption("Sin contactos.")
        for correo, c in tools_sim.CRM.items():
            st.markdown(f"**{c.get('nombre')}** · {c.get('empresa')} · {c.get('cargo')} — {c.get('estado')}  \n`{correo}`")
    with st.expander("Calendario / eventos (simulado)", expanded=False):
        if not tools_sim.EVENTOS:
            st.caption("Sin eventos confirmados (los borradores viven en la bandeja de aprobación).")
        for eid, ev in tools_sim.EVENTOS.items():
            st.markdown(f"**{eid}** · {ev.get('titulo')} · {ev.get('inicio')} · {ev.get('estado')}")


def pestaña_hilos():
    st.markdown(":material/forum: **Hilos de conversación**")
    if not st.session_state.hilos:
        st.info("Aún no hay hilos procesados.")
        return
    ids = list(st.session_state.hilos.keys())
    sel = st.selectbox("Hilo", ids, index=ids.index(st.session_state.hilo_actual) if st.session_state.hilo_actual in ids else 0)
    hilo = st.session_state.hilos[sel]
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("**Correos recibidos**")
        for cor in hilo.get("correos", []):
            st.markdown(f"`{cor['hora']}` de **{cor['remitente']}**")
            st.markdown(f"> {cor['cuerpo'][:600]}")
            if cor.get("adjunto"):
                st.markdown(f"> :material/attach_file: _Adjunto:_ {cor['adjunto'][:300]}")
    with c2:
        st.markdown("**Trazabilidad del Run**")
        traza = hilo.get("traza", [])
        if not traza:
            st.caption("Sin traza.")
        for linea in traza:
            st.markdown(linea[:900])
    if st.button("Borrar historial de este hilo"):
        st.session_state.hilos.pop(sel, None)
        if st.session_state.hilo_actual == sel:
            st.session_state.hilo_actual = None
        st.rerun()


def main():
    inicializar_estado()
    clave = leer_clave_api()
    with st.sidebar:
        st.markdown(":material/robot: **UTP Assistant**")
        st.caption("Asistente de gestión de correo comercial de UTPConsult")
        modelo_sel = st.selectbox("Modelo NVIDIA", list(MODELOS.keys()), index=0,
                                  format_func=lambda m: MODELOS[m])
        clave_override = st.text_input("API key NVIDIA (opcional)", type="password",
                                       placeholder="nvapi-…" if not clave else ":material/key: ya configurada")
        if clave_override:
            clave = clave_override
        st.divider()
        st.markdown("**Estado de la API**")
        if clave:
            st.markdown(":material/cloud_done: conectada")
        else:
            st.markdown(":material/cloud_off: falta la key")
        if st.button("Reiniciar datos simulados"):
            tools_sim.reiniciar_datos()
            st.rerun()
        with st.expander("Prompt de sistema (ver)"):
            st.code(agente_mod.construir_prompt_sistema(), language="markdown")
        st.divider()
        st.caption("Modelo recomendado: kimi-k3. La API de NVIDIA no ofrece File Search; "
                   "el texto del adjunto se inserta entre las etiquetas `<adjunto>` y se "
                   "aplican mascarado, lista blanca y recuperación de fragmentos.")

    st.title("UTP Assistant — automatización del flujo de correo comercial")

    if st.session_state.procesando:
        mostrar_proceso()

    tabs = st.tabs(["Nuevo correo", "Resultados del run", "Auditoría y sistemas", "Hilos"])
    with tabs[0]:
        pestaña_nuevo_correo(clave, modelo_sel)
    with tabs[1]:
        pestaña_resultados()
    with tabs[2]:
        pestaña_auditoria()
    with tabs[3]:
        pestaña_hilos()


if __name__ == "__main__":
    main()