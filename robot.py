#!/usr/bin/env python3
"""Robot de anuncios: revisa Wallapop (y más webs) y avisa por Telegram.

Uso:
    python robot.py            # ejecución normal (avisa y guarda estado)
    python robot.py --probar   # prueba: muestra lo que devuelve la web, sin avisar

La configuración se lee de la variable de entorno CONFIG_YAML (secreto de
GitHub) o, si no existe, del archivo config.yaml.
Variables necesarias para avisar: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.
"""

import html
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ESTADO = Path("state/vistos.json")
DIAS_GUARDAR = 60          # cuánto tiempo se recuerdan los anuncios vistos
MAX_AVISOS_POR_EJECUCION = 15

# Llodio/Laudio (01400). Se puede cambiar con zona.latitud / zona.longitud
LAT_DEFECTO, LON_DEFECTO = 43.1409, -2.9633

# Prefijos de código postal por provincia (para excluir zonas)
CP_PROVINCIA = {
    "gipuzkoa": "20", "guipuzcoa": "20", "guipúzcoa": "20",
    "álava": "01", "alava": "01", "araba": "01",
    "bizkaia": "48", "vizcaya": "48",
    "cantabria": "39", "burgos": "09",
}

# Categorías de Wallapop usadas por defecto: 100 = Coches, 14000 = Motos.
# Se pueden cambiar en config.yaml (webs.wallapop.categorias_ids).
CATEGORIAS_WALLAPOP = [100, 14000]


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def log(*args):
    print(*args, flush=True)


def cargar_config():
    crudo = os.environ.get("CONFIG_YAML", "").strip()
    if crudo:
        return yaml.safe_load(crudo)
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            est = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        est = {}
    est.setdefault("vistos", {})     # web -> {id: marca_de_tiempo}
    est.setdefault("listos", [])     # "web|tag" ya inicializados
    est.setdefault("alertas", {})    # clave -> marca_de_tiempo
    return est


def guardar_estado(est):
    limite = time.time() - DIAS_GUARDAR * 86400
    for web, ids in est["vistos"].items():
        est["vistos"][web] = {i: t for i, t in ids.items() if t >= limite}
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(est, f)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def formatea_precio(p):
    if p is None:
        return "sin precio"
    return f"{p:,.0f}".replace(",", ".") + " €"


def a_numero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def telegram(texto):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Faltan los secretos TELEGRAM_TOKEN y/o TELEGRAM_CHAT_ID")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": texto, "parse_mode": "HTML"},
        timeout=20,
    )
    if r.status_code != 200:
        # No se imprime la URL (contiene el token)
        raise RuntimeError(f"Telegram respondió {r.status_code}: {r.text[:200]}")


def alertar(est, clave, mensaje, horas=6):
    """Avisa de un problema, como mucho una vez cada `horas` horas."""
    ultimo = est["alertas"].get(clave, 0)
    if time.time() - ultimo < horas * 3600:
        return
    try:
        telegram("⚠️ " + html.escape(mensaje))
        est["alertas"][clave] = time.time()
    except Exception as e:  # noqa: BLE001
        log("No se pudo enviar la alerta:", e)


# --------------------------------------------------------------------------
# Wallapop
# --------------------------------------------------------------------------

WALLAPOP_URL = "https://api.wallapop.com/api/v3/general/search"
WALLAPOP_HEADERS = {
    "User-Agent": "Wallapop/1.0 (iPhone; iOS 17.0)",
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9",
    "X-DeviceOS": "0",
}


def _lista_items(datos):
    """Localiza la lista de anuncios en la respuesta (la estructura cambia)."""
    if not isinstance(datos, dict):
        return []
    d = datos.get("data", datos)
    if not isinstance(d, dict):
        return []
    payload = (d.get("section") or {}).get("payload") or {}
    for candidato in (payload.get("items"), d.get("search_objects"), d.get("items")):
        if isinstance(candidato, list):
            return candidato
    return []


def _normaliza_wallapop(it):
    precio = it.get("price")
    if isinstance(precio, dict):
        precio = precio.get("amount")
    loc = it.get("location") or {}
    slug = it.get("web_slug") or it.get("slug")
    ident = it.get("id")
    if not ident:
        return None
    return {
        "id": str(ident),
        "titulo": (it.get("title") or "").strip(),
        "precio": a_numero(precio),
        "ciudad": loc.get("city") or loc.get("label") or "",
        "region": loc.get("region") or loc.get("region2") or "",
        "cp": str(loc.get("postal_code") or ""),
        "lat": a_numero(loc.get("latitude")),
        "lon": a_numero(loc.get("longitude")),
        "url": f"https://es.wallapop.com/item/{slug}" if slug else "https://es.wallapop.com/",
        "vendido": bool(it.get("sold") or (it.get("flags") or {}).get("sold")),
    }


def wallapop_buscar(sesion, tag, web_cfg, zona, lat, lon):
    cats = web_cfg.get("categorias_ids", CATEGORIAS_WALLAPOP)
    radio = zona.get("radio_km", 80)
    params = {
        "keywords": tag,
        "latitude": lat,
        "longitude": lon,
        "distance_in_km": radio,
        "distance": int(radio * 1000),
        "order_by": "newest",
        "source": "search_box",
    }
    if web_cfg.get("precio_min") is not None:
        params["min_sale_price"] = web_cfg["precio_min"]
    if web_cfg.get("precio_max") is not None:
        params["max_sale_price"] = web_cfg["precio_max"]
    if cats:
        params["category_ids"] = ",".join(str(c) for c in cats)

    ultimo_error = None
    for intento in range(2):
        try:
            r = sesion.get(WALLAPOP_URL, params=params, timeout=20)
        except requests.RequestException as e:
            ultimo_error = f"red: {e.__class__.__name__}"
            time.sleep(3)
            continue
        if r.status_code == 200:
            try:
                return r.json(), r.status_code
            except ValueError:
                raise RuntimeError("Wallapop devolvió algo que no es JSON")
        ultimo_error = f"HTTP {r.status_code}"
        if r.status_code in (403, 429):
            break
        time.sleep(3)
    raise RuntimeError(f"Wallapop falló ({ultimo_error})")


# --------------------------------------------------------------------------
# Ruralpop
# --------------------------------------------------------------------------
# Ruralpop no tiene filtros por URL: se leen las páginas de categoría
# (por ejemplo https://www.ruralpop.com/ganaderia/equino), que salen ordenadas
# de más reciente a más antigua, y se filtra aquí por provincia y precio.

RURALPOP_BASE = "https://www.ruralpop.com"
RURALPOP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-ES,es;q=0.9",
}
RE_PRECIO = re.compile(r"^\d[\d\.]*\s*€$")
INSIGNIAS = {"¡destacado!", "destacado", "negociable"}

# Nombres con los que Ruralpop puede escribir cada provincia
ALIAS_PROVINCIA = {
    "álava": {"alava", "araba"},
    "bizkaia": {"bizkaia", "vizcaya"},
    "gipuzkoa": {"gipuzkoa", "guipuzcoa"},
    "cantabria": {"cantabria"},
    "burgos": {"burgos"},
}


def sin_acentos(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def nombres_provincia(prov):
    clave = prov.lower()
    base = sin_acentos(prov)
    return ALIAS_PROVINCIA.get(clave, {base}) | {base}


def provincia_en(region, lista):
    """¿La provincia/región escrita en el anuncio está en la lista?"""
    r = sin_acentos(region)
    if not r:
        return False
    for prov in lista:
        for nombre in nombres_provincia(prov):
            if nombre in r:
                return True
    return False


def _normaliza_ruralpop(a_tag, url):
    partes = [t.strip() for t in a_tag.stripped_strings if t.strip()]
    precio = None
    edad = ""
    ubicacion = ""
    titulos = []
    for p in partes:
        pl = p.lower()
        if RE_PRECIO.match(p):
            precio = a_numero(p.replace("€", "").replace(".", "").strip())
        elif pl in INSIGNIAS:
            continue
        elif pl.startswith("hace "):
            edad = p
        elif "," in p and len(p) <= 80 and not ubicacion:
            ubicacion = p
        else:
            titulos.append(p)
    if not titulos:
        img = a_tag.find("img", alt=True)
        if img and img["alt"].strip():
            titulos.append(img["alt"].strip())
    if not titulos:
        return None
    ciudad, _, region = ubicacion.rpartition(",")
    if not ubicacion:
        ciudad, region = "", ""
    slug = url.rstrip("/").split("/")[-1]
    return {
        "id": slug,
        "titulo": titulos[0],
        "precio": precio,
        "ciudad": ciudad.strip(),
        "region": region.strip(),
        "cp": "",
        "lat": None,
        "lon": None,
        "url": url,
        "vendido": False,
        "edad": edad,
    }


def ruralpop_buscar(sesion, url, paginas=1):
    anuncios = {}
    for p in range(1, paginas + 1):
        u = url if p == 1 else f"{url}{'&' if '?' in url else '?'}page={p}"
        r = requests.get(u, headers=RURALPOP_HEADERS, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Ruralpop falló (HTTP {r.status_code})")
        soup = BeautifulSoup(r.text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            if "/anuncio/" not in a_tag["href"]:
                continue
            completa = urljoin(RURALPOP_BASE, a_tag["href"]).split("?")[0].split("#")[0]
            if completa in anuncios:
                continue
            ad = _normaliza_ruralpop(a_tag, completa)
            if ad:
                anuncios[completa] = ad
        time.sleep(random.uniform(1, 2))
    return list(anuncios.values())


# --------------------------------------------------------------------------
# Filtros comunes
# --------------------------------------------------------------------------

def descartar(web, a, web_cfg, zona, lat, lon):
    """Devuelve el motivo si hay que descartar el anuncio, o None si vale."""
    if a["vendido"]:
        return "vendido"
    pmin, pmax = web_cfg.get("precio_min"), web_cfg.get("precio_max")
    if a["precio"] is not None:
        if pmin is not None and a["precio"] < pmin:
            return "precio bajo"
        if pmax is not None and a["precio"] > pmax:
            return "precio alto"
    # Provincias excluidas
    for prov in zona.get("provincias_excluidas", []):
        pref = CP_PROVINCIA.get(prov.lower())
        if pref and a["cp"].startswith(pref) and len(a["cp"]) == 5:
            return f"provincia excluida ({prov})"
        if provincia_en(a["region"], [prov]):
            return f"provincia excluida ({prov})"
    # Webs sin coordenadas (Ruralpop): solo se acepta si la provincia está
    # en la lista de provincias incluidas
    if web == "ruralpop":
        incluidas = zona.get("provincias_incluidas", [])
        if not a["region"]:
            if not zona.get("avisar_ubicacion_dudosa", True):
                return "sin ubicación"
        elif incluidas and not provincia_en(a["region"], incluidas):
            return f"fuera de zona ({a['region']})"
    # Distancia (si el anuncio trae coordenadas)
    if a["lat"] is not None and a["lon"] is not None:
        d = haversine_km(lat, lon, a["lat"], a["lon"])
        if d > zona.get("radio_km", 80):
            return f"fuera de radio ({d:.0f} km)"
    # Palabras a excluir (opcional en config)
    titulo = a["titulo"].lower()
    for palabra in web_cfg.get("excluir_palabras", []):
        if palabra.lower() in titulo:
            return f"palabra excluida ({palabra})"
    return None


def mensaje_aviso(web, tag, a):
    lugar = ", ".join(x for x in (a["ciudad"], a["region"]) if x) or "ubicación no indicada"
    nota = ""
    if web == "ruralpop":
        if not a["region"]:
            nota = "\nℹ️ Ubicación dudosa: no indica provincia"
        elif provincia_en(a["region"], ["burgos", "cantabria"]):
            nota = "\nℹ️ Comprueba la distancia: esta provincia es grande"
    return (
        f"🟢 <b>[{html.escape(web.capitalize())}]</b> {html.escape(a['titulo'])}\n"
        f"💶 {formatea_precio(a['precio'])} · 📍 {html.escape(lugar)}\n"
        f"🔎 {html.escape(tag)}{html.escape(nota) if nota else ''}\n"
        f"{html.escape(a['url'])}"
    )


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------

def busquedas_de(web, web_cfg):
    """Lista de búsquedas de una web como (nombre, url, palabras).

    Wallapop busca por tags. Ruralpop lee páginas de categoría (`urls`), cada
    una puede ser un texto o un bloque con `url`, `nombre` y `palabras`.
    """
    if web == "wallapop":
        return [(t, None, None) for t in web_cfg.get("tags", [])]
    salida = []
    for entrada in web_cfg.get("urls", []):
        if isinstance(entrada, str):
            entrada = {"url": entrada}
        url = entrada.get("url")
        if not url:
            continue
        nombre = entrada.get("nombre") or url.rstrip("/").split("/")[-1]
        salida.append((nombre, url, entrada.get("palabras") or []))
    if not salida:
        log("[ruralpop] no hay 'urls' en la configuración, se omite")
    return salida


def main():
    probar = "--probar" in sys.argv
    cfg = cargar_config()
    zona = cfg.get("zona", {})
    lat = zona.get("latitud", LAT_DEFECTO)
    lon = zona.get("longitud", LON_DEFECTO)
    webs = cfg.get("webs", {})
    est = cargar_estado()

    sesion = requests.Session()
    sesion.headers.update(WALLAPOP_HEADERS)

    pendientes = []          # (web, tag, anuncio)
    inicializados = []       # "web/tag" marcados en silencio
    n_marcados = 0
    vistos_en_ejecucion = set()

    for web, web_cfg in webs.items():
        if not web_cfg.get("activa"):
            continue
        if web not in ("wallapop", "ruralpop"):
            log(f"[{web}] sin soporte todavía, se omite")
            continue
        for tag, url_rp, palabras in busquedas_de(web, web_cfg):
            clave_tag = f"{web}|{tag}"
            try:
                if web == "wallapop":
                    datos, estado_http = wallapop_buscar(sesion, tag, web_cfg, zona, lat, lon)
                    items = _lista_items(datos)
                    anuncios = [x for x in (_normaliza_wallapop(i) for i in items) if x]
                else:  # ruralpop
                    datos, estado_http = {}, 200
                    anuncios = ruralpop_buscar(sesion, url_rp, web_cfg.get("paginas", 1))
                    if palabras:
                        anuncios = [a for a in anuncios if any(
                            sin_acentos(w) in sin_acentos(a["titulo"]) for w in palabras)]
            except Exception as e:  # noqa: BLE001
                log(f"[{web}/{tag}] error: {e}")
                if not probar:
                    alertar(est, f"error|{web}",
                            f"No he podido consultar {web.capitalize()}: {e}. "
                            "Lo sigo intentando en cada ejecución.")
                time.sleep(random.uniform(1, 2))
                continue

            if probar:
                log(f"\n=== {web} / '{tag}': {len(anuncios)} anuncios (HTTP {estado_http}) ===")
                if not anuncios:
                    log("Claves de primer nivel en la respuesta:",
                        list(datos.keys()) if isinstance(datos, dict) else type(datos))
                    log("Respuesta (primeros 600 caracteres):",
                        json.dumps(datos, ensure_ascii=False)[:600])
                for a in anuncios[:5]:
                    motivo = descartar(web, a, web_cfg, zona, lat, lon)
                    log(f"- {a['titulo'][:50]!r} | {formatea_precio(a['precio'])} | "
                        f"{a['ciudad']} {a['region']} {a['cp']} | "
                        f"{'DESCARTADO: ' + motivo if motivo else 'valido'}")
                time.sleep(random.uniform(1, 2))
                continue

            vistos = est["vistos"].setdefault(web, {})
            primera_vez = clave_tag not in est["listos"]
            nuevos = 0
            for a in anuncios:
                if a["id"] in vistos or (web, a["id"]) in vistos_en_ejecucion:
                    continue
                if descartar(web, a, web_cfg, zona, lat, lon):
                    # Se recuerda igualmente para no reevaluarlo cada vez
                    vistos[a["id"]] = time.time()
                    continue
                vistos[a["id"]] = time.time()
                vistos_en_ejecucion.add((web, a["id"]))
                if primera_vez:
                    n_marcados += 1
                else:
                    pendientes.append((web, tag, a))
                    nuevos += 1
            if primera_vez:
                est["listos"].append(clave_tag)
                inicializados.append(f"{web}/{tag}")
            log(f"[{web}/{tag}] {len(anuncios)} anuncios, {nuevos} nuevos")
            time.sleep(random.uniform(1, 2.5))

    if probar:
        log("\nModo prueba: no se ha avisado ni guardado nada.")
        return

    # Avisos
    try:
        if inicializados:
            telegram(
                "✅ <b>Robot activado.</b> He dejado como ya vistos "
                f"{n_marcados} anuncios que había ahora mismo "
                f"({len(inicializados)} búsquedas). A partir de aquí solo te aviso de los nuevos."
            )
        for web, tag, a in pendientes[:MAX_AVISOS_POR_EJECUCION]:
            telegram(mensaje_aviso(web, tag, a))
            time.sleep(0.7)
        resto = len(pendientes) - MAX_AVISOS_POR_EJECUCION
        if resto > 0:
            telegram(f"… y {resto} anuncios nuevos más. Revisa las webs.")
    except Exception as e:  # noqa: BLE001
        # Si Telegram falla, no se guarda el estado: así se reintentará
        log("Error enviando a Telegram:", e)
        sys.exit(1)

    guardar_estado(est)
    log(f"Hecho. Avisos enviados: {min(len(pendientes), MAX_AVISOS_POR_EJECUCION)}")


if __name__ == "__main__":
    main()
