# UTP Assistant — Asistente de IA para el flujo de correo comercial

Implementación funcional de la **Tarea Académica 2** "UTP Assistant: Propuesta de Diseño
Técnico de un Asistente de IA para la Automatización del Flujo de Correo Comercial en
UTPConsult". El proyecto traduce el diseño técnico del documento (prompt de sistema,
Function Calling con 5 herramientas y ciclo de vida de un Run) a una aplicación que
funciona **con la API gratuita de NVIDIA** y se despliega a una **URL pública** para que
el docente la vea sin instalar nada.

---

## 1. ¿Qué hace?

Pegas un correo entrante de un cliente (con opción de adjunto .txt/.md/.pdf) y el agente:

1. Clasifica el correo y extrae remitente, empresa, requisitos con **cita textual** y
   nivel de confianza.
2. Decide qué herramientas llamar siguiendo las reglas del prompt (R1–R8 del documento):
   - `consultar_disponibilidad_calendario` → franjas libres del equipo.
   - `agendar_reunion_en_google_calendar` → crea evento **en borrador** (requiere
     aprobación humana si la franja no fue confirmada).
   - `crear_ticket_en_jira` → incidencia en el proyecto UTPC, con validación de que cada
     *cita textual* exista realmente en el correo/adjunto (grounding, Riesgo 1).
   - `actualizar_contacto_en_crm` → crea/actualiza el prospecto.
   - `escalar_a_responsable_humano` → ante cifras, datos faltantes o posible inyección
     de prompt (R5, R6).
3. Ejecuta el ciclo del Run (`queued → in_progress → requires_action → submit_tool_outputs
   → completed`) mostrando cada ronda en vivo en el panel.
4. Genera el reporte final para el equipo interno (resumen, cliente, requisitos, acciones,
   pendientes y borrador de respuesta al cliente).

Además implementa los controles que el documento pide a nivel de diseño:

- **Capa de ingesta (Paso 1):** puedes cargar el correo en crudo como `.eml`; se parsea el
  MIME, se separan cuerpo/adjuntos y se resuelve el hilo con `Message-ID`/`References`.
- **Mascarado (Riesgo 3):** DNI, RUC, cuentas bancarias y credenciales se sustituyen por
  marcadores antes de llegar al modelo.
- **Lista blanca (Riesgo 2):** `agendar_reunion_en_google_calendar` rechaza
  programáticamente invitados ajenos al hilo o al dominio `@utpconsult.com` y escala el caso.
- **Trazabilidad (R6/R8):** cada ejecución lleva un `run_id`; el panel permite auditar las
  mutaciones de un run y **revertirlas**.
- **Estados del Run (Tabla 3):** se manejan también `failed` (con `last_error`),
  `expired` (reintento con idempotencia) e `incomplete` (reproceso con contexto resumido).
- **Spam / notificaciones automáticas:** la ingesta clasifica por heurística y termina el run
  sin usar herramientas ni gastar tokens.

Las integraciones (Jira, Calendar, CRM) están **simuladas en memoria** para la
demostración; la pantalla "Auditoría y sistemas" deja ver el estado de cada sistema.

## 2. Ejecutar en local

> Requiere Python 3.12+.

```powershell
cd "UTP Assistant"
pip install -r requirements.txt
streamlit run app.py
```

La clave de API se lee en este orden:
1. `.streamlit/secrets.toml` (ya viene con la clave de demo; no subir a GitHub).
2. Variable de entorno `NVIDIA_API_KEY`.
3. Campo "API key NVIDIA" de la barra lateral.

## 3. Desplegar gratis y compartir con el docente (sin que instale nada)

La app está hecha en **Streamlit** y se publica en Streamlit Community Cloud desde GitHub:

1. Crea un repositorio en GitHub y sube la carpeta `UTP Assistant` (no subas
   `.streamlit/secrets.toml`; ya está en `.gitignore`).
2. Entra a <https://share.streamlit.io> e inicia sesión con tu GitHub.
3. **Create app → Yup, I have an app** → elige el repositorio, rama `main` y archivo
   `app.py` → **Deploy**.
4. En **Settings → Secrets**, pega:
   ```toml
   NVIDIA_API_KEY = "nvapi-…"
   ```
5. Comparte el enlace `https://tunombre-app.streamlit.app` con el docente.

> Nota: en el plan gratuito la app "duerme" tras horas sin uso; al abrirla de nuevo
> aparece una pantalla de *wake-up* por unos segundos. Es normal.

## 4. ¿Por qué no Vercel?

Vercel (plan gratis) limita las funciones Python a **5 minutos** como máximo, y cada
petición corre en una función efímera (el estado simulado se pierde entre visitas). Como
cada correo tarda 2–6 minutos en procesarse (llamadas al modelo), Streamlit Community
Cloud es más adecuado: es un proceso persistente, admite procesos largos, soporta secrets
para la clave y coincide con la **Capa 5 "Panel Streamlit"** del documento.

## 5. Selección del modelo en la API de NVIDIA

La clave gratuita solo habilita unos pocos modelos de `build.nvidia.com`. Se probaron
nueve candidatos con el prompt real del documento y las 5 herramientas:

| Modelo | ¿Tool calling? | Resultado de la prueba |
|---|---|---|
| **`moonshotai/kimi-k3`** | ✅ | **Elegido.** Siguió las reglas R1–R6, usó las 5 herramientas, fechas correctas y el formato de salida. ~1–2 min por ronda. |
| `deepseek-ai/deepseek-v4-flash-0731` | ✅ | Correcto pero daña acentos (ó→Ã³) y es muy lento. |
| `meta/llama-3.2-11b-vision-instruct` | ⚠️ | Muy rápido, pero en la 3.ª ronda devolvió el tool call como texto suelto (rompe el ciclo). |
| `z-ai/glm-5.3`, `openai/gpt-oss-20b` | ❌ | Modelos de razonamiento: agotan los tokens pensando y no llegan a llamar herramientas. |
| Otros (llama-3.3-70b, nemotron, mixtral…) | — | La clave no tiene acceso (HTTP 404) o vencidos. |

## 6. Adaptación al documento ("x API de Asistentes de OpenAI")

La API de NVIDIA **no tiene** Threads, Runs ni File Search. Por eso:

- El **Thread** (historial del hilo de correo) se conserva en memoria en la aplicación,
  indexado por `id_hilo_correo` (pestaña "Hilos").
- El **Run** se replica manualmente como máquina de estados sobre *Chat Completions*:
  la app orquesta `requires_action` → ejecuta las funciones → `submit_tool_outputs`, tal
  como el propio documento describe la variante "Chat Completions" (Tabla 1).
- El **File Search** se reemplaza por el texto del adjunto insertado entre las etiquetas
  `<adjunto>` (igual que el `<correo_entrante>`), preservando la regla R6.
- Los controles del documento (cita textual obligatoria, idempotencia, aprobación humana)
  se implementaron en `tools_sim.py`.

## 7. Estructura del proyecto

```
UTP Assistant/
├── app.py               # Panel Streamlit (capa 5): entrada, EML, trazabilidad, aprobaciones
├── agent.py             # Motor del agente: prompt, herramientas, ciclo del Run
├── ingesta.py           # Capa 1: parsing EML, resolución de hilo, mascarado, spam, fragmentos
├── tools_sim.py         # Simulación de Jira, Calendar y CRM con controles del diseño
├── requirements.txt
├── .streamlit/
│   ├── secrets.toml        # Clave local (no subir)
│   └── secrets.toml.example
└── README.md
```