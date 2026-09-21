# -*- coding: utf-8 -*-
"""Capa 1 del documento: ingesta del correo, resolucion del hilo y normalizacion.

Responsabilidades (Paso 1 del informe):
- parsear_eml: convierte un correo crudo EML/MIME en un diccionario normalizado
  (remitente, destinatarios, asunto, cuerpo plano, adjuntos, message_id,
  references, in_reply_to).
- resolver_hilo: calcula el identificador canonico del hilo a partir de
  Message-ID / References / In-Reply-To (RFC 5322), segun el apartado 2.3.1.
- enmascarar_texto: enmascara identificadores que no contribuyen a la tarea
  (DNI, RUC, cuentas bancarias, credenciales) antes de llegar al modelo;
  control de la mitigacion del Riesgo 3 del informe.
- clasificar_correo: heuristica de ingesta para spam y notificaciones
  automaticas (complemento programatico de la regla del prompt).
- extraer_texto_adjunto: extrae texto de .txt/.md/.csv/.log/.pdf.
- recuperar_fragmentos: sustituto del File Search: selecciona los fragmentos
  del adjunto mas relevantes al cuerpo del correo para acotar el contexto.
"""

import io
import re
import unicodedata
from email import message_from_bytes
from email.header import decode_header, make_header
from email.policy import default
from html.parser import HTMLParser

DOMINIO_CORPORATIVO = "utpconsult.com"

MAX_FRAGMENTO = 1400
UMBRAL_FRAGMENTO = 3200
TOPE_FRAGMENTOS = 3


def _normalizar(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", texto).strip().lower()


def normalizar_email(texto):
    coincidencias = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", texto or "")
    return coincidencias[0].lower() if coincidencias else ""


def _id_seguro(texto, maxlen=90):
    limpio = re.sub(r"[^A-Za-z0-9_.@-]", "-", texto or "").strip("-")
    return limpio[:maxlen]


def enmascarar_texto(texto):
    """Enmascara DNI, RUC, cuentas y credenciales antes de enviar al modelo."""
    texto = texto or ""
    texto = re.sub(r"(?<!\d)\d{8}(?!\d)", "<DNI>", texto)
    texto = re.sub(r"(?<!\d)\d{11}(?!\d)", "<RUC>", texto)
    texto = re.sub(r"(?<!\d)\d{13,20}(?!\d)", "<CUENTA>", texto)
    texto = re.sub(r"\b(nvapi|sk)-[A-Za-z0-9_-]{6,}\b", r"\1-<REDACTADO>", texto)
    texto = re.sub(
        r"(?i)\b(password|contrase[ñn]a|passwd|api[_-]?key|token)\b\s*[:=]?\s*[^\s,;]{6,}",
        r"\1=<REDACTADO>",
        texto,
    )
    return texto


class _HtmlATexto(HTMLParser):
    def __init__(self):
        super().__init__()
        self._fragmentos = []
        self._omitir = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._omitir += 1
        if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
            self._fragmentos.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._omitir:
            self._omitir -= 1

    def handle_data(self, data):
        if not self._omitir:
            self._fragmentos.append(data)

    def texto(self):
        crudo = "".join(self._fragmentos)
        return re.sub(r"[ \t]+", " ", crudo).strip()


def _html_a_texto(html):
    p = _HtmlATexto()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return p.texto()


def parsear_eml(contenido):
    """Parse un EML (str o bytes) y devuelve un dict normalizado."""
    if isinstance(contenido, bytes):
        raw = contenido
    else:
        raw = contenido.encode("utf-8", errors="replace")
    corpus = message_from_bytes(raw, policy=default)

    texto_plano, texto_html, adjuntos = "", "", []

    def _parte(parte):
        nonlocal texto_plano, texto_html
        nombre = parte.get_filename()
        datos = parte.get_payload(decode=True) or b""
        if parte.get_content_disposition() == "attachment" or nombre:
            adjuntos.append({"nombre": nombre or "adjunto.bin",
                             "mime": parte.get_content_type(),
                             "texto": extraer_texto_adjunto(nombre or "adjunto.bin", datos)})
            return
        ctype = parte.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            return
        charset = parte.get_content_charset() or "utf-8"
        try:
            texto = datos.decode(charset, errors="replace")
        except (LookupError, TypeError):
            texto = datos.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            texto_plano = texto
        elif ctype == "text/html":
            texto_html = _html_a_texto(texto)

    if corpus.is_multipart():
        for parte in corpus.walk():
            _parte(parte)
    else:
        _parte(corpus)

    def _dec(encabezado):
        valor = corpus.get(encabezado, "")
        try:
            return str(make_header(decode_header(valor)))
        except Exception:
            return valor

    return {
        "remitente": _dec("From"),
        "destinatarios": [_dec("To"), _dec("Cc")],
        "asunto": _dec("Subject"),
        "fecha": _dec("Date"),
        "message_id": (corpus.get("Message-ID") or "").strip(" <>"),
        "references": (corpus.get("References") or "").strip(),
        "in_reply_to": (corpus.get("In-Reply-To") or "").strip(" <>"),
        "cuerpo": (texto_plano or texto_html or "").strip(),
        "adjuntos": adjuntos,
    }


def resolver_hilo(message_id, references="", in_reply_to=""):
    """Devuelve el id canonico del hilo (raiz del arbol de la conversacion).

    La raiz es el primer Message-ID de la cabecera References; si no existe,
    se usa In-Reply-To y, en ultimo caso, el propio Message-ID del correo.
    De esta forma, dos correos de la misma conversacion comparten id.
    """
    raices = re.findall(r"<([^<>]+)>", references or "")
    ancestro = ""
    if raices:
        ancestro = raices[0]
    elif in_reply_to:
        ancestro = in_reply_to
    base = ancestro or message_id or ""
    return _id_seguro(base) if base else ""


def extraer_texto_adjunto(nombre, datos):
    nombre = (nombre or "").lower()
    if nombre.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            lector = PdfReader(io.BytesIO(datos))
            return "\n".join((pag.extract_text() or "") for pag in lector.pages)
        except Exception as e:
            return f"[No se pudo extraer el texto del PDF: {e}]"
    if nombre.endswith((".txt", ".md", ".csv", ".log")):
        return datos.decode("utf-8", errors="replace")
    return "[Formato de adjunto no soportado]"


def clasificar_correo(remitente, cuerpo, asunto=""):
    """Heuristica de ingesta: spam, notificaciones y boletines automaticos."""
    texto = " ".join([asunto or "", cuerpo or ""]).lower()
    rem = (remitente or "").lower()
    categorias = []
    if not (cuerpo or "").strip():
        categorias.append("correo_vacio")
    if re.search(r"(no-?reply|noresponder|notificaciones?@|alerts?@|soporte-?auto)", rem):
        categorias.append("notificacion_automatica")
    if re.search(r"(ganaste|premio|sorteo|ganador|oferta exclusiva|farmacia en linea"
                 r"|heredero|cripto|invierte y gana|millones de)", texto):
        categorias.append("spam_probable")
    if re.search(r"(unsubscribe|darse de baja|dejar de recibir)", texto):
        categorias.append("boletin_promocional")
    accionable = bool(re.search(r"(reunion|requisitos|propuesta|avance|contrato"
                                r"|cotizacion|presupuesto|modulo|desarrollo)", texto))
    es_spam = ("spam_probable" in categorias) and not accionable
    return {"es_spam": es_spam, "categorias": categorias, "accionable": accionable}


def recuperar_fragmentos(adjunto, consulta, max_fragmentos=TOPE_FRAGMENTOS):
    """Selecciona los fragmentos del adjunto mas relevantes a la consulta.

    Sustituto determinista del File Search: si el adjunto es largo, evita
    truncar a ciegas y devuelve los fragmentos con mayor solapamiento de
    terminos contra el cuerpo del correo (consulta).
    """
    if not adjunto or not consulta:
        return adjunto or ""
    max_fragmentos = max(1, int(max_fragmentos))
    if len(adjunto) <= UMBRAL_FRAGMENTO:
        return adjunto
    tokens_consulta = set(_normalizar(consulta).split())

    parrafos = [p.strip() for p in re.split(r"\n\s*\n", adjunto) if p.strip()]
    fragmentos, actual = [], ""
    for par in parrafos:
        if len(actual) + len(par) > MAX_FRAGMENTO and actual:
            fragmentos.append(actual)
            actual = ""
        actual = f"{actual}\n\n{par}".strip()
    if actual:
        fragmentos.append(actual)

    def _puntaje(frag):
        tokens = set(_normalizar(frag).split())
        if not tokens:
            return 0.0
        return len(tokens & tokens_consulta) / len(tokens_consulta)

    ordenados = sorted(fragmentos, key=_puntaje, reverse=True)
    elegidos = [f for f in ordenados if _puntaje(f) > 0][:max_fragmentos]
    if not elegidos:
        elegidos = fragmentos[:max_fragmentos]
    return "\n\n".join(elegidos)