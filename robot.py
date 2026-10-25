#!/usr/bin/env python3
"""Robot de anuncios de Ruralpop: avisa por Telegram de TODOS los anuncios
nuevos que estén dentro del radio indicado desde un punto central.

Uso:
    python robot.py            # ejecución normal (avisa y guarda estado)
    python robot.py --probar   # prueba: muestra lo que lee, sin avisar ni guardar

La configuración se lee de la variable de entorno CONFIG_YAML (secreto de
GitHub) o, si no existe, del archivo config.yaml.
Secretos necesarios para avisar: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID.

Ruralpop solo indica "pueblo, provincia". Para medir la distancia, el robot
busca las coordenadas del pueblo en OpenStreetMap (Nominatim) y las guarda
para no repetir la consulta.
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
DIAS_GUARDAR = 60              # cuánto tiempo se recuerdan los anuncios vistos
DIAS_REINTENTAR_GEO = 7        # cuándo reintentar un pueblo que no se encontró
MAX_AVISOS_POR_EJECUCION = 20
MAX_GEOCODIFICAR_POR_EJECUCION = 60
MAX_ERRORES_GEO = 3            # tras estos errores seguidos se deja de consultar

BASE = "https://www.ruralpop.com"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-ES,es;q=0.9",
}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "robot-anuncios-personal/1.0 (GitHub Actions)",
                     "Accept-Language": "es"}

# Centro por defecto: Llodio/Laudio (01400)
LAT_DEFECTO, LON_DEFECTO = 43.1409, -2.9633
RADIO_DEFECTO = 100
# Provincias que pueden tener pueblos a menos de ~110 km de Llodio. Solo
# sirve para no consultar pueblos de provincias lejanas (ahorra consultas).
CANDIDATAS_DEFECTO = ["Álava", "Bizkaia", "Gipuzkoa", "Cantabria", "Burgos",
                      "La Rioja", "Navarra", "Palencia"]

RE_PRECIO = re.compile(r"^\d[\d\.]*\s*€$")
INSIGNIAS = {"¡destacado!", "destacado", "negociable"}

ALIAS_PROVINCIA = {
    "álava": {"alava", "araba"},
    "bizkaia": {"bizkaia", "vizcaya"},
    "gipuzkoa": {"gipuzkoa", "guipuzcoa"},
    "navarra": {"navarra", "nafarroa"},
}
# Todas las provincias, para reconocer cuando el anuncio solo trae provincia
PROVINCIAS_CONOCIDAS = {
    "alava", "araba", "albacete", "alicante", "almeria", "asturias", "avila",
    "badajoz", "barcelona", "bizkaia", "vizcaya", "burgos", "caceres", "cadiz",
    "cantabria", "castellon", "ciudad real", "cordoba", "a coruna", "la coruna",
    "coruna", "cuenca", "gipuzkoa", "guipuzcoa", "girona", "gerona", "granada",
    "guadalajara", "huelva", "huesca", "illes balears", "baleares", "jaen",
    "la rioja", "las palmas", "leon", "lleida", "lerida", "lugo", "madrid",
    "malaga", "murcia", "navarra", "nafarroa", "ourense", "orense", "palencia",
    "pontevedra", "salamanca", "santa cruz de tenerife", "segovia", "sevilla",
    "soria", "tarragona", "teruel", "toledo", "valencia", "valladolid", "zamora",
    "zaragoza", "ceuta", "melilla",
}


class ErrorGeo(Exception):
    """Fallo de red o de Nominatim (distinto de 'pueblo no encontrado')."""


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
    est.setdefault("listos", [])     # "web|categoria" ya inicializadas
    est.setdefault("alertas", {})    # clave -> marca_de_tiempo
    est.setdefault("geo", {})        # "pueblo|provincia" -> {ok, lat, lon, t}
    return est


def guardar_estado(est):
    limite = time.time() - DIAS_GUARDAR * 86400
    for web, ids in est["vistos"].items():
        est["vistos"][web] = {i: t for i, t in ids.items() if t >= limite}
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(est, f)


def a_numero(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def formatea_precio(p):
    if p is None:
        return "sin precio"
    return f"{p:,.0f}".replace(",", ".") + " €"


def sin_acentos(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower().strip()


def nombres_provincia(prov):
    base = sin_acentos(prov)
    return ALIAS_PROVINCIA.get(prov.lower(), {base}) | {base}


def provincia_en(region, lista):
    """¿La provincia escrita en el anuncio está en la lista?"""
    r = sin_acentos(region)
    if not r:
        return False
    return any(n in r for prov in lista for n in nombres_provincia(prov))


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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
    if time.time() - est["alertas"].get(clave, 0) < horas * 3600:
        return
    try:
        telegram("⚠️ " + html.escape(mensaje))
        est["alertas"][clave] = time.time()
    except Exception as e:  # noqa: BLE001
        log("No se pudo enviar la alerta:", e)


# --------------------------------------------------------------------------
# Ruralpop
# --------------------------------------------------------------------------
# Ruralpop no tiene filtros por URL: se leen las páginas de categoría
# (por ejemplo https://www.ruralpop.com/ganaderia/equino), ordenadas de más
# reciente a más antigua.

def _normaliza(a_tag, url):
    partes = [t.strip() for t in a_tag.stripped_strings if t.strip()]
    precio, edad, ubicacion, titulos = None, "", "", []
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
    ciudad, region = "", ""
    if ubicacion:
        resto, _, region = ubicacion.rpartition(",")
        ciudad = resto.split(",")[0]      # "Vega de Pas, Vega de Pas" -> "Vega de Pas"
    else:
        # Ubicación que solo trae la provincia (sin coma)
        for t in titulos[1:]:
            if sin_acentos(t) in PROVINCIAS_CONOCIDAS:
                region = t
                break
    return {
        "id": url.rstrip("/").split("/")[-1],
        "titulo": titulos[0],
        "precio": precio,
        "ciudad": ciudad.strip(),
        "region": region.strip(),
        "url": url,
        "edad": edad,
    }


def leer_categoria(url, paginas=1):
    anuncios = {}
    for p in range(1, paginas + 1):
        u = url if p == 1 else f"{url}{'&' if '?' in url else '?'}page={p}"
        r = requests.get(u, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Ruralpop falló (HTTP {r.status_code})")
        soup = BeautifulSoup(r.text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            if "/anuncio/" not in a_tag["href"]:
                continue
            completa = urljoin(BASE, a_tag["href"]).split("?")[0].split("#")[0]
            if completa in anuncios:
                continue
            ad = _normaliza(a_tag, completa)
            if ad:
                anuncios[completa] = ad
        if p < paginas:
            time.sleep(random.uniform(1, 2))
    return list(anuncios.values())


# --------------------------------------------------------------------------
# Distancia
# --------------------------------------------------------------------------

def geocodificar(est, ciudad, region, ctx):
    """Devuelve (lat, lon), o None si el pueblo no se encuentra.
    Lanza ErrorGeo si falla la consulta (para reintentar más tarde)."""
    clave = sin_acentos(f"{ciudad}|{region}")
    guardado = est["geo"].get(clave)
    if guardado:
        if guardado["ok"]:
            return guardado["lat"], guardado["lon"]
        if time.time() - guardado["t"] < DIAS_REINTENTAR_GEO * 86400:
            return None
    if ctx["errores"] >= MAX_ERRORES_GEO or ctx["consultas"] >= MAX_GEOCODIFICAR_POR_EJECUCION:
        raise ErrorGeo("límite de consultas alcanzado en esta ejecución")
    ctx["consultas"] += 1
    time.sleep(1.1)   # Nominatim pide como máximo 1 consulta por segundo
    try:
        r = requests.get(
            NOMINATIM,
            params={"q": f"{ciudad}, {region}, España", "format": "json",
                    "limit": 1, "countrycodes": "es"},
            headers=NOMINATIM_HEADERS, timeout=20,
        )
        if r.status_code != 200:
            raise ErrorGeo(f"Nominatim HTTP {r.status_code}")
        datos = r.json()
    except ErrorGeo:
        ctx["errores"] += 1
        raise
    except (requests.RequestException, ValueError) as e:
        ctx["errores"] += 1
        raise ErrorGeo(f"Nominatim: {e.__class__.__name__}")
    ctx["errores"] = 0
    if not datos:
        est["geo"][clave] = {"ok": False, "t": time.time()}
        return None
    lat, lon = float(datos[0]["lat"]), float(datos[0]["lon"])
    est["geo"][clave] = {"ok": True, "lat": lat, "lon": lon, "t": time.time()}
    return lat, lon


def evaluar(a, zona, est, ctx, centro):
    """Devuelve (veredicto, detalle):
       ("ok", km)      dentro del radio
       ("dudoso", txt) no se puede calcular la distancia
       ("fuera", txt)  descartar
    Lanza ErrorGeo si hay que reintentar en la próxima ejecución."""
    candidatas = zona.get("provincias_candidatas", CANDIDATAS_DEFECTO)
    for prov in zona.get("provincias_excluidas", []) or []:
        if provincia_en(a["region"], [prov]):
            return "fuera", f"provincia excluida ({prov})"
    if a["region"] and candidatas and not provincia_en(a["region"], candidatas):
        return "fuera", f"provincia lejana ({a['region']})"
    if not a["region"] or not a["ciudad"]:
        return "dudoso", "solo indica provincia" if a["region"] else "sin ubicación"
    pos = geocodificar(est, a["ciudad"], a["region"], ctx)
    if pos is None:
        return "dudoso", "no he encontrado el pueblo en el mapa"
    km = haversine_km(centro[0], centro[1], pos[0], pos[1])
    if km > zona.get("radio_km", RADIO_DEFECTO):
        return "fuera", f"a {km:.0f} km"
    return "ok", km


def mensaje_aviso(cat, a, veredicto, detalle):
    lugar = ", ".join(x for x in (a["ciudad"], a["region"]) if x) or "ubicación no indicada"
    if veredicto == "ok":
        lugar += f" (a {detalle:.0f} km)"
    else:
        lugar += f"\nℹ️ Ubicación dudosa: {detalle}"
    antiguedad = f" · {a['edad']}" if a["edad"] else ""
    return (
        f"🟢 <b>[{html.escape(cat['nombre'])}]</b> {html.escape(a['titulo'])}\n"
        f"💶 {formatea_precio(a['precio'])} · 📍 {html.escape(lugar)}"
        f"{html.escape(antiguedad)}\n"
        f"{html.escape(a['url'])}"
    )


# --------------------------------------------------------------------------
# Programa principal
# --------------------------------------------------------------------------

def main():
    probar = "--probar" in sys.argv
    cfg = cargar_config()
    zona = cfg.get("zona", {})
    centro = (zona.get("latitud", LAT_DEFECTO), zona.get("longitud", LON_DEFECTO))
    categorias = [c for c in cfg.get("categorias", []) if c.get("activa", True)]
    if not categorias:
        log("No hay categorías en la configuración (clave 'categorias').")
        sys.exit(1)

    est = cargar_estado()
    vistos = est["vistos"].setdefault("ruralpop", {})
    ctx = {"consultas": 0, "errores": 0}
    pendientes = []          # (categoria, anuncio, veredicto, detalle)
    inicializadas = []
    n_marcados = 0
    vistos_ahora = set()
    dudosos_permitidos = zona.get("avisar_ubicacion_dudosa", True)

    for cat in categorias:
        nombre, url = cat.get("nombre"), cat.get("url")
        if not nombre or not url:
            log(f"Categoría mal escrita (falta nombre o url): {cat}")
            continue
        clave = f"ruralpop|{nombre}"
        try:
            anuncios = leer_categoria(url, cat.get("paginas", 1))
        except Exception as e:  # noqa: BLE001
            log(f"[{nombre}] error: {e}")
            if not probar:
                alertar(est, f"error|{nombre}",
                        f"No he podido leer la categoría '{nombre}' de Ruralpop: {e}. "
                        "Lo sigo intentando en cada ejecución.")
            time.sleep(random.uniform(1, 2))
            continue

        if not anuncios:
            log(f"[{nombre}] 0 anuncios leídos: puede que la web haya cambiado")
            if not probar:
                alertar(est, f"vacio|{nombre}",
                        f"La categoría '{nombre}' de Ruralpop no ha devuelto ningún anuncio. "
                        "Puede que la web haya cambiado su diseño.", horas=24)
            continue

        # ---- Modo prueba: muestra el veredicto de los primeros anuncios
        if probar:
            log(f"\n=== {nombre}: {len(anuncios)} anuncios leídos ===")
            for a in anuncios[:6]:
                try:
                    v, d = evaluar(a, zona, est, ctx, centro)
                    res = (f"DENTRO, a {d:.0f} km" if v == "ok"
                           else ("DUDOSO: " if v == "dudoso" else "DESCARTADO: ") + d)
                except ErrorGeo as e:
                    res = f"(sin calcular: {e})"
                log(f"- {a['titulo'][:40]!r} | {formatea_precio(a['precio'])} | "
                    f"{a['ciudad']}, {a['region']} | {res}")
            time.sleep(random.uniform(1, 2))
            continue

        # ---- Primera vez en esta categoría: se deja todo como visto, en silencio
        if clave not in est["listos"]:
            for a in anuncios:
                if a["id"] not in vistos:
                    vistos[a["id"]] = time.time()
                    n_marcados += 1
            est["listos"].append(clave)
            inicializadas.append(nombre)
            log(f"[{nombre}] inicializada con {len(anuncios)} anuncios")
            time.sleep(random.uniform(1, 2))
            continue

        # ---- Revisión normal
        nuevos = 0
        for a in anuncios:
            if a["id"] in vistos or a["id"] in vistos_ahora:
                continue
            try:
                v, d = evaluar(a, zona, est, ctx, centro)
            except ErrorGeo as e:
                log(f"[{nombre}] '{a['titulo'][:30]}' se reintentará más tarde ({e})")
                continue                    # no se marca como visto
            vistos[a["id"]] = time.time()   # se recuerda aunque se descarte
            vistos_ahora.add(a["id"])
            if v == "fuera" or (v == "dudoso" and not dudosos_permitidos):
                continue
            pendientes.append((cat, a, v, d))
            nuevos += 1
        log(f"[{nombre}] {len(anuncios)} anuncios, {nuevos} nuevos para avisar")
        time.sleep(random.uniform(1, 2.5))

    if probar:
        log(f"\nModo prueba: no se ha avisado ni guardado nada "
            f"({ctx['consultas']} consultas al mapa).")
        return

    if ctx["errores"] >= MAX_ERRORES_GEO:
        alertar(est, "geo", "No consigo consultar el mapa (Nominatim) para calcular "
                "distancias. Los anuncios afectados se reintentarán en la próxima revisión.",
                horas=12)

    try:
        if inicializadas:
            telegram(
                "✅ <b>Robot activado.</b> Categorías nuevas: "
                f"{html.escape(', '.join(inicializadas))}. He dejado como ya vistos "
                f"{n_marcados} anuncios que había ahora mismo. "
                "A partir de aquí te aviso de todos los nuevos dentro de tu radio."
            )
        for cat, a, v, d in pendientes[:MAX_AVISOS_POR_EJECUCION]:
            telegram(mensaje_aviso(cat, a, v, d))
            time.sleep(0.7)
        resto = len(pendientes) - MAX_AVISOS_POR_EJECUCION
        if resto > 0:
            telegram(f"… y {resto} anuncios nuevos más. Revisa Ruralpop.")
    except Exception as e:  # noqa: BLE001
        # Si Telegram falla no se guarda el estado: así se reintentará
        log("Error enviando a Telegram:", e)
        sys.exit(1)

    guardar_estado(est)
    log(f"Hecho. Avisos enviados: {min(len(pendientes), MAX_AVISOS_POR_EJECUCION)} "
        f"({ctx['consultas']} consultas al mapa)")


if __name__ == "__main__":
    main()
